"""
relay.py
--------
Low-level hardware abstraction for every actuator channel. Channels
are built straight from config.TYPE_TO_GPIO / config.TYPE_TO_HARDWARE
instead of a hand-maintained light/door list, so adding a new
actuator type is a config.py-only change.

Two hardware kinds (config.TYPE_TO_HARDWARE):
  - "relay"  : simple digital active-low channel (pump, fan, light,
               valve) - on/off only.
  - "mosfet" : PWM-capable channel (water_pump) - supports a 0-100%
               speed via config.PUMP_SPEED, in addition to plain
               on/off (speed 0 == off, any speed > 0 == on).

Every channel starts OFF at boot (RelayChannel.__init__), and
config.ACTUATOR_STATES / config.PUMP_SPEED are kept in sync with the
actual GPIO output on every change, so anything reading those dicts
(oled_display.py, control.py's state push) never lies about what's
physically energized.

Safety watchdog: config.py doesn't define a max-on-time ceiling for
this deployment, so a sane default is used unless one is added
(config.DEFAULT_MAX_ON_TIME_S) - protects against a stuck command or
dropped connection leaving a pump/valve/light energized indefinitely.
"""

import time
from machine import Pin, PWM

import config

PWM_FREQ_HZ = 1000
DEFAULT_MAX_ON_TIME_S = getattr(config, "DEFAULT_MAX_ON_TIME_S", 12 * 60 * 60)  # 12h


class RelayChannel:
    """A single actuator output, driven either as a plain relay or PWM."""

    def __init__(self, actuator_type, pin_no, hardware="relay",
                 max_on_time_s=None):
        self.actuator_type = actuator_type
        self.pin_key = str(pin_no)
        self.hardware = hardware
        self.active_low = config.RELAY_ACTIVE_LOW if hasattr(config, "RELAY_ACTIVE_LOW") else True
        self.max_on_time_s = DEFAULT_MAX_ON_TIME_S if max_on_time_s is None else max_on_time_s

        if hardware == "mosfet":
            self._pin = PWM(Pin(int(pin_no)), freq=PWM_FREQ_HZ)
        else:
            self._pin = Pin(int(pin_no), Pin.OUT)

        self._on = False
        self._speed = 0
        self._on_since = None
        self.set(False)  # start safe: everything OFF at boot

    # ---- low level ----
    def _write_digital(self, energize):
        if self.hardware == "mosfet":
            self._pin.duty(0 if not energize else 1023)
        else:
            level = 0 if (self.active_low and energize) else (
                1 if self.active_low else int(energize)
            )
            self._pin.value(level)

    def _write_pwm_speed(self, speed_pct):
        duty = int((speed_pct / 100) * 1023)
        if self.active_low:
            duty = 1023 - duty
        self._pin.duty(duty)

    # ---- public API ----
    def set(self, energize):
        self._write_digital(energize)
        self._on = energize
        self._speed = 100 if energize else 0
        self._on_since = time.time() if energize else None
        self._sync_state()

    def set_speed(self, speed_pct):
        """Only meaningful for hardware == 'mosfet'; falls back to on/off."""
        speed_pct = max(0, min(100, speed_pct))
        if self.hardware == "mosfet":
            self._write_pwm_speed(speed_pct)
        else:
            self._write_digital(speed_pct > 0)
        self._speed = speed_pct
        self._on = speed_pct > 0
        self._on_since = time.time() if self._on else None
        self._sync_state()

    def on(self):
        self.set(True)

    def off(self):
        self.set(False)

    def toggle(self):
        self.set(not self._on)

    def is_on(self):
        return self._on

    def speed(self):
        return self._speed

    def seconds_on(self):
        if not self._on or self._on_since is None:
            return 0
        return time.time() - self._on_since

    def exceeded_max_on_time(self):
        return self._on and self.seconds_on() > self.max_on_time_s

    def _sync_state(self):
        config.ACTUATOR_STATES[self.pin_key] = 1 if self._on else 0
        if self.pin_key in config.PUMP_SPEED:
            config.PUMP_SPEED[self.pin_key] = self._speed


class RelayManager:
    """Owns all actuator channels, built from config.TYPE_TO_GPIO."""

    def __init__(self):
        self.channels = {}  # actuator_type -> RelayChannel

        for actuator_type, pin_no in config.TYPE_TO_GPIO.items():
            hardware = config.TYPE_TO_HARDWARE.get(actuator_type, "relay")
            self.channels[actuator_type] = RelayChannel(
                actuator_type=actuator_type, pin_no=pin_no, hardware=hardware
            )

    def get(self, actuator_type):
        return self.channels.get(actuator_type)

    def all_off(self):
        for ch in self.channels.values():
            ch.off()

    # ---- safety sweep ----
    def safety_sweep(self):
        """
        Call periodically from the main loop. Force-off any channel
        that has exceeded its allowed on-time. Returns a list of
        actuator types that were force-stopped, for logging/telemetry.
        """
        tripped = []
        for actuator_type, ch in self.channels.items():
            if ch.exceeded_max_on_time():
                ch.off()
                tripped.append(actuator_type)
        return tripped

    def state_snapshot(self):
        return {t: ch.is_on() for t, ch in self.channels.items()}

    def speed_snapshot(self):
        return {t: ch.speed() for t, ch in self.channels.items() if ch.hardware == "mosfet"}