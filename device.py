"""
device.py
---------
Handles this device's identity and its registration handshake with
the FastAPI "hydro" backend:
  1. Log in (auth.login()) to get a fresh JWT.
  2. POST the device itself to config.DEVICE_URL (HydroDevice.device_id).
  3. Bulk-register its actuators to config.ACTUATOR_BULK_URL.

Run once at boot (and retried from main.py if WiFi drops and
recovers), so the backend always has an up-to-date picture of the
device and what it can do.
"""

import ujson
import urequests

import config
import auth

HTTP_TIMEOUT_S = getattr(config, "HTTP_TIMEOUT_S", 8)


class Device:
    def __init__(self, actuator_manager):
        self.device_id = config.DEVICE_CODE
        self.actuator_manager = actuator_manager
        self.registered = False

    def register(self, ip_address):
        if not auth.is_authenticated():
            if not auth.login():
                self.registered = False
                return False

        if not self._register_device(ip_address):
            self.registered = False
            return False

        if not self._register_actuators():
            self.registered = False
            return False

        self.registered = True
        return True

    def _register_device(self, ip_address):
        payload = {
            "device_id": self.device_id,
            "client_id": config.CLIENT_ID,
            "ip_address": ip_address,
        }
        return self._post(config.DEVICE_URL, payload, "device")

    def _register_actuators(self):
        payload = {
            "device_id": self.device_id,
            "actuators": self.actuator_manager.registration_payload(),
        }
        return self._post(config.ACTUATOR_BULK_URL, payload, "actuators")

    def _post(self, url, payload, label):
        body = ujson.dumps(payload)
        try:
            resp = urequests.post(url, data=body, headers=auth.build_headers(),
                                   timeout=HTTP_TIMEOUT_S)

            if resp.status_code == 401:
                resp.close()
                if not auth.login():
                    return False
                resp = urequests.post(url, data=body, headers=auth.build_headers(),
                                       timeout=HTTP_TIMEOUT_S)

            ok = 200 <= resp.status_code < 300
            status = resp.status_code
            resp.close()

            if ok:
                print("[device] %s registered OK" % label)
            else:
                print("[device] %s registration rejected, status %s" % (label, status))
            return ok

        except Exception as e:
            print("[device] %s registration failed: %s" % (label, e))
            return False