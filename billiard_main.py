"""Dedicated entry point for the billiard ESP32; never replace hydro main.py."""

import time
import uasyncio as asyncio

import billiard_config as config
from billiard_api import BilliardApi
from billiard_controller import BilliardController


class BilliardWiFi:
    def __init__(self):
        import network
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)

    async def ensure_connected(self):
        if self.wlan.isconnected():
            return True
        self.wlan.connect(config.SSID, config.PASSWORD)
        for _ in range(30):
            if self.wlan.isconnected():
                return True
            await asyncio.sleep(1)
        return False


async def sync_once(controller, api):
    events = controller.storage.events(config.MAX_SYNC_BATCH)
    if events:
        controller.apply_result(api.sync(events))


async def run():
    wifi, api, controller = BilliardWiFi(), BilliardApi(), BilliardController()
    backoff_index = 0
    was_online = False
    while True:
        try:
            if await wifi.ensure_connected():
                # State is a boot/reconnect reconciliation operation, not a
                # high-frequency poll.  Heartbeats below maintain last_seen.
                if not was_online:
                    controller.reconcile(api.state())
                    was_online = True
                await sync_once(controller, api)
                if controller.heartbeat_due():
                    event = controller.heartbeat()
                    controller.apply_result(api.event(event))
                backoff_index = 0
                await asyncio.sleep(2)
                continue
            raise RuntimeError("WiFi unavailable")
        except Exception as error:
            was_online = False
            delay = config.RETRY_BACKOFF_S[min(backoff_index, len(config.RETRY_BACKOFF_S) - 1)]
            backoff_index += 1
            print("[billiard] offline/sync error:", error, "retry in", delay, "s")
            await asyncio.sleep(delay)


def main():
    asyncio.run(run())


main()
