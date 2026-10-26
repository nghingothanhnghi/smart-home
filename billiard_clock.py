"""UTC clock for the billiard controller: NTP first, server_time as fallback.

``synced`` lives in RAM only on purpose.  After a power cycle the ESP32 RTC
is back at 2000-01-01, so the flag must start False and only become True
once NTP (or the backend's ``server_time``) has actually set the clock.
Events stamped while ``synced`` is False are sent with clock_synced=false
and the backend ignores their timestamp (guide section 6).
"""

import time

import billiard_config as config

MIN_VALID_YEAR = 2024


def _valid():
    return time.gmtime()[0] >= MIN_VALID_YEAR


def parse_utc(text):
    """'2026-10-25T15:40:00Z' -> (y, m, d, h, mi, s), or None if not plain UTC."""
    try:
        text = str(text)
        if not (text.endswith("Z") or text.endswith("+00:00")):
            return None
        core = text[:19]
        if core[4] != "-" or core[7] != "-" or core[10] not in "T " \
                or core[13] != ":" or core[16] != ":":
            return None
        parts = (int(core[0:4]), int(core[5:7]), int(core[8:10]),
                 int(core[11:13]), int(core[14:16]), int(core[17:19]))
        if parts[0] < MIN_VALID_YEAR or not (1 <= parts[1] <= 12) \
                or not (1 <= parts[2] <= 31) or parts[3] > 23 \
                or parts[4] > 59 or parts[5] > 59:
            return None
        return parts
    except Exception:
        return None


class Clock:
    def __init__(self):
        self.synced = False

    def now_iso(self):
        t = time.gmtime()
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % t[:6]

    def sync_ntp(self):
        """Blocking (~1 s timeout). Returns True when the RTC was set."""
        try:
            import ntptime
            ntptime.host = config.NTP_HOST
            ntptime.settime()
        except Exception as e:
            print("[clock] NTP failed:", e)
            return False
        self.synced = _valid()
        if self.synced:
            print("[clock] NTP synced", self.now_iso())
        return self.synced

    def set_from_server(self, server_time):
        """Fallback when NTP is unreachable: adopt GET /state's server_time."""
        if self.synced:
            return False
        parts = parse_utc(server_time)
        if parts is None:
            return False
        try:
            import machine
            y, mo, d, h, mi, s = parts
            machine.RTC().datetime((y, mo, d, 0, h, mi, s, 0))
        except Exception as e:
            print("[clock] could not set RTC from server_time:", e)
            return False
        self.synced = _valid()
        if self.synced:
            print("[clock] set from server_time", self.now_iso())
        return self.synced

    def invalidate(self):
        self.synced = False
