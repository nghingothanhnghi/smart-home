"""
wifi.py
-------
Owns the WLAN station interface: connecting, reporting status, and
reconnecting with escalating backoff if the link drops. Every other
module treats WiFi as "up or down" through this class rather than
touching the `network` module directly.
"""

import network
import time

import config


class WiFiManager:
    def __init__(self):
        self._wlan = network.WLAN(network.STA_IF)
        self._wlan.active(True)
        self._backoff_index = 0

    def is_connected(self):
        return self._wlan.isconnected()

    def ip(self):
        if self.is_connected():
            return self._wlan.ifconfig()[0]
        return None

    def connect(self):
        """Blocking connect attempt, bounded by WIFI_CONNECT_TIMEOUT_S."""
        if self.is_connected():
            return True

        print("[wifi] connecting to '%s'..." % config.WIFI_SSID)
        self._wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

        start = time.time()
        while not self._wlan.isconnected():
            if time.time() - start > config.WIFI_CONNECT_TIMEOUT_S:
                print("[wifi] connect timed out")
                return False
            time.sleep(0.5)

        print("[wifi] connected, ip =", self._wlan.ifconfig()[0])
        self._backoff_index = 0
        return True

    def ensure_connected(self):
        """
        Call this frequently from the main loop. Non-blocking-ish:
        only attempts a (bounded) reconnect if the link is actually down,
        and backs off between attempts so a persistent outage doesn't
        spam reconnects or starve the rest of the loop.
        """
        if self.is_connected():
            return True

        ok = self.connect()
        if not ok:
            delay = self._next_backoff()
            print("[wifi] retrying in %ss" % delay)
            time.sleep(delay)
        return ok

    def _next_backoff(self):
        table = config.WIFI_RETRY_BACKOFF_S
        delay = table[min(self._backoff_index, len(table) - 1)]
        self._backoff_index += 1
        return delay
