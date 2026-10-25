"""Configuration for the central billiard controller only.

This module intentionally does not import ``config.py``.  Hydro and billiard
can therefore run from the same repository without sharing device identity,
authentication, or API routes.
"""

try:
    from secrets import WIFI_SSID, WIFI_PASSWORD, BILLIARD_DEVICE_ID, BILLIARD_DEVICE_KEY
except ImportError:
    # Keep the firmware bootable until secrets.py is provisioned.  API calls
    # will report the missing credentials instead of silently using hydro JWT.
    WIFI_SSID = ""
    WIFI_PASSWORD = ""
    BILLIARD_DEVICE_ID = "BILLIARD-CONTROLLER-01"
    BILLIARD_DEVICE_KEY = ""

SSID = WIFI_SSID
PASSWORD = WIFI_PASSWORD
FASTAPI_URL = "http://192.168.1.66:8000"

DEVICE_ID = BILLIARD_DEVICE_ID
DEVICE_KEY = BILLIARD_DEVICE_KEY
FIRMWARE_VERSION = "0.1.0"

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

STATE_FILE = "billiard_state.json"
QUEUE_FILE = "billiard_events.jsonl"
ERROR_FILE = "billiard_event_errors.jsonl"
MAX_SYNC_BATCH = 500
HEARTBEAT_S = 45                 # must remain below backend's 90 s offline timeout
HTTP_TIMEOUT_S = 8
RETRY_BACKOFF_S = (2, 4, 8, 16, 32, 60)
