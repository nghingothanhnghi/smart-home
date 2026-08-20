"""
device.py
---------
Handles this device's identity and its registration handshake with
the FastAPI backend: "here is who I am, here is my IP, here are the
actuators I expose". Run once at boot (and retried if it fails), so
the backend always has an up-to-date picture of the fleet.
"""

import ujson
import urequests

import config
import auth
from device_id import get_device_id


class Device:
    def __init__(self, actuator_manager):
        self.device_id = get_device_id()
        self.actuator_manager = actuator_manager
        self.registered = False

    def _url(self, key, **kwargs):
        path = config.BACKEND_ENDPOINTS[key].format(device_id=self.device_id, **kwargs)
        return config.BACKEND_BASE_URL + path

    def register(self, ip_address):
        payload = {
            "device_id": self.device_id,
            "model": config.DEVICE_MODEL,
            "firmware_version": config.FIRMWARE_VERSION,
            "ip_address": ip_address,
            "actuators": self.actuator_manager.registration_payload(),
        }

        url = self._url("register")
        headers = auth.build_signed_headers(payload)

        try:
            resp = urequests.post(url, data=ujson.dumps(payload), headers=headers)
            ok = 200 <= resp.status_code < 300
            resp.close()
            self.registered = ok
            if ok:
                print("[device] registered as", self.device_id)
            else:
                print("[device] registration rejected, status", resp.status_code)
            return ok
        except Exception as e:
            print("[device] registration failed:", e)
            self.registered = False
            return False
