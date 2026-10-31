"""
control.py
----------
The device's "brain loop" for the hydro backend:

  - GET  /hydro/status?device_id=<numeric_id>
        Scoped to THIS device (the unscoped call returns every device's
        full snapshot, ~40 KB, which runs the ESP32 out of heap and made
        poll_status() silently return [] -> no commands ever executed and
        the scheduler fell into offline mode).

        Each item in the entry's 'actuators' list IS the command:
          desired = manual_state if not null else current_state
        Rules applied here:
          * is_active == false      -> forced OFF (door: left alone)
          * pending_command "stop"  -> stop wins over on/off this cycle
          * row disappeared since the previous good poll -> forced OFF
            (door: left alone, its own HOLD timeout/stop covers it)
        Polling doubles as the heartbeat (no separate endpoint).

  - POST /hydro/actuator/{actuator_id}/reported-state
        ACK of what the GPIO is ACTUALLY doing. Body:
          {"device_id": <numeric device id>, "reported_state": bool,
           "error": str|null (max 500 chars)}
        It never changes the desired state - it only fills the backend's
        reported_state* columns so the dashboard can show desired vs
        actual. Sent:
          * on boot / first successful registration (every channel),
          * whenever a channel's real state differs from the last state
            the backend acknowledged (commands, safety-sweep trips,
            offline-scheduler changes, WiFi-reconnect catch-up),
          * with "error" set when a command could not be applied.
        Reconciliation is state-diff based, so a failed/lost ack is
        simply retried on the next status interval. At most
        MAX_ACKS_PER_TICK acks go out per interval so a burst can't
        block the event loop for long.

  - POST /sensor/data   -> periodic sensor telemetry.
  - POST /hydro/flow-readings -> per-pump flow rate.

401 handling: one transparent re-login + retry. If that still fails the
control loop backs off 30 s, doubling up to 10 min, so an expired or
rejected credential never hammers the server.

IMPORTANT: POST /actuators/bulk is CREATE-ONLY. It is called only from
device.py's registration flow, never from here.

If config.AUTO_MODE["enabled"] is True, backend commands are ignored.

Optional config.py names (all have defaults):
  HTTP_TIMEOUT_S      request timeout (default 8)
  STATUS_QUERY_EXTRA  extra query string appended to the status URL,
                      e.g. "&compact=1" if the backend adds a slim mode
  REPORTED_STATE_URL  base for acks (default FASTAPI_URL + "/hydro/actuator")
  MAX_ACKS_PER_TICK   max reported-state POSTs per status interval (default 3)
"""

import time
import gc
import ujson
import urequests

import config
import auth

try:
    import sensors
except Exception:
    sensors = None

try:
    import flow_sensor
except Exception:
    flow_sensor = None

HTTP_TIMEOUT_S = getattr(config, "HTTP_TIMEOUT_S", 8)
STATUS_QUERY_EXTRA = getattr(config, "STATUS_QUERY_EXTRA", "")
REPORTED_STATE_URL = getattr(config, "REPORTED_STATE_URL",
                             config.FASTAPI_URL + "/hydro/actuator")
MAX_ACKS_PER_TICK = getattr(config, "MAX_ACKS_PER_TICK", 3)

BACKOFF_START_S = 30
BACKOFF_MAX_S = 600

# sensors.read_all() key -> backend SensorDataCreateSchema 'data' key.
_SENSOR_KEY_MAP = {
    "temperature_c": "temperature",
    "humidity_pct": "humidity",
    "ec_ppm": "ppm",
}


class ControlLoop:
    def __init__(self, device, actuator_manager):
        self.device = device
        self.actuators = actuator_manager

        self._last_status_poll = 0
        self._last_sensor_push = 0

        self.on_status = None        # set by main.py -> scheduler.on_status

        self._known_types = set()    # local types seen in the last good poll
        self._backoff_s = 0
        self._blocked_until = None   # ticks_ms deadline while backing off

        # reported-state ack bookkeeping
        self._reported = {}          # local type -> last state the backend accepted
        self._ack_errors = {}        # local type -> pending error text (<=500 chars)

    # ---------------------------------------------------------
    # Auth backoff
    # ---------------------------------------------------------
    def _in_backoff(self):
        return (self._blocked_until is not None and
                time.ticks_diff(time.ticks_ms(), self._blocked_until) < 0)

    def _start_backoff(self):
        self._backoff_s = (BACKOFF_START_S if self._backoff_s == 0
                           else min(self._backoff_s * 2, BACKOFF_MAX_S))
        self._blocked_until = time.ticks_add(time.ticks_ms(), self._backoff_s * 1000)
        print("[control] auth rejected, backing off %ss" % self._backoff_s)

    def _reset_backoff(self):
        self._backoff_s = 0
        self._blocked_until = None

    def _request(self, method, url, body=None):
        """
        HTTP call with ONE transparent re-login on 401. Returns the open
        response, or None (backoff started on auth failure). Caller must
        close the response.
        """
        def _do():
            if method == "GET":
                return urequests.get(url, headers=auth.build_headers(),
                                     timeout=HTTP_TIMEOUT_S)
            return urequests.post(url, data=body, headers=auth.build_headers(),
                                  timeout=HTTP_TIMEOUT_S)

        resp = _do()
        if resp.status_code != 401:
            return resp

        resp.close()
        if not auth.login():
            self._start_backoff()
            return None

        resp = _do()
        if resp.status_code == 401:
            resp.close()
            self._start_backoff()
            return None
        return resp

    # ---------------------------------------------------------
    # Commands: GET /hydro/status?device_id=<numeric_id>
    # ---------------------------------------------------------
    def poll_status(self):
        if config.AUTO_MODE["enabled"]:
            return []

        if self.device.numeric_id is None:
            return []   # registration not finished; nothing to scope the poll to

        if self._in_backoff():
            return []

        url = config.STATUS_URL + "?device_id=" + str(self.device.numeric_id) + STATUS_QUERY_EXTRA

        # Parsing needs one large contiguous block - the scarcest thing
        # on an ESP32 heap. Collect first.
        gc.collect()

        resp = None
        try:
            resp = self._request("GET", url)
            if resp is None:
                return []

            if resp.status_code != 200:
                print("[control] status poll HTTP", resp.status_code)
                resp.close()
                return []

            data = resp.json()
            resp.close()
            resp = None
            gc.collect()

            self._reset_backoff()

            entries = data if isinstance(data, list) else [data]
            del data
            my_entry = self._find_my_entry(entries)
            del entries
            if my_entry is None:
                print("[control] no status entry for device_id", self.device.numeric_id)
                return []

            if self.on_status:
                try:
                    self.on_status(my_entry)
                except Exception as e:
                    print("[control] on_status failed:", e)

            raw = my_entry.get("actuators", [])
            del my_entry
            commands = self._extract_commands(raw)
            del raw
            gc.collect()
            return commands

        except MemoryError as e:
            print("[control] poll_status out of memory:", e)
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            gc.collect()
            return []

        except Exception as e:
            print("[control] poll_status failed:", e)
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            return []

    def _find_my_entry(self, entries):
        numeric_id = self.device.numeric_id
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if numeric_id is not None and entry.get("device_id") == numeric_id:
                return entry
            device_name = entry.get("device_name") or ""
            if device_name.find(self.device.device_id) != -1:
                return entry
        return None

    def _is_door(self, actuator_type):
        ch = self.actuators.relays.get(actuator_type)
        return ch is not None and ch.hardware == "door"

    def _extract_commands(self, raw_actuators):
        """
        Rows -> commands. Rows are matched to local channels by pin
        (actuators.resolve_actuator_type), never by type/name.
        """
        if not isinstance(raw_actuators, list):
            return []

        desired = {}
        stops = set()
        present = set()

        for item in raw_actuators:
            if not isinstance(item, dict):
                continue

            actuator_type = self.actuators.resolve_actuator_type(item)
            if actuator_type is None:
                print("[control] unresolved status row id=%s pin=%s port=%s" %
                      (item.get("id"), item.get("pin"), item.get("port")))
                continue

            present.add(actuator_type)

            # stop is momentary and overrides on/off this cycle
            if item.get("pending_command") == "stop":
                if actuator_type not in stops:
                    print("[control] pending_command=stop for", actuator_type)
                stops.add(actuator_type)
                continue

            # inactive rows are forced OFF (never auto-drive the door shut/open)
            if item.get("is_active") is False:
                if not self._is_door(actuator_type):
                    desired[actuator_type] = False
                continue

            manual_state = item.get("manual_state")
            state = manual_state if manual_state is not None else item.get("current_state")
            if state is None:
                continue

            desired[actuator_type] = bool(state)

        # Rows that existed last poll but are gone now -> OFF.
        # Only when we actually got a list of rows, so a malformed
        # response can't switch everything off.
        for t in self._known_types - present:
            if t not in desired and t not in stops and not self._is_door(t):
                print("[control] actuator", t, "removed on backend -> OFF")
                desired[t] = False
        self._known_types = present

        commands = [
            {"actuator_id": t, "action": "on" if on else "off"}
            for t, on in desired.items()
            if t not in stops
        ]
        commands += [{"actuator_id": t, "action": "stop"} for t in stops]
        return commands

    # ---------------------------------------------------------
    # Ack: POST /hydro/actuator/{id}/reported-state
    # ---------------------------------------------------------
    def _post_ack(self, backend_id, state, error):
        """
        One reported-state POST. Returns the HTTP status, or None if the
        request never completed (network error / auth backoff).
        """
        if self._in_backoff():
            return None

        url = REPORTED_STATE_URL + "/" + str(backend_id) + "/reported-state"
        body = ujson.dumps({
            "device_id": self.device.numeric_id,   # DB id (int), NOT the string code
            "reported_state": state,
            "error": error,
        })
        resp = None
        try:
            resp = self._request("POST", url, body)
            if resp is None:
                return None

            status = resp.status_code
            if status >= 300:
                try:
                    detail = resp.text
                except Exception:
                    detail = "<no body>"
                print("[control] reported-state %s rejected, status %s: %s" %
                      (backend_id, status, detail))
            resp.close()
            resp = None
            return status
        except Exception as e:
            print("[control] reported-state %s failed: %s" % (backend_id, e))
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            return None

    def sync_reported_states(self):
        """
        Reconcile what the backend last acknowledged against what the
        channels are really doing, and ack any difference (plus any
        pending error). Safe to call every status interval:
          * no-op when nothing changed (no HTTP at all),
          * retried automatically if an ack fails or WiFi was down,
          * capped at MAX_ACKS_PER_TICK requests per call.
        The reported value is the channel's real state (door: True =
        open/up, False = closed/down), read after any command ran.
        """
        if config.AUTO_MODE["enabled"]:
            return
        if self.device.numeric_id is None or self._in_backoff():
            return

        ids = self.device.actuator_ids_by_type
        if not ids:
            return   # backend actuator ids not resolved yet; retry next interval

        sent = 0
        for actuator_type, ch in self.actuators.relays.channels.items():
            if sent >= MAX_ACKS_PER_TICK:
                break

            backend_id = ids.get(actuator_type)
            if backend_id is None:
                continue

            state = bool(ch.is_on())
            error = self._ack_errors.get(actuator_type)
            if error is None and self._reported.get(actuator_type) == state:
                continue   # backend already knows

            sent += 1
            status = self._post_ack(backend_id, state, error)
            if status is None:
                continue   # transient - try again next interval

            if 200 <= status < 300:
                self._reported[actuator_type] = state
                self._ack_errors.pop(actuator_type, None)
                print("[control] ack %s -> %s%s" %
                      (actuator_type, "ON" if state else "OFF",
                       " (error reported)" if error else ""))
            elif status in (408, 429) or status >= 500:
                continue   # server-side/transient - retry next interval
            else:
                # 4xx (e.g. 400 'Actuator does not belong to device',
                # 404): retrying the identical request can't succeed and
                # would just spam the backend every interval. Remember
                # it as handled; a later state change tries again.
                self._reported[actuator_type] = state
                self._ack_errors.pop(actuator_type, None)

    # ---------------------------------------------------------
    # Telemetry
    # ---------------------------------------------------------
    def push_sensor_data(self):
        if sensors is None:
            return
        readings = sensors.read_all()
        if not readings:
            return

        data = {}
        for local_key, value in readings.items():
            if value is None:
                continue
            data[_SENSOR_KEY_MAP.get(local_key, local_key)] = value

        if not data:
            return

        payload = {"device_id": self.device.device_id, "data": data}
        print("[control] sensor payload:", payload)
        self._post(config.SENSOR_URL, payload, "sensor data")

    def push_flow_data(self):
        if flow_sensor is None:
            return
        readings = flow_sensor.read_all()
        if not readings:
            return

        for actuator_type, flow_rate in readings.items():
            actuator_id = self.device.actuator_ids_by_type.get(actuator_type)
            if actuator_id is None:
                continue   # ids not resolved yet; retry next cycle
            payload = {"actuator_id": actuator_id, "flow_rate": flow_rate}
            self._post(config.FLOW_URL, payload, "flow reading (%s)" % actuator_type)

    def _post(self, url, payload, label):
        if self._in_backoff():
            return
        body = ujson.dumps(payload)
        resp = None
        try:
            resp = self._request("POST", url, body)
            if resp is None:
                return

            status = resp.status_code
            if status >= 300:
                try:
                    detail = resp.text
                except Exception:
                    detail = "<no body>"
                print("[control] push %s rejected, status %s" % (label, status))
                print("[control] backend said:", detail)
            resp.close()
            resp = None
        except Exception as e:
            print("[control] push %s failed: %s" % (label, e))
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    # ---------------------------------------------------------
    # Main tick
    # ---------------------------------------------------------
    def tick(self):
        now = time.time()

        if now - self._last_status_poll >= config.SEND_INTERVAL:
            self._last_status_poll = now

            commands = self.poll_status()
            for command in commands:
                success, message = self.actuators.execute(command)
                if message is not None:
                    print("[control] executed", command, "->", success, message)
                if not success:
                    # surfaced to the dashboard via the next reported-state ack
                    self._ack_errors[command["actuator_id"]] = str(message)[:500]

            # Ack real channel state (after commands ran). Also catches
            # safety-sweep trips and offline-scheduler changes.
            self.sync_reported_states()

        if now - self._last_sensor_push >= config.SEND_INTERVAL:
            self._last_sensor_push = now
            self.push_sensor_data()
            self.push_flow_data()