# ESP32 IoT Relay Controller — 6 Lights + Sliding Door (220V)

MicroPython firmware for an ESP32-WROOM devkit that controls 6 lighting
circuits and one sliding door motor (all 220V AC loads switched through
relay modules), coordinated by an existing FastAPI backend.

## Folder Structure

- **`main.py`**: Entry point. Boot sequence + main loop coordinating WiFi,
  actuators, backend control loop, safety sweep, and the OLED.
- **`config.py`**: Centralized configuration — pin map, backend URLs,
  timing constants, feature flags. Safe to commit to version control.
- **`secrets.py`**: Private credentials (WiFi password, device auth token).
  **Do not commit this file.**
- **`device_id.py`**: Derives a stable device ID from the ESP32's chip ID.
- **`device.py`**: Registers the device (and its actuator list) with the
  FastAPI backend.
- **`auth.py`**: Builds auth headers / optional payload signing for every
  backend request.
- **`wifi.py`**: WiFi connect + reconnect-with-backoff.
- **`relay.py`**: Hardware abstraction for relay channels — active-low
  logic, per-channel safety timeout, door open/close interlock.
- **`actuators.py`**: Maps relay channels to logical actuators
  (`light_1`...`light_6`, `sliding_door`), executes backend commands.
- **`control.py`**: Polls the backend for commands, executes them,
  acknowledges, and pushes state/heartbeat back.
- **`sensors.py`**: Optional DHT11 / analog EC-PPM readers. Disabled by
  default (`config.ENABLE_SENSORS = False`) — kept so the project can
  scale to sensor-equipped variants without restructuring.
- **`oled_display.py` / `ssd1306.py`**: Local I2C OLED status screen
  (WiFi/backend/actuator state). Optional — firmware runs fine with no
  display attached.

## Hardware / Wiring

⚠️ **All loads here are 220V AC. This is not a beginner electronics
project — mains wiring must be done by, or reviewed by, a qualified
electrician. The ESP32 only ever switches the low-voltage side of an
opto-isolated relay module; it never touches mains directly.**

Use relay modules (or contactors driven by relay modules) rated for
your actual load current, with opto-isolation between the ESP32 GPIO
side and the relay coil/mains side. For the sliding door motor, most
installers use a small AC contactor for the motor itself, triggered by
the relay module — don't switch a motor directly off a 5V/10A relay
board unless it's rated for the motor's inrush current.

| Function        | GPIO | Notes |
|------------------|------|-------|
| Light 1          | 13   | Relay channel, active-low |
| Light 2          | 14   | |
| Light 3          | 27   | |
| Light 4          | 26   | |
| Light 5          | 25   | |
| Light 6          | 33   | |
| Door – OPEN      | 32   | Interlocked in software with CLOSE |
| Door – CLOSE     | 23   | Interlocked in software with OPEN |
| OLED SDA         | 21   | I2C, 128x64 SSD1306, addr 0x3C |
| OLED SCL         | 22   | |
| Status LED       | 2    | Onboard LED on most WROOM devkits |

Pins 34–39 (input-only) and the flash/strapping pins (0, 2\*, 6–12, 15)
are deliberately avoided for relay outputs. `GPIO2` is only used for
the onboard status LED, not a relay.

Door mode is configurable in `config.py` via `DOOR_MODE`:
- `"PULSE"` (default): energizes the OPEN/CLOSE relay for
  `DOOR_PULSE_S` seconds then auto-releases — matches most commercial
  sliding-door operators that accept a momentary trigger.
- `"HOLD"`: relay stays energized until an explicit `stop` command or
  the `DOOR_MAX_RUN_S` safety timeout — use if your motor needs
  continuous power while travelling and you rely on end-limit switches.

## Safety Design

- Every relay boots into the OFF state (`RelayChannel.__init__` forces
  `set(False)` before anything else runs).
- `RelayManager.safety_sweep()` runs every loop iteration and force-offs
  any channel that's exceeded its max on-time — protects against a
  stuck command, a crashed backend, or a dropped connection leaving a
  light or the door motor energized indefinitely.
- The door's OPEN and CLOSE channels are hardware-interlocked in
  software (`RelayManager.energize_door`) — the opposite channel is
  always switched off before the target one is switched on, so the two
  can never be energized simultaneously.
- Any unhandled exception in the main loop forces all relays off before
  continuing or resetting the board (see `main.py`).

## Backend API Contract (FastAPI)

The firmware expects these endpoints (paths configurable in
`config.BACKEND_ENDPOINTS`, base URL in `config.BACKEND_BASE_URL`):

```
POST /api/v1/devices/register
  body: { device_id, model, firmware_version, ip_address, actuators: [...] }

GET  /api/v1/devices/{device_id}/commands
  response: { "commands": [ { "command_id", "actuator_id", "action" }, ... ] }

POST /api/v1/devices/{device_id}/commands/{command_id}/ack
  body: { success, message }

POST /api/v1/devices/{device_id}/state
  body: { timestamp, state: { lights: {light_1: bool, ...}, door_open, door_close } }

POST /api/v1/devices/{device_id}/heartbeat
  body: { timestamp }
```

All requests carry `Authorization: Bearer <DEVICE_AUTH_TOKEN>` (see
`secrets.py` / `auth.py`). If `DEVICE_HMAC_SECRET` is set, requests also
carry an `X-Signature` header for payload integrity.

Valid `actuator_id` / `action` pairs the firmware understands:

| actuator_id | actions |
|---|---|
| `light_1` … `light_6` | `on`, `off`, `toggle` |
| `sliding_door` | `open`, `close`, `stop` |

## Setup

1. Flash MicroPython to the ESP32-WROOM (esptool + the official
   MicroPython `.bin` for your board).
2. Copy all files in this folder to the board's filesystem (e.g. via
   `mpremote cp *.py :` or `ampy`, or Thonny's file browser). You will
   need MicroPython's `urequests` module available on the device if it
   isn't already part of your firmware build.
3. Edit `secrets.py` with your real WiFi credentials and the device
   auth token issued by your FastAPI backend's provisioning flow.
4. Edit `config.py`'s `BACKEND_BASE_URL` to point at your backend.
5. Reset the board. `main.py` runs automatically; watch the serial
   console (115200 baud) for boot/registration logs.

## Scaling This Project

- **Add a 7th light**: add one line to `LIGHT_PINS` in `config.py`.
  Nothing else changes — `actuators.py` and `relay.py` build the
  channel automatically.
- **Add another door / curtain**: add a new `kind` in `relay.py` +
  a case in `actuators.py._execute_*`, following the existing door
  pattern (interlocked pair if it's motor-driven).
- **Add sensors**: flip `config.ENABLE_SENSORS = True`, wire to the
  pins already reserved in `config.py`, and thread `sensors.read_all()`
  results into `control.push_state()`'s payload.
- **Multiple devices**: since `device_id.py` derives a unique ID per
  board automatically, this exact codebase can be flashed to every
  controller in the building — only `secrets.py`'s device token differs
  per unit.
