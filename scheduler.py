"""
scheduler.py
------------
Offline-capable schedule executor for the hydro controller.

Design
  * The backend's growing_batch (stages -> recipes) is compacted into a
    small JSON file in flash every time a /hydro/status poll succeeds.
  * Evaluation is LEVEL-based and stateless: every second we compute
    "what should each scheduled channel be right now?" from the wall
    clock and the schedule. No timers/edges to lose, so reboots, NTP
    jumps and missed ticks can't desynchronise it.
  * Online  (backend polled OK within OFFLINE_AFTER_S): the backend
    drives actuators via control.py exactly as before; this module only
    caches the schedule.
  * Offline: control.py is not polled (no blocking HTTP) and this
    module drives the channels from the cached schedule.
  * All network work done here (WiFi reconnect, backend probe) is
    uasyncio-friendly so it never delays schedule execution.

The sliding door is NEVER driven by the scheduler.
"""

import gc
import json
import os
import time
import uasyncio as asyncio

import config

SCHEDULE_FILE = getattr(config, "SCHEDULE_FILE", "schedule.json")
TZ_OFFSET_S = getattr(config, "TZ_OFFSET_S", 7 * 3600)        # Vietnam = UTC+7
OFFLINE_AFTER_S = getattr(config, "OFFLINE_AFTER_S", 3 * config.SEND_INTERVAL)
PROBE_TIMEOUT_S = getattr(config, "PROBE_TIMEOUT_S", 3)
NTP_RESYNC_S = getattr(config, "NTP_RESYNC_S", 6 * 3600)
NTP_RETRY_S = 300
MIN_VALID_YEAR = 2024
WIFI_BACKOFF_S = getattr(config, "WIFI_RETRY_BACKOFF_S", (2, 5, 10, 20, 30))


# ---------------------------------------------------------------------------
# Pure helpers (no hardware) - easy to unit test on a PC with CPython
# ---------------------------------------------------------------------------
def _hms(s):
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 else 0)


def compact_from_status(entry):
    """
    /hydro/status device entry -> compact schedule dict, or None if the
    entry has no growing_batch key at all (don't wipe a good cache on a
    malformed response). Recipes become [type, action, start_s, end_s,
    on_min, off_min].
    """
    if "growing_batch" not in entry:
        return None
    gb = entry.get("growing_batch") or {}
    start = gb.get("start_date")
    stages = []
    if start and gb.get("status", "growing") == "growing":
        for st in gb.get("stages") or []:
            recs = []
            for r in st.get("recipes") or []:
                try:
                    recs.append([
                        r["actuator_type"], r.get("action", "on"),
                        _hms(r["start_time"]), _hms(r["end_time"]),
                        r.get("interval_on_min") or 0,
                        r.get("interval_off_min") or 0,
                    ])
                except Exception:
                    continue
            stages.append({"ds": st.get("day_start", 0),
                           "de": st.get("day_end", 99999), "r": recs})
    return {"v": 1, "start": start, "stages": stages}


def desired_states(sched, local_s):
    """
    {actuator_type: bool} for every type mentioned anywhere in the
    schedule. local_s = time.time() + TZ offset (device epoch).
    Types outside every active window resolve to False.
    """
    if not sched or not sched.get("start"):
        return {}
    t = time.localtime(local_s)
    sod = t[3] * 3600 + t[4] * 60 + t[5]
    y, m, d = [int(x) for x in sched["start"][:10].split("-")]
    day = (local_s - time.mktime((y, m, d, 0, 0, 0, 0, 0))) // 86400

    out = {}
    forced_off = set()
    for st in sched["stages"]:                      # managed set = all stages
        for rec in st["r"]:
            out[rec[0]] = False
    for st in sched["stages"]:
        if not (st["ds"] <= day < st["de"]):
            continue
        for typ, act, s0, s1, on_m, off_m in st["r"]:
            if s0 <= s1:
                inside = s0 <= sod < s1
                elapsed = sod - s0
            else:                                   # window wraps midnight
                inside = sod >= s0 or sod < s1
                elapsed = sod - s0 if sod >= s0 else sod + 86400 - s0
            if not inside:
                continue
            if on_m and off_m:                      # interval mode inside window
                inside = elapsed % ((on_m + off_m) * 60) < on_m * 60
            if not inside:
                continue
            if act == "off":
                forced_off.add(typ)
            else:
                out[typ] = True
    for typ in forced_off:
        out[typ] = False
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
                self.sched = json.load(f)
            self._saved_hash = hash(json.dumps(self.sched))
            print("[sched] loaded cached schedule,", len(self.sched.get("stages", [])), "stage(s)")
        except Exception as e:           # missing or corrupt -> run with none
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

    # ---- hooks called from control.py -------------------------------
    def on_status(self, entry):
        """control.poll_status() calls this with our device's entry."""
        self._ok_ms = time.ticks_ms()
        sched = compact_from_status(entry)
        if sched is not None:
            self.set_schedule(sched)

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