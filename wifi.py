"""
wifi.py
-------
Owns the WLAN station interface: connecting, reporting status, and
reconnecting with escalating backoff if the link drops. Every other
module treats WiFi as "up or down" through this class rather than
touching the `network` module directly.

Reads config.SSID / config.PASSWORD (the new config.py names).
WIFI_CONNECT_TIMEOUT_S / WIFI_RETRY_BACKOFF_S aren't defined in the
new config.py, so sane defaults are used unless you add them there.
"""

import network
import time

import config

CONNECT_TIMEOUT_S = getattr(config, "WIFI_CONNECT_TIMEOUT_S", 15)
RETRY_BACKOFF_S = getattr(config, "WIFI_RETRY_BACKOFF_S", (2, 5, 10, 20, 30))


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
        """Blocking connect attempt, bounded by CONNECT_TIMEOUT_S."""
        if self.is_connected():
            return True

        print("[wifi] connecting to '%s'..." % config.SSID)
        self._wlan.connect(config.SSID, config.PASSWORD)

        start = time.time()
        while not self._wlan.isconnected():
            if time.time() - start > CONNECT_TIMEOUT_S:
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
        table = RETRY_BACKOFF_S
        delay = table[min(self._backoff_index, len(table) - 1)]
        self._backoff_index += 1
        return delay