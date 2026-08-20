"""
control.py
----------
The device's "brain loop": periodically polls the FastAPI backend for
pending commands, executes them via actuators.py, acknowledges each
one, and periodically pushes full state + a heartbeat back up.

The backend is the source of truth. Optional local automation
(ENABLE_LOCAL_AUTOMATION) is only a fallback/enhancement layer and
must never contradict a command that just came from the backend.
"""

import time
import ujson
import urequests

import config
import auth


class ControlLoop:
    def __init__(self, device, actuator_manager):
        self.device = device
        self.actuators = actuator_manager

        self._last_command_poll = 0
        self._last_state_push = 0
        self._last_heartbeat = 0

    # ---------------------------------------------------------
    # Commands: GET /devices/{id}/commands
    # ---------------------------------------------------------
    def poll_commands(self):
        url = config.BACKEND_BASE_URL + config.BACKEND_ENDPOINTS["commands"].format(
            device_id=self.device.device_id
        )
        try:
            resp = urequests.get(url, headers=auth.build_headers(),
                                  timeout=config.HTTP_TIMEOUT_S)
            if resp.status_code != 200:
                resp.close()
                return []
            data = resp.json()
            resp.close()
            return data.get("commands", [])
        except Exception as e:
            print("[control] poll_commands failed:", e)
            return []

    def ack_command(self, command_id, success, message):
        url = config.BACKEND_BASE_URL + config.BACKEND_ENDPOINTS["ack"].format(
            device_id=self.device.device_id, command_id=command_id
        )
        payload = {"success": success, "message": message}
        try:
            resp = urequests.post(url, data=ujson.dumps(payload),
                                   headers=auth.build_headers())
            resp.close()
        except Exception as e:
            print("[control] ack_command failed:", e)

    # ---------------------------------------------------------
    # State: POST /devices/{id}/state
    # ---------------------------------------------------------
    def push_state(self):
        url = config.BACKEND_BASE_URL + config.BACKEND_ENDPOINTS["state"].format(
            device_id=self.device.device_id
        )
        payload = {
            "timestamp": time.time(),
            "state": self.actuators.state_snapshot(),
        }
        try:
            resp = urequests.post(url, data=ujson.dumps(payload),
                                   headers=auth.build_headers())
            resp.close()
        except Exception as e:
            print("[control] push_state failed:", e)

    def push_heartbeat(self):
        url = config.BACKEND_BASE_URL + config.BACKEND_ENDPOINTS["heartbeat"].format(
            device_id=self.device.device_id
        )
        try:
            resp = urequests.post(url, data=ujson.dumps({"timestamp": time.time()}),
                                   headers=auth.build_headers())
            resp.close()
        except Exception as e:
            print("[control] push_heartbeat failed:", e)

    # ---------------------------------------------------------
    # Main tick, call this frequently from main.py's loop
    # ---------------------------------------------------------
    def tick(self):
        now = time.time()

        if now - self._last_command_poll >= config.COMMAND_POLL_INTERVAL_S:
            self._last_command_poll = now
            for command in self.poll_commands():
                success, message = self.actuators.execute(command)
                print("[control] executed", command, "->", success, message)
                self.ack_command(command.get("command_id"), success, message)

        if now - self._last_state_push >= config.STATE_PUSH_INTERVAL_S:
            self._last_state_push = now
            self.push_state()

        if now - self._last_heartbeat >= config.HEARTBEAT_INTERVAL_S:
            self._last_heartbeat = now
            self.push_heartbeat()
