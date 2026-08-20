"""
control.py
----------
The device's "brain loop" for the hydro backend:

  - GET  /hydro/status   -> pending commands (config.STATUS_URL).
                             Polling this also doubles as our
                             heartbeat, since there's no separate
                             heartbeat endpoint in this backend.
  - POST /sensor/data    -> periodic sensor telemetry (config.SENSOR_URL).
  - POST /actuators/bulk -> periodic actuator-state push (config.ACTUATOR_BULK_URL),
                             so the dashboard reflects what's physically
                             energized after every command batch.

Both intervals are driven by config.SEND_INTERVAL. auth.py's login()
is retried transparently whenever a request comes back 401 (expired
token, or a DB wipe that removed the backing user).

The backend is the source of truth *unless* config.AUTO_MODE["enabled"]
is True, in which case backend commands are ignored so local
automation logic (if any) doesn't get fought over the actuators.
"""

import time
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
            return data.get("commands", [])

        except Exception as e:
            print("[control] poll_status failed:", e)
            return []

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

    # ---------------------------------------------------------
    # Actuator state push: POST /actuators/bulk
    # ---------------------------------------------------------
    def push_actuator_state(self):
        payload = {
            "device_id": self.device.device_id,
            "actuators": self.actuators.registration_payload(),
            "states": self.actuators.state_snapshot(),
            "speeds": self.actuators.speed_snapshot(),
        }
        self._post(config.ACTUATOR_BULK_URL, payload, "actuator state")

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
                print("[control] executed", command, "->", success, message)

            # Reflect current actuator state back to the backend on
            # every poll (not just when a command ran), so the
            # dashboard never goes stale.
            self.push_actuator_state()

        if now - self._last_sensor_push >= config.SEND_INTERVAL:
            self._last_sensor_push = now
            self.push_sensor_data()