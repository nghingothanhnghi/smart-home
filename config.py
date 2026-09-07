# config.py
# ================================
# 🔐 DEVICE ID (unique per ESP32)
# ================================
from device_id import get_device_code
from device_id import get_device_code
from secrets import (
    WIFI_SSID,
    WIFI_PASSWORD,
    AUTH_USERNAME,
    AUTH_PASSWORD,
)

DEVICE_CODE = get_device_code()
# 👉 This is sent to backend as `device_id`
# 👉 Backend stores it in HydroDevice.device_id (STRING, unique)

# ================================
# 📶 WIFI CONFIG
# ================================
SSID = WIFI_SSID
PASSWORD = WIFI_PASSWORD

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

# ================================
# 📍 DEVICE LOCATION
# ================================
# Sent to the backend as HydroDevice.location (plain string, e.g. for
# grouping devices or POST /hydro/devices/location/{location}/control).
# Since one codebase gets flashed to every board (see README's
# "Multiple devices" section), set this PER BOARD before flashing -
# leave as None/empty to skip syncing location entirely.
DEVICE_LOCATION = "Greenhouse A"  # ASSUMPTION: placeholder - set the real location for this board

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

FLOW_URL = FASTAPI_URL + "/hydro/flow-readings"

# ================================
# 💧 FLOW SENSORS (per pump)
# ================================
# Maps actuator TYPE (must match a key in TYPE_TO_GPIO for a pump) to
# the native GPIO the flow sensor's pulse output is wired to.
# MUST be a native pin ("34"), never "mcp:..." — see gpio_manager.py,
# expander pins can't fire interrupts fast enough for pulse counting.
FLOW_SENSOR_ENABLED = True

FLOW_SENSOR_PINS = {
    "water_pump": "34",
    # "pump": "35",   # add a second one the same way if this board has 2 pumps
}

# Pulses-per-liter, from the sensor's datasheet (YF-S201 ≈ 450 P/L).
# Calibrate per physical sensor if you want L/min to be accurate.
FLOW_CALIBRATION = {
    "water_pump": 450,
}

# ================================
# 🤖 AUTO MODE FLAG
# ================================
AUTO_MODE = {"enabled": False}   # mutable dictionary
# 👉 If True → ESP32 uses local logic (auto_control)
# 👉 If False → controlled by backend

# ================================
# 🧩 GPIO EXPANDERS (optional)
# ================================
# Each entry is one physical I2C GPIO-expander chip, keyed by a short
# unit id (a string, used in pin descriptor strings like "mcp:0:5"
# below - see gpio_manager.py). Leave this dict empty if every
# actuator uses a native ESP32 GPIO, as this deployment currently does
# - gpio_manager.py never touches I2C for an expander unless some pin
# descriptor actually asks for one.
#
# unit_cfg keys: driver (default "mcp23017"), i2c_id, scl, sda, addr,
# freq (all optional except addr once you have more than one chip on
# the bus - see the MCP23017's A0-A2 address pins).
#
# Example (uncomment + wire an MCP23017 before use):
# GPIO_EXPANDERS = {
#     "0": {"driver": "mcp23017", "i2c_id": 0, "scl": 22, "sda": 21, "addr": 0x20},
# }
GPIO_EXPANDERS = {}

# ================================
# ⚡ GPIO MAPPING (CRITICAL)
# ================================
# Map actuator type → pin descriptor (STRING). A descriptor is either
# a native ESP32 GPIO number ("13") or an expander pin ("mcp:0:5" -
# unit "0" from GPIO_EXPANDERS above, expander pin 5). Every value
# here goes through gpio_manager.py, so mixing native and expander
# pins in the same TYPE_TO_GPIO is fine - e.g. to add a 7th light on
# an expander without touching relay.py/actuators.py, add
# "light_7": "mcp:0:0" below (and "light_7": "relay" to
# TYPE_TO_HARDWARE). Note: expander pins are digital in/out only (no
# PWM) - don't point a "mosfet" TYPE_TO_HARDWARE entry at one, see
# gpio_manager.get_pwm_pin's docstring.

TYPE_TO_GPIO = {
    "light_1": "13",
    "light_2": "14",
    "light_3": "27",
    "light_4": "26",
    "light_5": "25",
    "light_6": "33",
    "sliding_door": "32,23",  # informational only for registration payload; see DOOR_* below
}

TYPE_TO_HARDWARE = {
    "light_1": "relay",
    "light_2": "relay",
    "light_3": "relay",
    "light_4": "relay",
    "light_5": "relay",
    "light_6": "relay",
    "sliding_door": "door",   # tells RelayManager to build a DoorChannel, not a RelayChannel
}

# ================================
# 🚪 SLIDING DOOR (interlocked pair)
# ================================
DOOR_OPEN_PIN = 32
DOOR_CLOSE_PIN = 23
# Both DOOR_OPEN_PIN/DOOR_CLOSE_PIN are pin descriptors too (see the
# GPIO MAPPING note above) - a plain int like 32 is a native GPIO,
# same as always; "mcp:0:2" would put the door on an expander pin.
#
# "HOLD": relay stays energized while the door travels, until an
# explicit stop command or DOOR_MAX_RUN_S trips. Switched from
# "PULSE" because a stop command can only ever interrupt something
# that's still energized - in PULSE mode the relay self-released
# after DOOR_PULSE_S (2s), long before a stop sent from the backend
# could arrive within the SEND_INTERVAL (10s) poll window, so `stop`
# was a guaranteed no-op. Requires the door operator to be driven by
# continuous power while travelling (with its own end-limit switches),
# not a momentary trigger - confirm this matches the physical motor
# controller before flashing.
DOOR_MODE = "HOLD"
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
    "13": 0, "14": 0, "27": 0, "26": 0, "25": 0, "33": 0,
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
