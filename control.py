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

        if now - self._last_sensor_push >= config.SEND_INTERVAL:
            self._last_sensor_push = now
            self.push_sensor_data()
            self.push_flow_data()