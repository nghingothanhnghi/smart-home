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
config.TYPE_TO_HARDWARE and it shows up here automatically. The same
goes for moving a channel onto a GPIO expander pin (see
gpio_manager.py / mcp23017.py) - TYPE_TO_GPIO's value is just a
descriptor string, native or expander, and this file doesn't care
which.
"""

import config
import gpio_manager
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
        self._pin_to_type = self._build_pin_map()
        
    def _build_pin_map(self):
        """
        Maps identifiers a /hydro/status row could plausibly carry back
        to our local actuator_type key (light_1..light_6, sliding_door):
        the raw descriptor string we registered with (e.g. "13" or
        "mcp:0:5", matching the row's 'pin' field) AND the synthetic
        int gpio_manager.registration_port() sent as the row's 'port'
        field. Backend rows are identified by pin/port on their end -
        'type' is a freeform, user-editable label there (someone could
        rename a light's type to 'pump' on the dashboard), so it can't
        be trusted to tell us WHICH physical channel a /hydro/status
        row refers to.
        """
        pin_map = {}
        for actuator_type, pin_no in config.TYPE_TO_GPIO.items():
            hardware = config.TYPE_TO_HARDWARE.get(actuator_type, "relay")
            pin_map[str(pin_no)] = actuator_type

            if hardware == "door":
                # TYPE_TO_GPIO's value for the door ("32,23") is a
                # combined, display-only string - not a real
                # single-pin descriptor, so it can't go through
                # gpio_manager.registration_port(). Index the actual
                # open-pin descriptor (config.DOOR_OPEN_PIN) instead,
                # since the backend may report just that as 'port'.
                open_pin = config.DOOR_OPEN_PIN
                pin_map[str(open_pin)] = actuator_type
                pin_map[str(gpio_manager.registration_port(open_pin))] = actuator_type
            else:
                pin_map[str(gpio_manager.registration_port(pin_no))] = actuator_type
        return pin_map

    def resolve_actuator_type(self, item):
        """actuator_type for a /hydro/status row, resolved by pin/port."""
        for key in ("pin", "port"):
            val = item.get(key)
            if val is not None and str(val) in self._pin_to_type:
                return self._pin_to_type[str(val)]
        return None        

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

        'port' is an Integer column server-side, so it can't hold a
        GPIO-expander descriptor like "mcp:0:5" directly -
        gpio_manager.registration_port() gives back the real GPIO
        number for native pins (unchanged from before expander support
        existed) or a synthetic, collision-free int for expander pins.
        'pin' (String) always carries the human-readable descriptor
        itself, so the physical wiring is still recoverable from the
        backend row.

        Only fields present in HydroActuatorBase/Create/Update are sent -
        'hardware' and 'supported_actions' aren't modeled server-side and
        were previously silently dropped by pydantic on every call, so
        they're not included anymore. That info stays firmware-side, in
        config.TYPE_TO_HARDWARE.
        
        """
        actuators = []
        for actuator_type, pin_no in config.TYPE_TO_GPIO.items():
            hardware = config.TYPE_TO_HARDWARE.get(actuator_type, "relay")
                
            if hardware == "door":
                # `port` (Integer) can only hold one pin - keep it as the
                # open pin's registration port for numeric/back-compat
                # display. `pin` (String) CAN hold both - store
                # "open,close" there so the backend record actually
                # reflects the real wiring instead of silently losing
                # the close pin.
                pin_field = gpio_manager.registration_port(config.DOOR_OPEN_PIN)
                pin_str = "%s,%s" % (config.DOOR_OPEN_PIN, config.DOOR_CLOSE_PIN)
            else:
                pin_field = gpio_manager.registration_port(pin_no)
                pin_str = str(pin_no)

            label = _title_case(actuator_type.replace("_", " "))
            entry = {
                "actuator_id": actuator_type,
                "type": actuator_type,
                "name": label,
                "pin": pin_str,        # <-- door: "32,23"; expander pin: e.g. "mcp:0:5"
                "port": pin_field,
                "is_active": True,
                "default_state": False,  # every channel boots OFF (relay.py) - keep backend's default matching
                "sensor_key": None,
                "manual_state": None,    # AUTO by default; dashboard/app sets this to override
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
        """
        Runs the relay safety sweep (force-off on stuck/long-running
        channels) and services any in-flight non-blocking door pulses
        (PULSE-mode auto-release - see relay.py's service_pulses()).
        """
        self.relays.service_pulses()
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

