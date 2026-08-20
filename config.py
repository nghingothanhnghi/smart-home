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
PASSWORD = "71237123"

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
    "pump": "25",
    "fan": "23",
    "light": "27",
    "water_pump": "16",
    "valve": "18",
}

TYPE_TO_HARDWARE = {
    "pump": "relay",
    "fan": "relay",
    "light": "relay",
    "water_pump": "mosfet",   # 👈 THIS is enough
    "valve": "relay",
}

# 0–100 (%)
PUMP_SPEED = {
    "16": 0
}

# ================================
# 🔌 RUNTIME STATE STORAGE
# ================================
# This is what actually drives relays

ACTUATOR_STATES = {
    "25": 0,
    "23": 0,
    "27": 0,
    "16": 0,
    "18": 0,
}

# Backend logic:
# 1 = actuator ON
# 0 = actuator OFF

# Relay hardware is ACTIVE LOW:
# GPIO LOW  -> relay ON
# GPIO HIGH -> relay OFF


# ================================
# ⏱ TIMING CONFIG
# ================================
SEND_INTERVAL = 10  # seconds (send sensor data)
RETRY_DELAY = 5     # seconds (retry when failed)