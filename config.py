"""
config.py
---------
Single source of truth for hardware pin mapping, backend endpoints,
timing constants and feature flags. No secrets live here directly -
they are imported from secrets.py so this file CAN be committed to
version control safely.

Editing this file is how you "scale" the project: add a light, change
a pin, point at a new backend, all without touching logic files.
"""

import secrets

# =========================================================
# DEVICE IDENTITY
# =========================================================
DEVICE_MODEL = "esp32-relay-controller"
FIRMWARE_VERSION = "1.0.0"

# =========================================================
# WIFI
# =========================================================
WIFI_SSID = secrets.WIFI_SSID
WIFI_PASSWORD = secrets.WIFI_PASSWORD
WIFI_CONNECT_TIMEOUT_S = 15
WIFI_RETRY_BACKOFF_S = (2, 5, 10, 20, 30)  # escalating backoff, then repeats last value

# =========================================================
# BACKEND (FastAPI)
# =========================================================
BACKEND_BASE_URL = "http://192.168.1.66:8000"

BACKEND_ENDPOINTS = {
    "register": "/devices/register",
    "commands": "/devices/{device_id}/commands",
    "ack": "/devices/{device_id}/commands/{command_id}/ack",
    "state": "/devices/{device_id}/state",
    "heartbeat": "/devices/{device_id}/heartbeat",
}

DEVICE_AUTH_TOKEN = secrets.DEVICE_AUTH_TOKEN
DEVICE_HMAC_SECRET = secrets.DEVICE_HMAC_SECRET

HTTP_TIMEOUT_S = 8
COMMAND_POLL_INTERVAL_S = 3
STATE_PUSH_INTERVAL_S = 15
HEARTBEAT_INTERVAL_S = 30

# =========================================================
# GPIO PIN MAP
# =========================================================
# NOTE on ESP32 WROOM pin choice:
# - Avoided GPIO 34-39 (input-only, cannot drive relays).
# - Avoided GPIO 0, 2, 12, 15 (strapping pins - can break boot if
#   pulled the wrong way by an externally-connected relay module).
# - Avoided GPIO 6-11 (connected to internal flash, unusable).
# - I2C bus uses the common default SDA=21 / SCL=22 for the OLED.
#
# Relay modules are almost always ACTIVE LOW (a LOW signal energizes
# the relay coil through the optocoupler). RELAY_ACTIVE_LOW below
# controls this globally; override per-channel if you mix modules.
RELAY_ACTIVE_LOW = True

# 6 lights, each on its own 220V-rated relay channel.
LIGHT_PINS = {
    "light_1": 13,
    "light_2": 14,
    "light_3": 27,
    "light_4": 26,
    "light_5": 25,
    "light_6": 33,
}

# Sliding door: two relay channels drive a motor contactor/relay in
# opposite directions (OPEN / CLOSE). They are hardware-interlocked
# in software (relay.py) so both can never be energized together.
DOOR_OPEN_PIN = 32
DOOR_CLOSE_PIN = 23

# "PULSE" -> relay energizes for DOOR_PULSE_S seconds then switches off
#            (typical for sliding door controllers that self-latch on
#            a momentary trigger, e.g. most commercial slide operators).
# "HOLD"  -> relay stays energized until explicitly stopped or until
#            DOOR_MAX_RUN_S safety timeout is hit (use if your motor
#            needs continuous power while travelling, with end-limit
#            switches doing the actual stopping).
DOOR_MODE = "PULSE"
DOOR_PULSE_S = 1.0
DOOR_MAX_RUN_S = 15  # safety cutoff regardless of mode

I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_I2C_ADDR = 0x3C

# =========================================================
# SAFETY
# =========================================================
# Hard ceiling: any relay (light or door) that has been ON longer than
# this is force-switched OFF by relay.py's watchdog sweep. Prevents a
# stuck/duplicated command or dropped connection from leaving a 220V
# load energized indefinitely. Lights use a long ceiling; doors use
# DOOR_MAX_RUN_S instead (see relay.py).
DEFAULT_MAX_ON_TIME_S = 12 * 60 * 60  # 12 hours

# =========================================================
# OPTIONAL SENSORS (kept for future scaling; disabled by default
# since this deployment is lights + door only)
# =========================================================
ENABLE_SENSORS = False
DHT11_PIN = 4
EC_PPM_ADC_PIN = 35  # input-only pin is fine here, it's analog-only

# =========================================================
# LOCAL AUTOMATION (optional; backend remains source of truth)
# =========================================================
ENABLE_LOCAL_AUTOMATION = False

# =========================================================
# MISC
# =========================================================
STATUS_LED_PIN = 2  # onboard LED on most WROOM dev boards
