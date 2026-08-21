# config.py
# ================================
# 🔐 DEVICE ID (unique per ESP32)
# ================================
from device_id import get_device_code
from secrets import AUTH_USERNAME, AUTH_PASSWORD

DEVICE_CODE = get_device_code()
# 👉 This is sent to backend as `device_id`
# 👉 Backend stores it in HydroDevice.device_id (STRING, unique)

# ================================
# 📶 WIFI CONFIG
# ================================
SSID = "Oanh Nguyen 2.4Ghz"
PASSWORD = "24322432"

# ================================
# 🌐 BACKEND BASE URL
# ================================
FASTAPI_URL = "http://192.168.1.66:8000"

# ================================
# 👤 AUTH / USER CONTEXT
# ================================
# ⚠️ CHANGED: no more static AUTH_TOKEN baked into firmware.
# A static JWT breaks in two independent ways:
#   1. DB wipe        -> the user it references no longer exists -> 401 forever
#   2. Natural expiry  -> ACCESS_TOKEN_EXPIRE_MINUTES defaults to 30 days
#      on the backend -> token dies even with a healthy DB
#
# Storing username/password instead and logging in at boot means BOTH
# problems go away: a DB wipe only requires recreating the same
# username/password (no reflash), and expiry is irrelevant since a fresh
# token is minted every boot.
#
# In a real deployment, move these two lines into a separate, untracked
# secrets.py (gitignored) rather than committing credentials in config.py.
AUTH_USERNAME = AUTH_USERNAME
AUTH_PASSWORD = AUTH_PASSWORD

CLIENT_ID = "706cfcdc-5e1c-4bae-b159-f66425c81ecc"  # informational only — backend ignores this on writes
USER_ID = 1                                          # informational only — backend ignores this on writes

# HEADERS starts with no Authorization — auth.login() fills it in at boot
# (see auth.py) and can refresh it again later if a request comes back 401.
HEADERS = {
    "Content-Type": "application/json"
}

# ================================
# 🔗 API ROUTES (MATCH BACKEND)
# ================================

# Auth
LOGIN_URL = FASTAPI_URL + "/auth/login"

# Device (ESP32 registration)
DEVICE_URL = FASTAPI_URL + "/hydro/devices"
# ↔ POST → create device
# ↔ GET  → list devices

# Sensor data
SENSOR_URL = FASTAPI_URL + "/sensor/data"
# ↔ POST → send sensor data

# Actuators
ACTUATOR_URL = FASTAPI_URL + "/actuators"
ACTUATOR_BULK_URL = ACTUATOR_URL + "/bulk"
# ↔ POST bulk → register actuators

# System status (IMPORTANT)
STATUS_URL = FASTAPI_URL + "/hydro/status"
# ↔ GET → ESP32 fetch commands from backend

# ================================
# 🤖 AUTO MODE FLAG
# ================================
AUTO_MODE = {"enabled": False}   # mutable dictionary
# 👉 If True → ESP32 uses local logic (auto_control)
# 👉 If False → controlled by backend

# ================================
# ⚡ GPIO MAPPING (CRITICAL)
# ================================
# Map actuator type → GPIO PIN (STRING)

TYPE_TO_GPIO = {
    "light_1": "13",
    "light_2": "14",
    "light_3": "27",
    "light_4": "26",
    "light_5": "25",
    "light_6": "33",
    "light_7": "4",     # ASSUMPTION: free GPIO, not in README's table - confirm wiring
    "sliding_door": "32,23",  # informational only for registration payload; see DOOR_* below
}

TYPE_TO_HARDWARE = {
    "light_1": "relay",
    "light_2": "relay",
    "light_3": "relay",
    "light_4": "relay",
    "light_5": "relay",
    "light_6": "relay",
    "light_7": "relay",
    "sliding_door": "door",   # tells RelayManager to build a DoorChannel, not a RelayChannel
}

# ================================
# 🚪 SLIDING DOOR (interlocked pair)
# ================================
DOOR_OPEN_PIN = 32
DOOR_CLOSE_PIN = 23
DOOR_MODE = "PULSE"        # "PULSE" (momentary trigger) or "HOLD" (energized while travelling)
DOOR_PULSE_S = 2           # PULSE mode: how long to energize the OPEN/CLOSE relay
DOOR_MAX_RUN_S = 20        # HOLD mode: safety ceiling before force-stop

# 0–100 (%)
PUMP_SPEED = {
    "16": 0
}

# ================================
# 🔌 RUNTIME STATE STORAGE
# ================================
ACTUATOR_STATES = {
    "13": 0, "14": 0, "27": 0, "26": 0, "25": 0, "33": 0, "4": 0,
    "door": 0,   # 1 = logically open, 0 = logically closed
}

# Relay hardware is ACTIVE LOW:
# GPIO LOW  -> relay ON
# GPIO HIGH -> relay OFF
RELAY_ACTIVE_LOW = True


# ================================
# ⏱ TIMING CONFIG
# ================================
SEND_INTERVAL = 10  # seconds (send sensor data)
RETRY_DELAY = 5     # seconds (retry when failed)
