"""
control.py
----------
The device's "brain loop" for the hydro backend:

  - GET  /hydro/status   -> a snapshot per device (config.STATUS_URL).
                             Each entry has an 'actuators' list where
                             every item IS the command - there's no
                             separate action/state field to look for:

                               {"type": "light", "current_state": true,
                                "manual_state": null, "mode": "auto", ...}

                             The desired output is manual_state if it's
                             not null (a manual override from the
                             dashboard/app), otherwise current_state
                             (the backend's own automation decision -
                             see the 'automation' block, which is what
                             computes current_state from sensor
                             thresholds). Polling this also doubles as
                             our heartbeat, since there's no separate
                             heartbeat endpoint in this backend.

  - POST /sensor/data    -> periodic sensor telemetry (config.SENSOR_URL).
                             This is what feeds the backend's own
                             automation engine, which is why the ESP32
                             doesn't need to push actuator state back -
                             the backend already knows what it decided.

Both intervals are driven by config.SEND_INTERVAL. auth.py's login()
is retried transparently whenever a request comes back 401 (expired
token, or a DB wipe that removed the backing user).

IMPORTANT: config.ACTUATOR_BULK_URL / POST /actuators/bulk is NOT
called from here anymore. It's a *creation* endpoint (confirmed live -
calling it repeatedly created 6x duplicate rows per actuator type in
one boot session), not an upsert/state-update endpoint. It's only
meant to be called once, from device.py's registration flow. If you
want the ESP32 to report state back for display, ask the backend
maintainer for the actual per-actuator update route (something like
PATCH /actuators/{id}) - don't repurpose bulk-create for it.

The backend is the source of truth *unless* config.AUTO_MODE["enabled"]
is True, in which case backend commands are ignored so local
automation logic (if any) doesn't get fought over the actuators.
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

HTTP_TIMEOUT_S = getattr(config, "HTTP_TIMEOUT_S", 8)


class ControlLoop:
    def __init__(self, device, actuator_manager):
        self.device = device
        self.actuators = actuator_manager

        self._last_status_poll = 0
        self._last_sensor_push = 0

    # ---------------------------------------------------------
    # Commands: GET /hydro/status
    # ---------------------------------------------------------
    def poll_status(self):
        if config.AUTO_MODE["enabled"]:
            # Local automation owns the actuators - don't even ask the
            # backend for commands that would fight it.
            return []

        # /hydro/status returns every device's full snapshot in one
        # response (currently tens of KB thanks to duplicate actuator
        # rows on the backend - see the module docstring). Parsing
        # that needs one large contiguous allocation, which is the
        # scarcest thing on an ESP32's heap - collect first to give it
        # the best chance of finding a big enough free block.
        gc.collect()

        try:
            resp = urequests.get(config.STATUS_URL, headers=auth.build_headers(),
                                  timeout=HTTP_TIMEOUT_S)

            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return []
                resp = urequests.get(config.STATUS_URL, headers=auth.build_headers(),
                                      timeout=HTTP_TIMEOUT_S)

            if resp.status_code != 200:
                resp.close()
                return []

            data = resp.json()
            resp.close()
            resp = None
            gc.collect()  # free the raw response buffer promptly

            entries = data if isinstance(data, list) else [data]
            my_entry = self._find_my_entry(entries)
            if my_entry is None:
                return []

            return self._extract_commands(my_entry.get("actuators", []))

        except MemoryError as e:
            # Ran out of contiguous heap parsing the response - not
            # fatal, just skip this cycle. gc.collect() here helps
            # clear any partially-built objects before the next tick.
            print("[control] poll_status out of memory:", e)
            gc.collect()
            return []

        except Exception as e:
            print("[control] poll_status failed:", e)
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

    def _extract_commands(self, raw_actuators):
        """
        Each item in the 'actuators' list IS the desired state for
        that actuator type - manual_state wins when set (a dashboard/
        app override), otherwise current_state (the backend's
        automation-engine decision) applies.

        Duplicate rows of the same type are collapsed to one command -
        this backend accumulates one row per bulk-register call rather
        than upserting by (device, type), so a device with a messy
        registration history can have several rows per physical
        actuator. They're kept consistent with each other by the
        backend's own automation engine, so the last one seen is as
        good as any - but see the docstring at the top of this file:
        the real fix is to stop calling /actuators/bulk repeatedly,
        which is now done.
        """
        desired = {}
        for item in raw_actuators:
            if not isinstance(item, dict):
                continue

            actuator_type = item.get("type")
            if actuator_type is None:
                continue

            manual_state = item.get("manual_state")
            state = manual_state if manual_state is not None else item.get("current_state")
            if state is None:
                continue

            desired[actuator_type] = bool(state)

        return [
            {"actuator_id": actuator_type, "action": "on" if on else "off"}
            for actuator_type, on in desired.items()
        ]

    # ---------------------------------------------------------
    # Sensor data: POST /sensor/data
    # ---------------------------------------------------------
    def push_sensor_data(self):
        if sensors is None:
            return
        readings = sensors.read_all()
        if not readings:
            return

        payload = {"device_id": self.device.device_id, "timestamp": time.time()}
        payload.update(readings)
        self._post(config.SENSOR_URL, payload, "sensor data")

    def _post(self, url, payload, label):
        body = ujson.dumps(payload)
        try:
            resp = urequests.post(url, data=body, headers=auth.build_headers(),
                                   timeout=HTTP_TIMEOUT_S)
            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return
                resp = urequests.post(url, data=body, headers=auth.build_headers(),
                                       timeout=HTTP_TIMEOUT_S)

            status = resp.status_code
            if status >= 300:
                try:
                    detail = resp.text
                except Exception:
                    detail = "<no body>"
                resp.close()
                print("[control] push %s rejected, status %s" % (label, status))
                print("[control] backend said:", detail)
                return

            resp.close()
        except Exception as e:
            print("[control] push %s failed: %s" % (label, e))

    # ---------------------------------------------------------
    # Main tick, call this frequently from main.py's loop
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
