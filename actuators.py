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
        list directly, not an object wrapping it.

        device_id here must be the backend's NUMERIC device PK
        (Device.numeric_id), not our string device_id/device code -
        the backend's actuator table's device_id column is an integer
        foreign key. Pass it to stamp it onto every entry (required
        for registration; omit it for state pushes where it's not
        needed).

        'name' and 'port' are required by the backend's actuator
        schema - 'label'/'gpio' are also included since they don't
        hurt and are handy for debugging/other consumers.
        """
        actuators = []
        for actuator_type, pin_no in config.TYPE_TO_GPIO.items():
            hardware = config.TYPE_TO_HARDWARE.get(actuator_type, "relay")
                
            if hardware == "door":
                # Backend's `port` column is a plain int - can't hold "32,23".
                # Use the OPEN pin as the canonical port/pin for DB/display
                # purposes; both real pins live in config.DOOR_OPEN_PIN /
                # config.DOOR_CLOSE_PIN and are what relay.py actually uses.
                pin_field = config.DOOR_OPEN_PIN
                supported = ["on", "off", "stop"]
            elif hardware == "mosfet":
                pin_field = int(pin_no)
                supported = ["on", "off", "toggle", "speed"]
            else:
                pin_field = int(pin_no)
                supported = ["on", "off", "toggle"]                

            label = _title_case(actuator_type.replace("_", " "))
            entry = {
                "actuator_id": actuator_type,
                "type": actuator_type,
                "name": label,
                "label": label,
                "gpio": str(pin_field),
                "pin": str(pin_field),
                "port": pin_field,
                "hardware": hardware,
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

        Returns (success: bool, message: str|None) for logging/state-push.
        message is None when the command was a no-op (the actuator
        already matched the requested state) - the backend currently
        re-sends every actuator's full desired state on every poll, so
        without this every unchanged actuator would print a log line
        every cycle.
        """
        actuator_id = command.get("actuator_id")
        action = command.get("action")

        channel = self.relays.get(actuator_id)
        if channel is None:
            return False, "unknown actuator_id: %s" % actuator_id

        try:
            before_on = channel.is_on()
            before_speed = channel.speed()

            if action == "on":
                channel.on()
            elif action == "off":
                channel.off()
            elif action == "toggle":
                channel.toggle()
            elif action == "stop":
                if not hasattr(channel, "stop"):
                    return False, "'%s' does not support stop" % actuator_id
                channel.stop()
            elif action == "speed":
                if channel.hardware != "mosfet":
                    return False, "'%s' does not support speed" % actuator_id
                channel.set_speed(command.get("value", 0))
            else:
                return False, "unsupported action '%s' for %s" % (action, actuator_id)

            if action == "stop":
                return True, "%s -> stop" % actuator_id

            changed = channel.is_on() != before_on or channel.speed() != before_speed
            if not changed:
                return True, None

            if channel.hardware == "mosfet":
                result = "%d%%" % channel.speed()
            elif channel.hardware == "door":
                result = "up" if channel.is_on() else "down"
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
