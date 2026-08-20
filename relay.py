"""
relay.py
--------
Low-level hardware abstraction for every relay channel driving a 220V
load (6 lights + 2 door-direction channels). This is the ONLY module
that touches machine.Pin for actuation, which keeps GPIO handling and
safety logic in one auditable place.

Safety features:
- Active-low support (most opto-isolated relay boards energize on LOW).
- Per-channel max-on-time watchdog: a channel that's been ON longer
  than allowed is force-switched OFF on the next sweep(), regardless
  of what the backend or a dropped connection thinks the state is.
- Door interlock: OPEN and CLOSE channels can never be energized at
  the same time, enforced at this layer so no upstream bug can ever
  short the motor windings against each other.
"""

import time
from machine import Pin

import config


class RelayChannel:
    """A single relay output (one light, or one door direction)."""

    def __init__(self, name, pin_no, active_low=None, max_on_time_s=None,
                 kind="light"):
        self.name = name
        self.kind = kind  # "light" | "door_open" | "door_close"
        self.active_low = config.RELAY_ACTIVE_LOW if active_low is None else active_low
        self.max_on_time_s = (
            config.DEFAULT_MAX_ON_TIME_S if max_on_time_s is None else max_on_time_s
        )

        self._pin = Pin(pin_no, Pin.OUT)
        self._on = False
        self._on_since = None
        self.set(False)  # start safe: everything OFF at boot

    # ---- low level ----
    def _write(self, energize):
        level = 0 if (self.active_low and energize) else (
            1 if (self.active_low and not energize) else (1 if energize else 0)
        )
        self._pin.value(level)

    # ---- public API ----
    def set(self, energize):
        self._write(energize)
        self._on = energize
        self._on_since = time.time() if energize else None

    def on(self):
        self.set(True)

    def off(self):
        self.set(False)

    def toggle(self):
        self.set(not self._on)

    def is_on(self):
        return self._on

    def seconds_on(self):
        if not self._on or self._on_since is None:
            return 0
        return time.time() - self._on_since

    def exceeded_max_on_time(self):
        return self._on and self.seconds_on() > self.max_on_time_s


class RelayManager:
    """
    Owns all relay channels, builds them from config, and provides the
    safety sweep + door interlock used by actuators.py.
    """

    def __init__(self):
        self.channels = {}  # name -> RelayChannel

        for name, pin_no in config.LIGHT_PINS.items():
            self.channels[name] = RelayChannel(
                name=name, pin_no=pin_no, kind="light"
            )

        self.channels["door_open"] = RelayChannel(
            name="door_open",
            pin_no=config.DOOR_OPEN_PIN,
            kind="door_open",
            max_on_time_s=config.DOOR_MAX_RUN_S,
        )
        self.channels["door_close"] = RelayChannel(
            name="door_close",
            pin_no=config.DOOR_CLOSE_PIN,
            kind="door_close",
            max_on_time_s=config.DOOR_MAX_RUN_S,
        )

    def get(self, name):
        return self.channels.get(name)

    # ---- door interlock ----
    def energize_door(self, direction):
        """
        direction: 'open' or 'close'. Guarantees the opposite channel
        is off first, so the two can never be energized together.
        """
        assert direction in ("open", "close")
        other = "door_close" if direction == "open" else "door_open"
        target = "door_open" if direction == "open" else "door_close"

        self.channels[other].off()
        self.channels[target].on()

    def stop_door(self):
        self.channels["door_open"].off()
        self.channels["door_close"].off()

    # ---- safety sweep ----
    def safety_sweep(self):
        """
        Call periodically from the main loop. Force-off any channel
        that has exceeded its allowed on-time. Returns a list of
        channel names that were force-stopped, for logging/telemetry.
        """
        tripped = []
        for name, ch in self.channels.items():
            if ch.exceeded_max_on_time():
                ch.off()
                tripped.append(name)
        return tripped

    def all_off(self):
        for ch in self.channels.values():
            ch.off()

    def state_snapshot(self):
        return {name: ch.is_on() for name, ch in self.channels.items()}
