"""
gpio_manager.py
----------------
Single place that turns a pin *descriptor string* (what config.py's
TYPE_TO_GPIO / DOOR_OPEN_PIN / DOOR_CLOSE_PIN hold) into an actual pin
object - a native machine.Pin, or a pin on an I2C GPIO expander
(mcp23017.py, or any future expander registered the same way).
relay.py and actuators.py never touch machine.Pin or MCP23017
directly; they go through this module, so moving a channel from a
native GPIO to an expander pin, or adding a second/third expander
chip, is a config.py-only change - exactly the same design principle
TYPE_TO_GPIO already uses for adding a new actuator type.

Descriptor formats:
  "13"        -> native ESP32 GPIO 13 (unchanged from before this
                 module existed - every existing config.py value still
                 works with zero changes)
  "mcp:0:5"   -> pin 5 on the expander registered under unit id "0" in
                 config.GPIO_EXPANDERS

DoorChannel takes two independent descriptors (open_pin, close_pin) -
each can be native or expander on its own; there's no combined
"32,23"-style descriptor at this layer (that comma-joined form only
exists in the backend registration payload - see actuators.py).

To support a different expander chip later (PCF8574, MCP23008,
TCA9555, ...):
  1. Write its driver the same shape as mcp23017.py - a `.pin(n, mode,
     pull)` method returning an object with `.value()`/`.on()`/`.off()`.
  2. Add a builder function below and register it in
     _BACKEND_FACTORIES under a driver name (e.g. "pcf8574").
  3. Give it its own descriptor prefix in `_parse()` (e.g. "pcf:").
Nothing in relay.py, actuators.py, or control.py needs to change.
"""

import config

# unit_id -> already-constructed expander instance. Built lazily so a
# board with an empty config.GPIO_EXPANDERS never touches I2C for this
# at all, and a board with one never pays for a second bus scan.
_expander_cache = {}


def _build_mcp23017(unit_cfg):
    from machine import I2C, Pin
    from mcp23017 import MCP23017

    i2c = I2C(
        unit_cfg.get("i2c_id", 0),
        scl=Pin(unit_cfg["scl"]),
        sda=Pin(unit_cfg["sda"]),
        freq=unit_cfg.get("freq", 400000),
    )
    return MCP23017(i2c, address=unit_cfg.get("addr", 0x20))


# driver name (config.GPIO_EXPANDERS[unit]["driver"]) -> builder(unit_cfg) -> expander instance.
# Add an entry here to support a new expander chip.
_BACKEND_FACTORIES = {
    "mcp23017": _build_mcp23017,
}


def _get_expander(unit_id):
    if unit_id not in _expander_cache:
        expanders = getattr(config, "GPIO_EXPANDERS", {})
        if unit_id not in expanders:
            raise ValueError(
                "gpio_manager: no config.GPIO_EXPANDERS entry for unit %r "
                "(check the pin descriptor and config.py agree)" % (unit_id,)
            )
        unit_cfg = expanders[unit_id]
        driver = unit_cfg.get("driver", "mcp23017")
        factory = _BACKEND_FACTORIES.get(driver)
        if factory is None:
            raise ValueError(
                "gpio_manager: unknown expander driver %r for unit %r - "
                "register it in gpio_manager._BACKEND_FACTORIES" % (driver, unit_id)
            )
        _expander_cache[unit_id] = factory(unit_cfg)
    return _expander_cache[unit_id]


def _parse(descriptor):
    """Returns ('native', gpio_no) or ('mcp', unit_id, pin_no)."""
    s = str(descriptor)
    if s.startswith("mcp:"):
        _, unit_id, pin_no = s.split(":")
        return ("mcp", unit_id, int(pin_no))
    return ("native", int(s))


def is_expander_pin(descriptor):
    return _parse(descriptor)[0] != "native"


def supports_pwm(descriptor):
    """Only native ESP32 GPIOs have a PWM peripheral behind them - see mcp23017.py's docstring."""
    return _parse(descriptor)[0] == "native"


def get_digital_pin(descriptor, mode="out", pull=None):
    """
    Returns an object with a machine.Pin-shaped .value()/.on()/.off()
    API, regardless of whether `descriptor` names a native GPIO or an
    expander pin. `mode` is "in" or "out"; `pull` is None or "up".
    """
    kind = _parse(descriptor)

    if kind[0] == "native":
        from machine import Pin
        _, gpio_no = kind
        pin_mode = Pin.OUT if mode == "out" else Pin.IN
        if pull == "up":
            return Pin(gpio_no, pin_mode, Pin.PULL_UP)
        return Pin(gpio_no, pin_mode)

    _, unit_id, pin_no = kind
    import mcp23017
    expander = _get_expander(unit_id)
    exp_mode = mcp23017.OUT if mode == "out" else mcp23017.IN
    return expander.pin(pin_no, mode=exp_mode, pull=pull)


def get_pwm_pin(descriptor, freq_hz):
    """PWM is native-GPIO-only; raises a clear error rather than silently degrading an expander pin to on/off."""
    if not supports_pwm(descriptor):
        raise ValueError(
            "gpio_manager: pin %r is on a GPIO expander, which has no PWM "
            "peripheral behind it - use TYPE_TO_HARDWARE = 'relay' "
            "(on/off only) for this channel, or move it to a native GPIO "
            "if you need speed control" % (descriptor,)
        )
    from machine import Pin, PWM
    _, gpio_no = _parse(descriptor)
    return PWM(Pin(gpio_no), freq=freq_hz)


def registration_port(descriptor):
    """
    Stable integer for the backend's `port` field (its schema requires
    an int). Native pins use their real GPIO number, so every board
    that predates expander support keeps sending exactly the same
    value it always has. Expander pins get a synthetic number
    (9000 + unit*16 + pin) that can never collide with a real ESP32
    GPIO (0-39), so the two pin spaces never alias each other in the
    backend's data. Requires expander unit ids to be small integers
    given as strings (e.g. "0", "1"), matching config.GPIO_EXPANDERS.
    """
    kind = _parse(descriptor)
    if kind[0] == "native":
        return kind[1]
    _, unit_id, pin_no = kind
    return 9000 + int(unit_id) * 16 + pin_no
