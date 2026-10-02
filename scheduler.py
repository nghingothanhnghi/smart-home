"""
scheduler.py
------------
Offline-capable schedule executor for the hydro controller.

Design
  * GET /hydro/config?device_id=<DEVICE_CODE> (small, per-device) returns
    each actuator's `schedules` [{start,end,days,on_min,off_min}].
    A background task fetches it whenever the backend is reachable and
    caches a compact form in flash (schedule.json).
  * Backend rows are matched to local channels by pin/port (same as
    actuators.resolve_actuator_type) - NOT by the generic 'type'/'name'.
  * Evaluation is LEVEL-based and stateless: every second we compute
    "should this channel be on right now?" from the wall clock. Reboots,
    missed ticks and NTP jumps can't desynchronise it.
  * Online  (control.py polled /hydro/status OK within OFFLINE_AFTER_S):
    backend drives actuators as before.
  * Offline: control.py isn't polled; this module drives every channel
    that has a cached schedule. Channels with no schedule are left alone
    (the 12h safety sweep still applies). Manual overrides can't arrive
    while offline.
  * The sliding door is NEVER driven by the scheduler.
"""

import gc
import json
import os
import time
import ujson
import urequests
import uasyncio as asyncio

import config
import auth

SCHEDULE_FILE = getattr(config, "SCHEDULE_FILE", "schedule.json")
CONFIG_URL = getattr(config, "CONFIG_URL", config.FASTAPI_URL + "/hydro/config")
TZ_OFFSET_S = getattr(config, "TZ_OFFSET_S", 7 * 3600)        # Vietnam = UTC+7
OFFLINE_AFTER_S = getattr(config, "OFFLINE_AFTER_S", 3 * config.SEND_INTERVAL)
SYNC_INTERVAL_S = getattr(config, "SCHEDULE_SYNC_S", 60)
HTTP_TIMEOUT_S = getattr(config, "HTTP_TIMEOUT_S", 8)
PROBE_TIMEOUT_S = getattr(config, "PROBE_TIMEOUT_S", 3)
NTP_RESYNC_S = getattr(config, "NTP_RESYNC_S", 6 * 3600)
NTP_RETRY_S = 300
MIN_VALID_YEAR = 2024
WIFI_BACKOFF_S = getattr(config, "WIFI_RETRY_BACKOFF_S", (2, 5, 10, 20, 30))
SCHEMA = 2

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ---------------------------------------------------------------------------
# Pure helpers (no hardware) - easy to unit test on a PC with CPython
# ---------------------------------------------------------------------------
def _hm(s):
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 else 0)


def _daymask(s):
    """'mon,tue' -> bitmask (bit0 = Monday). Empty/None = every day."""
    if not s:
        return 127
    m = 0
    for d in s.split(","):
        i = _DAYS.get(d.strip().lower()[:3])
        if i is not None:
            m |= 1 << i
    return m


def compact_from_config(data, resolve):
    """
    /hydro/config response -> {"v":2,"a":{local_type: [[s0,s1,mask,on_m,off_m],...]}}
    `resolve(row)` maps a backend actuator row to a local actuator type
    (or None). Only actuators WITH schedules are kept. Returns None if
    the response is malformed so a good cache is never wiped by junk.
    """
    acts = data.get("actuators") if isinstance(data, dict) else None
    if not isinstance(acts, list):
        return None
    out = {}
    for a in acts:
        scheds = a.get("schedules") or []
        if not scheds:
            continue
        typ = resolve(a)
        if typ is None:
            print("[sched] backend actuator id=%s pin=%s not on this board, skipped"
                  % (a.get("id"), a.get("pin")))
            continue
        lst = out.setdefault(typ, [])
        for s in scheds:
            try:
                lst.append([_hm(s["start"]), _hm(s["end"]), _daymask(s.get("days")),
                            s.get("on_min") or 0, s.get("off_min") or 0])
            except Exception:
                continue
    return {"v": SCHEMA, "a": out}


def desired_states(sched, local_s):
    """{local_type: bool} for every type that has a schedule. local_s = time.time()+TZ."""
    if not sched or sched.get("v") != SCHEMA:
        return {}
    t = time.localtime(local_s)
    sod = t[3] * 3600 + t[4] * 60 + t[5]
    wd = t[6]                    # 0 = Monday
    prev = (wd - 1) % 7
    out = {}
    for typ, wins in sched["a"].items():
        on = False
        for s0, s1, mask, on_m, off_m in wins:
            if s0 <= s1:
                if not (s0 <= sod < s1 and (mask >> wd) & 1):
                    continue
                elapsed = sod - s0
            elif sod >= s0:                         # wraps midnight, evening part
                if not (mask >> wd) & 1:
                    continue
                elapsed = sod - s0
            elif sod < s1:                          # wraps midnight, morning part
                if not (mask >> prev) & 1:          # belongs to YESTERDAY's entry
                    continue
                elapsed = sod + 86400 - s0
            else:
                continue
            if on_m and off_m and elapsed % ((on_m + off_m) * 60) >= on_m * 60:
                continue                            # in the "off" half of an interval
            on = True
            break
        out[typ] = on
    return out


async def _tcp_ok(host, port):
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port),
                                      PROBE_TIMEOUT_S)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _host_port(url):
    hp = url.split("://", 1)[-1].split("/", 1)[0]
    if ":" in hp:
        h, p = hp.rsplit(":", 1)
        return h, int(p)
    return hp, 443 if url.startswith("https") else 80


# ---------------------------------------------------------------------------
class Scheduler:
    def __init__(self, actuators):
        self.actuators = actuators
        self.sched = None
        self.reachable = False       # set by probe_task
        self._ok_ms = None           # ticks_ms of last good /hydro/status
        self._saved_hash = None
        self._latched = set()        # channels tripped by the safety sweep
        self._was_offline = None
        self._warned = set()
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self):
        try:
            with open(SCHEDULE_FILE) as f:
                s = json.load(f)
            if s.get("v") != SCHEMA:
                raise ValueError("old schema")
            self.sched = s
            self._saved_hash = hash(json.dumps(s))
            print("[sched] loaded cached schedule for:", list(s["a"].keys()))
        except Exception as e:           # missing/corrupt/old -> run with none
            print("[sched] no usable cached schedule:", e)
            self.sched = None

    def _save(self, sched):
        s = json.dumps(sched)
        h = hash(s)
        if h == self._saved_hash:
            return                       # unchanged: don't wear flash
        tmp = SCHEDULE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(s)
            try:
                os.rename(tmp, SCHEDULE_FILE)      # atomic on littlefs
            except OSError:
                os.remove(SCHEDULE_FILE)           # FAT can't rename over
                os.rename(tmp, SCHEDULE_FILE)
            self._saved_hash = h
            print("[sched] schedule saved to flash (%d bytes)" % len(s))
        except Exception as e:
            print("[sched] save failed:", e)

    def set_schedule(self, sched):
        self.sched = sched
        self._save(sched)

    # ---- backend sync ------------------------------------------------
    def _fetch_config(self):
        """Blocking GET (small payload). Returns parsed dict or None."""
        url = CONFIG_URL + "?device_id=" + auth._url_escape(config.DEVICE_CODE)
        try:
            resp = urequests.get(url, headers=auth.build_headers(), timeout=HTTP_TIMEOUT_S)
            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return None
                resp = urequests.get(url, headers=auth.build_headers(), timeout=HTTP_TIMEOUT_S)
            if resp.status_code != 200:
                print("[sched] config fetch status", resp.status_code)
                resp.close()
                return None
            data = resp.json()
            resp.close()
            return data
        except Exception as e:
            print("[sched] config fetch failed:", e)
            return None

    def sync_once(self):
        data = self._fetch_config()
        if data is None:
            return False
        sched = compact_from_config(data, self.actuators.resolve_actuator_type)
        del data
        gc.collect()
        if sched is None:
            print("[sched] config response malformed, keeping cached schedule")
            return False
        self.set_schedule(sched)
        return True

    # ---- hooks -------------------------------------------------------
    def on_status(self, entry):
        """control.poll_status() calls this: backend is alive and commanding."""
        self._ok_ms = time.ticks_ms()

    def latch(self, tripped):
        for t in tripped or ():
            self._latched.add(t)

    @property
    def online(self):
        return (self._ok_ms is not None and
                time.ticks_diff(time.ticks_ms(), self._ok_ms) < OFFLINE_AFTER_S * 1000)

    @property
    def offline(self):
        return not self.online

    @staticmethod
    def time_valid():
        return time.localtime()[0] >= MIN_VALID_YEAR

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            print("[sched]", msg)

    # ---- execution ---------------------------------------------------
    def apply(self):
        off = self.offline
        if off != self._was_offline:
            self._was_offline = off
            print("[sched] mode ->", "OFFLINE (schedule drives)" if off else "ONLINE (backend drives)")
        if not off or self.sched is None:
            return
        if not self.time_valid():
            self._warn_once("time", "clock not set yet (no NTP) - holding, not executing")
            return
        self._warned.discard("time")

        want = desired_states(self.sched, time.time() + TZ_OFFSET_S)
        for typ, on in want.items():
            ch = self.actuators.relays.get(typ)
            if ch is None:
                self._warn_once(typ, "schedule type '%s' has no local channel, skipped" % typ)
                continue
            if ch.hardware == "door":
                continue                                  # never automate the door
            if typ in self._latched:                      # safety sweep tripped it
                if not on:
                    self._latched.discard(typ)            # window ended: re-arm
                continue
            ok, msg = self.actuators.execute(
                {"actuator_id": typ, "action": "on" if on else "off"})
            if msg is not None:
                print("[sched]", msg)

    # ---- async tasks (run these under uasyncio) ----------------------
    async def run(self, wdt=None):
        while True:
            if wdt:
                wdt.feed()
            self.apply()
            await asyncio.sleep(1)

    async def sync_task(self, wifi):
        """Refresh the cached schedule while the backend is reachable."""
        while True:
            if wifi.is_connected() and self.reachable:
                ok = self.sync_once()      # short blocking call, only when probe passed
                await asyncio.sleep(SYNC_INTERVAL_S if ok else 15)
            else:
                await asyncio.sleep(5)

    async def wifi_task(self, wifi):
        idx = 0
        while True:
            if wifi.is_connected():
                idx = 0
                await asyncio.sleep(2)
                continue
            if not await wifi.connect_async():
                d = WIFI_BACKOFF_S[min(idx, len(WIFI_BACKOFF_S) - 1)]
                idx += 1
                print("[wifi] retrying in %ss" % d)
                await asyncio.sleep(d)

    async def probe_task(self, wifi):
        host, port = _host_port(config.FASTAPI_URL)
        while True:
            self.reachable = wifi.is_connected() and await _tcp_ok(host, port)
            await asyncio.sleep(10 if self.reachable else 5)

    async def ntp_task(self, wifi):
        while True:
            ok = False
            if wifi.is_connected():
                try:
                    import ntptime
                    ntptime.settime()        # blocking, ~1s timeout
                    ok = True
                    print("[sched] NTP synced")
                except Exception as e:
                    print("[sched] NTP failed:", e)
            await asyncio.sleep(NTP_RESYNC_S if ok else NTP_RETRY_S)
            gc.collect()