"""
actuators.py
------------
Maps physical relay channels (relay.py) to logical "actuators" the
FastAPI backend knows about (e.g. "light_1", "sliding_door"), builds
the registration payload sent on boot, and dispatches incoming
commands from control.py to the right relay action.

This is the layer you'd extend to add a new device type later
(e.g. a curtain, a fan) without touching relay.py or control.py.
"""

import time

import config
from relay import RelayManager


class ActuatorManager:
    def __init__(self):
        self.relays = RelayManager()
        self._door_pulse_deadline = None  # for PULSE mode auto-stop

    # ---------------------------------------------------------
    # Registration payload (sent to POST /devices/register)
    # ---------------------------------------------------------
    def registration_payload(self):
        actuators = []

        for name in config.LIGHT_PINS.keys():
            actuators.append({
                "actuator_id": name,
                "type": "light",
                "label": name.replace("_", " ").title(),
                "supported_actions": ["on", "off", "toggle"],
                "voltage": "220V",
            })

        actuators.append({
            "actuator_id": "sliding_door",
            "type": "door",
            "label": "Sliding Door",
            "supported_actions": ["open", "close", "stop"],
            "voltage": "220V",
            "mode": config.DOOR_MODE,
        })

        return actuators

    # ---------------------------------------------------------
    # Command execution
    # ---------------------------------------------------------
    def execute(self, command):
        """
        command: dict like:
            {"command_id": "...", "actuator_id": "light_1", "action": "on"}
            {"command_id": "...", "actuator_id": "sliding_door", "action": "open"}

        Returns (success: bool, message: str) for acking back to the backend.
        """
        actuator_id = command.get("actuator_id")
        action = command.get("action")

        try:
            if actuator_id in config.LIGHT_PINS:
                return self._execute_light(actuator_id, action)
            elif actuator_id == "sliding_door":
                return self._execute_door(action)
            else:
                return False, "unknown actuator_id: %s" % actuator_id
        except Exception as e:
            return False, "error executing command: %s" % str(e)

    def _execute_light(self, actuator_id, action):
        relay = self.relays.get(actuator_id)
        if relay is None:
            return False, "no relay mapped for %s" % actuator_id

        if action == "on":
            relay.on()
        elif action == "off":
            relay.off()
        elif action == "toggle":
            relay.toggle()
        else:
            return False, "unsupported action '%s' for light" % action

        return True, "%s -> %s" % (actuator_id, "on" if relay.is_on() else "off")

    def _execute_door(self, action):
        if action == "open":
            self.relays.energize_door("open")
            self._arm_pulse_autostop()
        elif action == "close":
            self.relays.energize_door("close")
            self._arm_pulse_autostop()
        elif action == "stop":
            self.relays.stop_door()
            self._door_pulse_deadline = None
        else:
            return False, "unsupported action '%s' for door" % action

        return True, "door -> %s" % action

    def _arm_pulse_autostop(self):
        if config.DOOR_MODE == "PULSE":
            self._door_pulse_deadline = time.time() + config.DOOR_PULSE_S
        else:
            self._door_pulse_deadline = None

    # ---------------------------------------------------------
    # Maintenance loop hooks (call from main.py's loop)
    # ---------------------------------------------------------
    def tick(self):
        """Handles PULSE-mode auto-stop and relay safety sweep."""
        if self._door_pulse_deadline is not None and time.time() >= self._door_pulse_deadline:
            self.relays.stop_door()
            self._door_pulse_deadline = None

        tripped = self.relays.safety_sweep()
        if tripped:
            print("[actuators] safety sweep force-stopped:", tripped)
        return tripped

    def state_snapshot(self):
        raw = self.relays.state_snapshot()
        return {
            "lights": {k: v for k, v in raw.items() if k in config.LIGHT_PINS},
            "door_open": raw.get("door_open", False),
            "door_close": raw.get("door_close", False),
        }

    def all_off(self):
        self.relays.all_off()
