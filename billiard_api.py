"""HTTP client for the billiard device-key contract; no hydro JWT is used."""

import time
import ujson
import urequests

import billiard_config as config


class ApiError(Exception):
    """Non-2xx response. 401/403 mean credentials/device mismatch (no tight retry)."""

    def __init__(self, status, body):
        Exception.__init__(self, "HTTP %s: %s" % (status, body))
        self.status = status
        self.body = body


class BilliardApi:
    def __init__(self):
        # ticks_ms of the last successful authenticated call. ANY successful
        # call refreshes last_seen on the backend, so main uses this to decide
        # whether a keepalive GET /state is needed.
        self.last_ok_ms = None

    def headers(self):
        return {
            "Content-Type": "application/json",
            "X-Device-Id": config.DEVICE_ID,
            "X-Device-Key": config.DEVICE_KEY,
        }

    @staticmethod
    def _body(response):
        try:
            return response.json()
        except Exception:
            try:
                return response.text
            except Exception:
                return None

    def _request(self, method, url, payload=None):
        if not config.DEVICE_KEY:
            raise RuntimeError("BILLIARD_DEVICE_KEY is not configured")
        response = None
        try:
            # timeout: without it a half-dead backend blocks the whole loop
            if method == "GET":
                response = urequests.get(url, headers=self.headers(),
                                         timeout=config.HTTP_TIMEOUT_S)
            else:
                response = urequests.post(url, headers=self.headers(),
                                          data=ujson.dumps(payload),
                                          timeout=config.HTTP_TIMEOUT_S)
            status = response.status_code
            body = self._body(response)
            if status < 200 or status >= 300:
                raise ApiError(status, body)
            self.last_ok_ms = time.ticks_ms()
            return body if isinstance(body, dict) else {}
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
