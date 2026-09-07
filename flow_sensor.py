"""
flow_sensor.py
--------------
Pulse-counting flow sensor support (YF-S201/YF-B1 style) for actuator
types listed in config.FLOW_SENSOR_PINS. Each sensor's pulse output
must be on a NATIVE ESP32 GPIO — MCP23017 expander pins in this
firmware are polled, not interrupt-capable, so init() refuses an
"mcp:" descriptor rather than silently never counting pulses.

Flow rate (L/min) = (pulses_since_last_read / pulses_per_liter)
                    / (seconds_since_last_read / 60)
"""

from machine import Pin
import time
import config

_counters = {}          # actuator_type -> pulse count since last read()
_pins = {}               # actuator_type -> Pin (kept alive so IRQ isn't GC'd)
_last_read_time = {}

FLOW_SENSOR_PINS = getattr(config, "FLOW_SENSOR_PINS", {})
FLOW_CALIBRATION = getattr(config, "FLOW_CALIBRATION", {})
FLOW_SENSOR_ENABLED = getattr(config, "FLOW_SENSOR_ENABLED", False)


def _make_irq(actuator_type):
    def _irq(pin):
        _counters[actuator_type] = _counters.get(actuator_type, 0) + 1
    return _irq


def init():
    """Call once at boot (from main.py), before the control loop starts."""
    if not FLOW_SENSOR_ENABLED:
        return

    now = time.time()
    for actuator_type, descriptor in FLOW_SENSOR_PINS.items():
        if str(descriptor).startswith("mcp:"):
            print("[flow_sensor] '%s' is on expander pin %s — flow sensors "
                  "need a native interrupt-capable GPIO, skipping" %
                  (actuator_type, descriptor))
            continue

        pin = Pin(int(descriptor), Pin.IN, Pin.PULL_UP)
        pin.irq(trigger=Pin.IRQ_RISING, handler=_make_irq(actuator_type))

        _pins[actuator_type] = pin
        _counters[actuator_type] = 0
        _last_read_time[actuator_type] = now
        print("[flow_sensor] '%s' armed on pin %s" % (actuator_type, descriptor))


def read_all():
    """
    Returns {actuator_type: flow_rate_l_per_min}. Resets each counter
    after reading, so this reports the rate since the PREVIOUS call —
    call it on a steady interval (control.py's SEND_INTERVAL).
    """
    if not FLOW_SENSOR_ENABLED:
        return {}

    now = time.time()
    results = {}

    for actuator_type in _pins:
        pulses = _counters.get(actuator_type, 0)
        _counters[actuator_type] = 0

        elapsed = now - _last_read_time.get(actuator_type, now)
        _last_read_time[actuator_type] = now
        if elapsed <= 0:
            continue

        pulses_per_liter = FLOW_CALIBRATION.get(actuator_type, 450)
        liters = pulses / pulses_per_liter
        flow_rate = liters / (elapsed / 60)

        results[actuator_type] = round(flow_rate, 3)

    return results