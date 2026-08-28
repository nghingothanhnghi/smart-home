# ESP32 IoT Relay Controller — 6 Lights + Sliding Door (220V)

MicroPython firmware for an ESP32-WROOM devkit that controls 6 lighting
circuits and one sliding door motor (all 220V AC loads switched through
relay modules), coordinated by an existing FastAPI "hydro" backend.

## Folder Structure

- **`main.py`**: Entry point. Boot sequence + main loop coordinating WiFi,
  actuators, backend control loop, safety sweep, and the OLED.
- **`config.py`**: Centralized configuration — device identity, WiFi,
  backend URLs, GPIO/pin map, door mode, timing constants. Imports
  `AUTH_USERNAME` / `AUTH_PASSWORD` from `secrets.py`; WiFi `SSID` /
  `PASSWORD` currently live directly in this file (see **Security note**
  below).
- **`secrets.py`**: Private backend login credentials
  (`AUTH_USERNAME`, `AUTH_PASSWORD`). **Do not commit this file** —
  already covered by `.gitignore`.
- **`device_id.py`**: Derives a stable device ID (`esp32-<chip id hex>`)
  from the ESP32's hardware unique ID. Never changes across
  reboots/reflashes for the same physical board.
- **`auth.py`**: Logs in against the backend's `POST /auth/login`
  (OAuth2 form-encoded username/password) and keeps `config.HEADERS`
  populated with a fresh Bearer JWT. Re-logs-in transparently on any
  `401`.
- **`device.py`**: Registers the device and its actuator list with the
  backend, resolves the backend's numeric device PK, and syncs
  `DEVICE_LOCATION` once per boot.
- **`wifi.py`**: WiFi connect + reconnect-with-backoff.
- **`relay.py`**: Hardware abstraction for every actuator channel —
  active-low relay logic, per-channel safety timeout, and the
  interlocked `DoorChannel` for the sliding door (PULSE or HOLD mode).
- **`actuators.py`**: Maps relay channels to logical actuator types
  (`light_1`...`light_6`, `sliding_door`), builds the backend
  registration payload, and executes commands coming back from the
  backend.
- **`control.py`**: Polls `GET /hydro/status` for desired actuator
  state, executes it, and pushes optional sensor telemetry to
  `POST /sensor/data`. Polling doubles as the heartbeat — there's no
  separate heartbeat endpoint.
- **`sensors.py`**: Optional DHT11 / analog EC-PPM readers, feeding
  `control.py`'s sensor push. Disabled by default
  (`config.ENABLE_SENSORS` not set → falls back to `False`) — kept so
  the project can scale to sensor-equipped variants without
  restructuring.
- **`oled_display.py` / `ssd1306.py`**: Local I2C OLED status screen
  (WiFi/backend/actuator/mode state). Optional — firmware runs fine
  with no display attached; drawing is skipped safely if init fails.

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
- **`"HOLD"` (current default)**: the OPEN/CLOSE relay stays energized
  for the whole travel time, until an explicit `stop` command or the
  `DOOR_MAX_RUN_S` (20s) safety timeout trips. **Requires your door
  operator to accept continuous drive power while traveling, with its
  own end-limit switches** — confirm this matches your physical motor
  controller before flashing. This mode was chosen over `"PULSE"`
  specifically so a backend `stop` command can actually interrupt a
  still-energized relay.
- `"PULSE"`: energizes the OPEN/CLOSE relay for `DOOR_PULSE_S` seconds
  (default 2s) then auto-releases (non-blocking — handled by
  `RelayManager.service_pulses()` each loop tick, not a blocking
  `sleep`). Matches commercial operators that accept a momentary
  trigger, but a `stop` command sent after the pulse has already
  self-released is a no-op — use `HOLD` if you need `stop` to be
  meaningful.

## Safety Design

- Every relay/door channel boots into the OFF/closed state
  (`RelayChannel.__init__` / `DoorChannel.__init__` force `set(False)`
  before anything else runs).
- `RelayManager.safety_sweep()` runs every loop iteration and force-offs
  (or `stop()`s, for the door) any channel that's exceeded its max
  on-time — protects against a stuck command, a crashed backend, or a
  dropped connection leaving a light or the door motor energized
  indefinitely. Default ceiling: 12h for lights, 20s (`DOOR_MAX_RUN_S`)
  for the door in HOLD mode.
- The door's OPEN and CLOSE channels are hardware-interlocked in
  software (`DoorChannel.set`) — the opposite channel is always
  switched off before the target one is switched on, so the two can
  never be energized simultaneously.
- Re-commanding a channel to the state it's already in is a no-op —
  this prevents a backend that re-sends the same desired state every
  poll cycle from continually resetting the safety-sweep timer.
- Any unhandled exception in the main loop forces all relays off
  (`actuators.all_off()`) and continues; a fatal error at the top level
  forces all relays off and resets the board (see `main.py`).

## Backend API Contract (FastAPI "hydro" backend)

Base URL: `config.FASTAPI_URL`. All authenticated requests carry
`Authorization: Bearer <JWT>` (obtained via login, see below and
`auth.py`) in `config.HEADERS`.

```
POST /auth/login
  body (form-urlencoded): username, password
  response: { access_token, ... }
  → JWT stored in config.HEADERS; auth.py re-logs-in automatically on any 401.

POST /hydro/devices
  body: { device_id, name, client_id, ip_address, location? }
  response: { id, ... }               # numeric PK, resolved into Device.numeric_id
  → 400 "already exists" is handled as success: falls back to
    GET /hydro/devices and matches by device_id to recover the numeric id.

GET  /hydro/devices
  → used only for the idempotent lookup above.

PUT  /hydro/devices/{numeric_id}
  body: { location }
  → syncs config.DEVICE_LOCATION once per boot (POST only sets it on a
    device's first-ever registration; existing devices need this route).

GET  /actuators/device/{numeric_id}
  → checked before bulk-registering actuators, so a reboot never
    re-creates duplicate rows for the same device.

POST /actuators/bulk
  body: [ { actuator_id, type, name, pin, port, device_id }, ... ]
  → CREATE-ONLY, not an upsert. Called exactly once per boot, only if
    GET /actuators/device/{id} came back empty. Do not call this
    repeatedly — see control.py's module docstring for the duplicate-row
    incident this caused.

GET  /hydro/status
  response: [ { device_id (numeric), device_name, actuators: [
      { type, current_state, manual_state, mode, pending_command, ... }
  ] }, ... ]
  → polled every config.SEND_INTERVAL (10s); doubles as the heartbeat
    (no separate heartbeat endpoint). Desired state per actuator is
    manual_state if not null (a dashboard/app override), else
    current_state (the backend's own automation decision). A
    pending_command of "stop" overrides on/off for that actuator this
    cycle.

POST /sensor/data
  body: { device_id, timestamp, temperature_c?, humidity_pct?, ec_ppm? }
  → pushed every config.SEND_INTERVAL if config.ENABLE_SENSORS is True.
```

Valid `actuator_id` / desired-state pairs the firmware understands:

| actuator_id | supported actions |
|---|---|
| `light_1` … `light_6` | `on`, `off`, `toggle` |
| `sliding_door` | `on` (open), `off` (close), `toggle`, `stop` (HOLD mode only) |

## Setup

1. Flash MicroPython to the ESP32-WROOM (esptool + the official
   MicroPython `.bin` for your board).
2. Copy all files in this folder to the board's filesystem (e.g. via
   `mpremote cp *.py :` or `ampy`, or Thonny's file browser). You will
   need MicroPython's `urequests` and `ujson` modules available on the
   device if they aren't already part of your firmware build.
3. Create `secrets.py` with `AUTH_USERNAME` and `AUTH_PASSWORD` — the
   login credentials for this device on the FastAPI backend. This file
   is gitignored; never commit it.
4. Edit `config.py`:
   - `SSID` / `PASSWORD` for your WiFi network.
   - `FASTAPI_URL` to point at your backend.
   - `DEVICE_LOCATION` — set per physical board before flashing (this
     codebase is shared across every controller; see **Scaling**
     below).
   - Confirm `DOOR_MODE` matches what your physical door operator
     actually needs (see **Hardware / Wiring** above).
5. Reset the board. `main.py` runs automatically; watch the serial
   console (115200 baud) for boot/login/registration logs.

### Security note

`SSID` / `PASSWORD` (WiFi) are currently hardcoded directly in
`config.py`, which **is** tracked by git — only `secrets.py` is
gitignored. If this repo is shared or pushed anywhere, either move the
WiFi credentials into `secrets.py` alongside `AUTH_USERNAME` /
`AUTH_PASSWORD`, or make sure `config.py` itself is kept private for
your deployment.

## Operating the Device

Day-to-day control does **not** happen on the ESP32 itself — it happens
through whatever dashboard/app talks to the FastAPI backend. The
firmware is a dumb executor: it polls `GET /hydro/status` every
`config.SEND_INTERVAL` (10s) and does whatever the backend says.

- **Turn a light on/off**: set that light's state (manual override or
  automation) on the backend/dashboard. The ESP32 picks it up on its
  next poll (worst case ~10s later).
- **Open/close the door**: same — command `sliding_door` via the
  backend. In `HOLD` mode the relay stays energized for the whole
  travel; send `stop` to interrupt it mid-travel, or wait for the
  `DOOR_MAX_RUN_S` (20s) safety timeout to force it off automatically.
- **Switch to local automation**: flip `config.AUTO_MODE["enabled"] =
  True`. While enabled, the ESP32 stops polling `/hydro/status`
  entirely (so the backend and local logic never fight over an
  actuator) — you'd add your own rules where `main.py` marks the
  placeholder (step 4 of the main loop).
- **Monitor what's happening**: connect over serial at 115200 baud.
  Every login, registration, command execution, and safety-sweep trip
  is logged with a `[module]` prefix (`[wifi]`, `[auth]`, `[device]`,
  `[control]`, `[actuators]`, `[oled]`), which is the fastest way to
  see what the firmware is actually doing versus what you expect.
- **Check status at a glance**: the OLED (if attached) refreshes every
  2s and shows WiFi/IP, backend registration state, how many actuators
  are currently on, and whether you're in AUTO or BACKEND mode.

### Before flashing to a real door motor

Bench-test the door channel in isolation first — wire just the
OPEN/CLOSE relay pins, send `on`/`off`/`stop` commands from the
backend, and confirm the interlock and `DOOR_MAX_RUN_S` timeout behave
as expected **before** connecting the actual motor/contactor. This
catches `DOOR_MODE` mismatches (see **Hardware / Wiring** above) without
risking hardware.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `[auth] login failed, status 422` | Backend expects form-encoded body, not JSON — shouldn't happen unless `auth.py` was modified; check `secrets.py` values are correct otherwise. |
| `[device] could not confirm actuator status, skipping bulk create` | `GET /actuators/device/{id}` failed — actuators won't register until this succeeds on a later boot/reconnect. |
| Actuators registered multiple times | Something is calling `POST /actuators/bulk` outside of `device.py`'s guarded first-boot path — see `control.py`'s module docstring. |
| Door `stop` command has no visible effect | `DOOR_MODE` is `"PULSE"` and the pulse already self-released before `stop` arrived — switch to `"HOLD"`. |
| OLED stays blank | Non-fatal — check serial log for `[oled] display not available, continuing without it: ...` and verify I2C wiring/address. |

## Scaling This Project

- **Add a 7th light**: add one line to `TYPE_TO_GPIO` (and
  `TYPE_TO_HARDWARE`) in `config.py`. Nothing else changes —
  `actuators.py` and `relay.py` build the channel automatically.
- **Add another door / curtain**: add a new `"door"`-hardware entry in
  `config.TYPE_TO_HARDWARE` and wire up its own open/close pins,
  following `DoorChannel`'s interlocked pattern.
- **Add sensors**: set `config.ENABLE_SENSORS = True` (and optionally
  `DHT11_PIN` / `EC_PPM_ADC_PIN`), wire the hardware, and
  `control.py`'s tick will start pushing `sensors.read_all()` results
  automatically.
- **Multiple devices**: since `device_id.py` derives a unique ID per
  board automatically, this exact codebase can be flashed to every
  controller in the building — only `secrets.py`'s credentials and
  `config.py`'s `DEVICE_LOCATION` differ per unit.
