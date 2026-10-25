"""HTTP client for the billiard device-key contract; no hydro JWT is used."""

import ujson
import urequests

import billiard_config as config


class BilliardApi:
    def headers(self):
        return {
            "Content-Type": "application/json",
            "X-Device-Id": config.DEVICE_ID,
            "X-Device-Key": config.DEVICE_KEY,
        }

    def _request(self, method, url, payload=None):
        if not config.DEVICE_KEY:
            raise RuntimeError("BILLIARD_DEVICE_KEY is not configured")
        response = None
        try:
            if method == "GET":
                response = urequests.get(url, headers=self.headers())
            else:
                response = urequests.post(url, headers=self.headers(), data=ujson.dumps(payload))
            body = response.json() if response.content else {}
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError("HTTP %s: %s" % (response.status_code, body))
            return body
        finally:
            if response:
                response.close()

    def state(self):
        return self._request("GET", config.STATE_URL)

    def event(self, event):
        return self._request("POST", config.EVENTS_URL, event)

    def sync(self, events):
        return self._request("POST", config.SYNC_URL, {
            "device_id": config.DEVICE_ID, "events": events,
        })
