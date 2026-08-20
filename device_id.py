"""
device_id.py
------------
Derives a stable, unique device ID from the ESP32's hardware chip ID.
This is what identifies the unit to the FastAPI backend, so it must
never change across reboots/deploys for the same physical board.
"""

import machine
import ubinascii

_cached_id = None


def get_device_id():
    """Return a stable device id string, e.g. 'esp32-a1b2c3d4e5f6'."""
    global _cached_id
    if _cached_id is None:
        raw = machine.unique_id()
        hex_id = ubinascii.hexlify(raw).decode()
        _cached_id = "esp32-" + hex_id
    return _cached_id
