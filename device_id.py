"""
device_id.py
------------
Derives a stable, unique device code from the ESP32's hardware chip
ID. This is what identifies the unit to the FastAPI backend as
`device_id` (HydroDevice.device_id), so it must never change across
reboots/deploys for the same physical board.
"""

import machine
import ubinascii

_cached_code = None


def get_device_code():
    """Return a stable device code string, e.g. 'esp32-a1b2c3d4e5f6'."""
    global _cached_code
    if _cached_code is None:
        raw = machine.unique_id()
        hex_id = ubinascii.hexlify(raw).decode()
        _cached_code = "esp32-" + hex_id
    return _cached_code


# Backward-compatible alias - older modules/snippets may still import
# get_device_id(); keep it pointed at the same function.
get_device_id = get_device_code