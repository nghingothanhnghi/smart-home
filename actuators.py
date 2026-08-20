"""
actuators.py
------------
Maps the actuator types the FastAPI "hydro" backend knows about
(pump, fan, light, water_pump, valve - config.TYPE_TO_GPIO) to
physical relay/mosfet channels (relay.py), builds the bulk
registration payload sent to POST /actuators/bulk, and dispatches
commands coming back from the GET /hydro/status poll.

This is the layer you'd extend to add a new actuator type later
(e.g. a second valve) - add it to config.TYPE_TO_GPIO /
config.TYPE_TO_HARDWARE and it shows up here automatically.
"""

import config
from relay import RelayManager


def _title_case(s):
    """
    Manual replacement for str.title() - MicroPython's built-in string
    type doesn't implement it, so calling .title() raises
    AttributeError at runtime even though it's fine under CPython.
    """
    return " ".join(w[:1].upper() + w[1:] for w in s.split(" ") if w)


class ActuatorManager:
    def __init__(self):
        self.relays = RelayManager()

    # ---------------------------------------------------------
    # Registration payload (sent to POST /actuators/bulk)
    # ---------------------------------------------------------
    def registration_payload(self, device_id=None):
        """
        Returns a bare list of actuator dicts - the backend's
        /actuators/bulk endpoint expects the POST body to be a JSON
        list directly, not an object wrapping it. Pass device_id to
        stamp it onto every entry (required for registration; omit it
        for state pushes where it's not needed).
        """
        actuators = []
        for actuator_type, pin_no in config.TYPE_TO_GPIO.items():
            hardware = config.TYPE_TO_HARDWARE.get(actuator_type, "relay")
            supported = ["on", "off", "toggle"]
            if hardware == "mosfet":
                supported.append("speed")

            entry = {
                "actuator_id": actuator_type,
                "type": actuator_type,
                "gpio": pin_no,
                "hardware": hardware,
                "label": _title_case(actuator_type.replace("_", " ")),
                "supported_actions": supported,
            }
            if device_id is not None:
                entry["device_id"] = device_id
            actuators.append(entry)
        return actuators

    # ---------------------------------------------------------
    # Command execution
    # ---------------------------------------------------------
    def execute(self, command):
        """
        command: dict like:
            {"command_id": "...", "actuator_id": "pump", "action": "on"}
            {"command_id": "...", "actuator_id": "water_pump",
             "action": "speed", "value": 60}

        actuator_id matches a key in config.TYPE_TO_GPIO (i.e. the
        actuator "type": pump/fan/light/water_pump/valve).

        Returns (success: bool, message: str) for logging/state-push.
        """
        actuator_id = command.get("actuator_id")
        action = command.get("action")

        channel = self.relays.get(actuator_id)
        if channel is None:
            return False, "unknown actuator_id: %s" % actuator_id

        try:
            if action == "on":
                channel.on()
            elif action == "off":
                channel.off()
            elif action == "toggle":
                channel.toggle()
            elif action == "speed":
                if channel.hardware != "mosfet":
                    return False, "'%s' does not support speed" % actuator_id
                channel.set_speed(command.get("value", 0))
            else:
                return False, "unsupported action '%s' for %s" % (action, actuator_id)

            if channel.hardware == "mosfet":
                result = "%d%%" % channel.speed()
            else:
                result = "on" if channel.is_on() else "off"
            return True, "%s -> %s" % (actuator_id, result)

        except Exception as e:
            return False, "error executing command: %s" % str(e)

    # ---------------------------------------------------------
    # Maintenance loop hooks (call from main.py's loop)
    # ---------------------------------------------------------
    def tick(self):
        """Runs the relay safety sweep (force-off on stuck/long-running channels)."""
        tripped = self.relays.safety_sweep()
        if tripped:
            print("[actuators] safety sweep force-stopped:", tripped)
        return tripped

    def state_snapshot(self):
        return self.relays.state_snapshot()

    def speed_snapshot(self):
        return self.relays.speed_snapshot()

    def all_off(self):
        self.relays.all_off()
