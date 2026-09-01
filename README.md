# ESP32 IoT Relay Controller — 6 Lights + Sliding Door (220V)

MicroPython firmware for an ESP32-WROOM devkit that controls 6 lighting
circuits and one sliding door motor (all 220V AC loads switched through
relay modules), coordinated by an existing FastAPI "hydro" backend.

## Folder Structure

- **`main.py`**: Entry point. Boot sequence + main loop coordinating WiFi,
  actuators, backend control loop, safety sweep, and the OLED.
- **`config.py`**: Centralized configuration — device identity, WiFi,
  backend URLs, GPIO/pin map, GPIO-expander definitions, door mode, timing
  constants. Imports `AUTH_USERNAME` / `AUTH_PASSWORD` from `secrets.py`;
  WiFi `SSID` / `PASSWORD` currently live directly in this file (see
  **Security note** below).
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
- **`gpio_manager.py`**: Turns a *pin descriptor string* (native GPIO
  number, or `"mcp:<unit>:<pin>"` for an I2C expander pin) into an actual
  pin object with a `machine.Pin`-shaped API. `relay.py` and
  `actuators.py` go through this instead of touching `machine.Pin`/`PWM`
  or an expander driver directly — see **GPIO Expansion** below.
- **`mcp23017.py`**: Full driver for the MCP23017 16-bit I2C GPIO
  expander (digital in/out, pull-ups, optional interrupt-on-change).
  Used by `gpio_manager.py` whenever a `"mcp:..."` descriptor is
  configured; untouched if you never add one.
- **`relay.py`**: Hardware abstraction for every actuator channel —
  active-low relay logic, per-channel safety timeout, and the
  interlocked `DoorChannel` for the sliding door (PULSE or HOLD mode).
  Builds every pin through `gpio_manager.py`.
- **`actuators.py`**: Maps relay channels to logical actuator types
  (`light_1`...`light_6`, `sliding_door`), builds the backend
  registration payload, and executes commands coming back from the
  backend.
- **`control.py`**: Polls `GET /hydro/status` (scoped to this device via
  `?device_id=<numeric_id>`) for desired actuator state, executes it,
  and pushes sensor telemetry to `POST /sensor/data`. Polling doubles
  as the heartbeat — there's no separate heartbeat endpoint.
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

If you later add an MCP23017 GPIO expander (see **GPIO Expansion**
below), it shares the same I2C bus as the OLED (SDA 21 / SCL 22) by
default — I2C is a shared bus, so this is expected and fine as long as
each device has a distinct address (OLED 0x3C, MCP23017 0x20–0x27 via
its A0–A2 pins). Use a separate `i2c_id`/pins in `config.GPIO_EXPANDERS`
if you'd rather put it on its own bus.

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

## GPIO Expansion (`gpio_manager.py` / `mcp23017.py`)

Every pin used by `relay.py` — each light, and the door's OPEN/CLOSE
pins — is a **pin descriptor string**, not a raw GPIO number, resolved
through `gpio_manager.py`:

- `"13"` → native ESP32 GPIO 13 (unchanged behavior; every existing
  `config.py` value already uses this form).
- `"mcp:0:5"` → pin 5 of the I2C GPIO expander registered as unit `"0"`
  in `config.GPIO_EXPANDERS`.

`relay.py` and `actuators.py` never import `machine.Pin`/`PWM` or an
expander driver directly — they only ever call `gpio_manager.py`, so
adding capacity or changing hardware never touches the actuator logic:

- **Add a 7th light on an MCP23017 expander** (no free native GPIOs
  left, or you just want to keep mains wiring off the main board):
  1. In `config.py`, add an entry to `GPIO_EXPANDERS`, e.g.
     `"0": {"driver": "mcp23017", "i2c_id": 0, "scl": 22, "sda": 21, "addr": 0x20}`.
  2. Add `"light_7": "mcp:0:0"` to `TYPE_TO_GPIO` and
     `"light_7": "relay"` to `TYPE_TO_HARDWARE`.
  3. Done — `relay.py` builds a `RelayChannel` on that expander pin the
     same way it builds one on a native GPIO; `actuators.py`'s
     registration payload and `control.py`'s pin-matching already
     understand both forms.
- **Add a second expander chip / a different chip entirely** (PCF8574,
  MCP23008, TCA9555, ...): write its driver with the same
  `.pin(n, mode, pull) -> object with .value()/.on()/.off()` shape as
  `mcp23017.py`, then register a builder for it in
  `gpio_manager._BACKEND_FACTORIES` under its own `driver` name and
  give it its own descriptor prefix in `gpio_manager._parse()` (e.g.
  `"pcf:0:3"`). No changes needed in `relay.py`, `actuators.py`, or
  `control.py`.

**Hardware limitation — no PWM on expander pins:** the MCP23017 (and
most simple I2C GPIO expanders) is digital in/out only, with no PWM
peripheral behind it. `gpio_manager.get_pwm_pin()` raises a clear error
at boot if a `TYPE_TO_HARDWARE = "mosfet"` entry points at an expander
pin, rather than silently degrading a speed-controlled channel (e.g.
`water_pump`) to plain on/off — keep PWM-capable actuators on native
GPIOs.

Bench-test any new expander-backed channel the same way you would a
new relay: confirm the pin toggles correctly with the OLED/serial log
before wiring it to a live 220V load, and get an electrician's sign-off
on the mains side per the **Hardware / Wiring** warning above.

## Safety Design

- Every relay/door channel boots into the OFF/closed state
  (`RelayChannel.__init__` / `DoorChannel.__init__` force `set(False)`
  before anything else runs) — true whether the channel sits on a
  native GPIO or a GPIO-expander pin.
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
  response: { access_token, token_type }
  → JWT stored in config.HEADERS; auth.py re-logs-in automatically on any 401.

POST /hydro/devices
  body (HydroDeviceCreate): {
    device_id, name, external_id, location?, type, is_active, thresholds,
    client_id, ip_address
  }
  response (HydroDeviceOut): { id, ... }   # numeric PK -> Device.numeric_id
  → user_id/client_id are overwritten server-side from the authenticated
    user regardless of what's sent, so client_id in the payload is inert.
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
  body (list of HydroActuatorCreate): [
    { actuator_id, type, name, pin, port, is_active, default_state,
      device_id, sensor_key, manual_state }, ...
  ]
  → CREATE-ONLY, not an upsert. device_id here is the NUMERIC PK from
    the device-registration step, not the string device_id/DEVICE_CODE.
    Called exactly once per boot, only if GET /actuators/device/{id}
    came back empty. Do not call this repeatedly — see control.py's
    module docstring for the duplicate-row incident this caused.
  → `port` (Integer) is a synthetic value for expander pins
    (`gpio_manager.registration_port()`: real GPIO number for native
    pins, `9000 + unit*16 + pin` for expander pins — never collides
    with a real ESP32 GPIO 0-39). `pin` (String) always carries the
    human-readable descriptor (`"13"`, `"mcp:0:5"`, or `"32,23"` for
    the door) so the physical wiring stays recoverable from the row.

GET  /hydro/status?device_id={numeric_id}
  response: [ { device_id (numeric), device_name, location, sensors,
      actuators: [
        { id, name, type, pin, port, current_state, manual_state,
          mode, pending_command, ... }
      ], growing_batch, system, automation
  }, ... ]
  → polled every config.SEND_INTERVAL (10s), scoped to this device via
    the query param; doubles as the heartbeat (no separate heartbeat
    endpoint). Desired state per actuator is manual_state if not null
    (a dashboard/app override), else current_state (the backend's own
    automation decision). A pending_command of "stop" is fire-once —
    cleared server-side the instant this GET reads it, so a missed
    poll means a missed stop.
  → ⚠️ 'type' is a generic hardware category, confirmed live to be
    "light" for every one of the 6 lights (not light_1..light_6) — the
    sliding door's type happens to be unique but that's incidental.
    'name' is also unreliable: it's user-editable from the dashboard
    (e.g. "Light 1" can be renamed "Đèn (Garage xe)" without changing
    which GPIO it drives). The reliable fields are 'pin' and 'port',
    which `actuators.py` matches back to a `config.TYPE_TO_GPIO`
    descriptor (or its `gpio_manager.registration_port()`) — not by
    type or name. A row that matches neither is skipped rather than
    guessed at.

POST /sensor/data
  body (SensorDataCreateSchema): {
    device_id, client_id, data: { temperature?, humidity?, ec?, ppm? }
  }
  → device_id here is the STRING DEVICE_CODE (looked up via
    get_device_by_external_id against HydroDevice.device_id) — NOT the
    numeric id used by /actuators/bulk and /hydro/status. Readings are
    nested under "data" using the backend's key names (temperature/
    humidity/ppm), not sensors.py's local temperature_c/humidity_pct/
    ec_ppm names — control.py remaps them before sending. Pushed every
    config.SEND_INTERVAL if config.ENABLE_SENSORS is True. This call
    also triggers the backend's automation_service.run_control_loop,
    so it's what drives server-side automatic actuator decisions —
    not just telemetry.
```

**Two ID types, don't mix them up:**

| Field | Where used | Value |
|---|---|---|
| `device_id` (string) | `POST /hydro/devices` body, `POST /sensor/data` body | `DEVICE_CODE` from `device_id.py` |
| `device_id` (numeric) | `POST /actuators/bulk` body, `GET /hydro/status` query param | the `id` returned from `POST /hydro/devices` |

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
   - Only if you're adding a GPIO expander: `GPIO_EXPANDERS` and any
     `"mcp:..."` descriptors in `TYPE_TO_GPIO` — see **GPIO Expansion**
     above. Leave `GPIO_EXPANDERS = {}` and every pin as a plain GPIO
     number if you're not using one; nothing else changes.
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
| `[control] push sensor data rejected, status 422` | Sensor payload doesn't match `SensorDataCreateSchema` — confirm you're on the current `control.py` (readings nested under `"data"` with `temperature`/`humidity`/`ppm` keys, not the old flat `temperature_c`/`humidity_pct`/`ec_ppm` shape). |
| `poll_status()` returns nothing even though the backend has commands queued | `Device.numeric_id` hasn't resolved yet (registration hasn't completed) — `GET /hydro/status` is scoped by `?device_id=<numeric_id>` and is skipped entirely until that id exists. |
| A specific light never responds to backend commands, others do | Its backend actuator row's `pin`/`port` doesn't match any `config.TYPE_TO_GPIO` descriptor (e.g. the row was created manually on the dashboard, or `config.py`'s pin map drifted from what's actually registered) — `_extract_commands()` silently skips rows it can't map rather than guessing. Check `GET /actuators/device/{numeric_id}` for the actual registered pins. |
| OLED stays blank | Non-fatal — check serial log for `[oled] display not available, continuing without it: ...` and verify I2C wiring/address. |
| `gpio_manager: no config.GPIO_EXPANDERS entry for unit ...` | A `TYPE_TO_GPIO`/`DOOR_OPEN_PIN`/`DOOR_CLOSE_PIN` value uses `"mcp:<unit>:..."` but that unit id isn't in `config.GPIO_EXPANDERS` — add it or fix the typo. |
| `gpio_manager: pin ... is on a GPIO expander, which has no PWM peripheral` | A `TYPE_TO_HARDWARE = "mosfet"` entry points at an `"mcp:..."` descriptor — expander pins are digital-only; move that channel to a native GPIO. |

## Scaling This Project

- **Add a 7th light**: add one line to `TYPE_TO_GPIO` (and
  `TYPE_TO_HARDWARE`) in `config.py` — a native GPIO number if you have
  one free, or an expander descriptor (`"mcp:0:0"`) if you don't. See
  **GPIO Expansion** above. Nothing else changes — `actuators.py` and
  `relay.py` build the channel automatically either way.
- **Add another door / curtain**: add a new `"door"`-hardware entry in
  `config.TYPE_TO_HARDWARE` and wire up its own open/close pins,
  following `DoorChannel`'s interlocked pattern.
- **Run out of native GPIOs**: add an MCP23017 (or another chip — see
  **GPIO Expansion**) instead of re-partitioning existing wiring.
- **Add sensors**: set `config.ENABLE_SENSORS = True` (and optionally
  `DHT11_PIN` / `EC_PPM_ADC_PIN`), wire the hardware, and
  `control.py`'s tick will start pushing `sensors.read_all()` results
  (remapped to the backend's `temperature`/`humidity`/`ppm` keys)
  automatically.
- **Multiple devices**: since `device_id.py` derives a unique ID per
  board automatically, this exact codebase can be flashed to every
  controller in the building — only `secrets.py`'s credentials and
  `config.py`'s `DEVICE_LOCATION` (and `GPIO_EXPANDERS`/pin map, if a
  particular board has different hardware) differ per unit.
