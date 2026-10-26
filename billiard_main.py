"""Dedicated entry point for the billiard ESP32; never replace hydro main.py.

MicroPython only auto-runs ``main.py``.  On a billiard board, copy
``main_billiard.py`` to the device AS ``main.py`` (it just imports this file).

Optional RS485/display adapter: if a ``billiard_adapter`` module exists and
exposes ``async def run(controller)``, it is started alongside the sync loop
and shares the same BilliardController (so its button events are queued and
synced by this file).
"""

import gc
import time
import uasyncio as asyncio

import billiard_config as config
from billiard_api import BilliardApi, ApiError
from billiard_clock import Clock
from billiard_controller import BilliardController


class BilliardWiFi:
    def __init__(self):
        import network
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)

    async def ensure_connected(self):
        if self.wlan.isconnected():
            return True
        try:
            self.wlan.connect(config.SSID, config.PASSWORD)
        except OSError as e:
            print("[billiard] wifi connect() raised:", e)
            self.wlan.active(False)
            await asyncio.sleep_ms(300)
            self.wlan.active(True)
            return False
        for _ in range(30):
            if self.wlan.isconnected():
                return True
            await asyncio.sleep(1)
        return False

    def rssi(self):
        try:
            return self.wlan.status("rssi")
        except Exception:
            return None


class Syncer:
    """Keeps the persistent queue, the server and the clock in step."""

    def __init__(self, wifi, api, controller, clock):
        self.wifi, self.api, self.controller, self.clock = wifi, api, controller, clock
        self.online = False
        self.offline_since = None          # time.time() when the link was lost
        self._last_alert = 0
        self._boot_ms = time.ticks_ms()
        self._next_ntp_ms = time.ticks_ms()
        self._next_diag_ms = time.ticks_ms()
        self._next_stall_ms = time.ticks_ms()

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _due(deadline_ms):
        return time.ticks_diff(time.ticks_ms(), deadline_ms) >= 0

    def _diagnostics(self):
        data = {
            "uptime": time.ticks_diff(time.ticks_ms(), self._boot_ms) // 1000,
            "free_memory": gc.mem_free(),
        }
        rssi = self.wifi.rssi()
        if rssi is not None:
            data["wifi_rssi"] = rssi
        return data

    def _sync_ntp(self):
        ok = self.clock.sync_ntp()
        wait = config.NTP_RESYNC_S if (ok or self.clock.synced) else config.NTP_RETRY_S
        self._next_ntp_ms = time.ticks_add(time.ticks_ms(), wait * 1000)

    # ---- delivery ------------------------------------------------------
    async def flush(self):
        """Send queued events in small batches (RAM!) until nothing is pending."""
        c = self.controller
        for _ in range(config.MAX_BATCHES_PER_PASS):
            events = c.pending_events(config.MAX_SYNC_BATCH)
            if not events:
                return
            if len(events) == 1:
                result = self.api.event(events[0])      # live path
            else:
                result = self.api.sync(events)           # replay path
            new_errors = c.apply_result(result, events)
            if new_errors:
                print("[billiard] %d event(s) rejected/conflicted, see %s"
                      % (new_errors, config.ERROR_FILE))
            del events, result
            gc.collect()
            await asyncio.sleep_ms(0)

    # ---- connection lifecycle -------------------------------------------
    async def on_connect(self):
        """Order matters (see billiard_controller.reconcile): clock, flush the
        backlog, THEN fetch server state and reconcile against it."""
        c = self.controller
        self._sync_ntp()
        c.reset_delivery()
        await self.flush()
        state = self.api.state()
        c.reconcile(state)
        c.fill_gaps(state.get("server_state_version", 0))
        c.reset_delivery()
        c.heartbeat(self._diagnostics())
        self._next_diag_ms = time.ticks_add(time.ticks_ms(), config.HEARTBEAT_EVENT_S * 1000)
        self._next_stall_ms = time.ticks_add(time.ticks_ms(), config.STALL_RETRY_S * 1000)
        self.online = True
        self.offline_since = None
        c.online, c.sync_error, c.alert = True, False, None
        print("[billiard] online, queue unacked:", c.has_unacked())

    async def step(self):
        c = self.controller
        await self.flush()

        # Un-acked events (a rejected one blocks the ack cursor): re-send them
        # now and then so a fix made in the POS lets them through.
        if c.has_unacked() and self._due(self._next_stall_ms):
            self._next_stall_ms = time.ticks_add(time.ticks_ms(), config.STALL_RETRY_S * 1000)
            c.reset_delivery()
            await self.flush()

        if self._due(self._next_diag_ms):
            self._next_diag_ms = time.ticks_add(time.ticks_ms(), config.HEARTBEAT_EVENT_S * 1000)
            c.heartbeat(self._diagnostics())

        # Any authenticated request keeps the backend's last_seen fresh.
        last = self.api.last_ok_ms
        if last is None or time.ticks_diff(time.ticks_ms(), last) >= config.KEEPALIVE_S * 1000:
            c.observe_server_time(self.api.state())

        if self._due(self._next_ntp_ms):
            self._sync_ntp()

    def go_offline(self, error):
        c = self.controller
        self.online = False
        c.online = False
        c.sync_error = isinstance(error, ApiError) and error.status in (401, 403)
        if self.offline_since is None:
            self.offline_since = time.time()
        offline_for = max(0, time.time() - self.offline_since)
        if offline_for >= config.OFFLINE_ALERT_S:
            c.alert = "OFFLINE %dh" % (offline_for // 3600)
            if time.time() - self._last_alert >= 3600:
                self._last_alert = time.time()
                print("[billiard] WARNING: offline %dh - backend rejects events older "
                      "than 48h, restore connectivity" % (offline_for // 3600))


def _load_adapter():
    try:
        import billiard_adapter
        return billiard_adapter
    except ImportError:
        return None


async def sync_loop(controller, clock):
    wifi, api = BilliardWiFi(), BilliardApi()
    syncer = Syncer(wifi, api, controller, clock)
    attempt = 0
    while True:
        try:
            if not await wifi.ensure_connected():
                raise RuntimeError("WiFi unavailable")
            if not syncer.online:
                await syncer.on_connect()
            await syncer.step()
            attempt = 0
            await asyncio.sleep_ms(config.LOOP_MS)
        except Exception as error:
            syncer.go_offline(error)
            if controller.sync_error:
                # 401/403: wrong key or device_id - never retry in a tight loop.
                delay = config.AUTH_RETRY_S
                print("[billiard] SYNC ERROR (auth):", error)
            else:
                delay = config.RETRY_BACKOFF_S[min(attempt, len(config.RETRY_BACKOFF_S) - 1)]
                print("[billiard] offline/sync error:", error, "retry in", delay, "s")
            attempt += 1
            await asyncio.sleep(delay)


async def run():
    clock = Clock()
    controller = BilliardController(clock=clock)
    tasks = [asyncio.create_task(sync_loop(controller, clock))]
    adapter = _load_adapter()
    if adapter is not None:
        tasks.append(asyncio.create_task(adapter.run(controller)))
    await asyncio.gather(*tasks)


def main():
    asyncio.run(run())


main()
