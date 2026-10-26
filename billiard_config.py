"""Configuration for the central billiard controller only.

This module intentionally does not import ``config.py``.  Hydro and billiard
can therefore run from the same repository without sharing device identity,
authentication, or API routes.
"""

try:
    import secrets as _secrets
except ImportError:
    _secrets = None


def _secret(name, default=""):
    # Each name is read on its own, so a secrets.py that is missing one of
    # the BILLIARD_* names no longer blanks out the WiFi credentials too.
    return getattr(_secrets, name, default) if _secrets else default


WIFI_SSID = _secret("WIFI_SSID")
WIFI_PASSWORD = _secret("WIFI_PASSWORD")
BILLIARD_DEVICE_ID = _secret("BILLIARD_DEVICE_ID", "BILLIARD-CONTROLLER-01")
# Empty key -> billiard_api raises a clear error instead of using a hydro JWT.
BILLIARD_DEVICE_KEY = _secret("BILLIARD_DEVICE_KEY")

SSID = WIFI_SSID
PASSWORD = WIFI_PASSWORD
FASTAPI_URL = "http://192.168.1.66:8000"

DEVICE_ID = BILLIARD_DEVICE_ID
DEVICE_KEY = BILLIARD_DEVICE_KEY
FIRMWARE_VERSION = "0.2.0"

STATE_URL = FASTAPI_URL + "/billiard/controller/state"
EVENTS_URL = FASTAPI_URL + "/billiard/controller/events"
SYNC_URL = FASTAPI_URL + "/billiard/controller/sync"

# Physical RS485 address -> billiard_tables.id.  Provision these from the
# manager's table list; never send an RS485 address to the backend.
TABLE_MAP = {
    1: 11,
    2: 12,
    3: 13,
}

# ---- persistent files (LittleFS) ----
STATE_FILE = "billiard_state.json"
QUEUE_FILE = "billiard_events.jsonl"
ERROR_FILE = "billiard_event_errors.jsonl"
ERROR_FILE_MAX_BYTES = 16 * 1024   # rotated to ERROR_FILE + ".old" beyond this

# ---- sync tuning ----
SERVER_MAX_BATCH = 500             # backend limit, NOT a safe ESP32 RAM limit
MAX_SYNC_BATCH = 20                # events per /sync request on the device
MAX_BATCHES_PER_PASS = 10          # yield to other tasks after this many
LOOP_MS = 500                      # main loop tick; network only when needed
HTTP_TIMEOUT_S = 8
RETRY_BACKOFF_S = (2, 4, 8, 16, 32, 60)
AUTH_RETRY_S = 60                  # 401/403: never retry in a tight loop
STALL_RETRY_S = 60                 # re-send un-acked (rejected/blocked) events this often

# ---- liveness ----
# Backend flips the device to offline after 90 s without ANY authenticated
# request, so a cheap GET /state keepalive is enough - it needs no seq and
# no flash write.  A real heartbeat *event* (diagnostics) is queued rarely.
KEEPALIVE_S = 45
HEARTBEAT_EVENT_S = 3600

# ---- clock ----
NTP_HOST = "pool.ntp.org"
NTP_RESYNC_S = 6 * 3600
NTP_RETRY_S = 300
OFFLINE_ALERT_S = 24 * 3600        # backend rejects events older than 48 h
