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
  `DEVICE_LOCATION` once per boot. Also resolves each actuator's backend
  numeric id (`actuator_ids_by_type`), used to tag flow readings and
  reported-state acknowledgements.
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
  Builds every pin through `gpio_manager.py`. Whenever a channel's
  hardware state actually changes (or a pin write fails) it queues an
  acknowledgement in `config.PENDING_STATE_REPORTS`.
- **`actuators.py`**: Maps relay channels to logical actuator types
  (`light_1`...`light_6`, `sliding_door`), builds the backend
  registration payload, and executes commands coming back from the
  backend.
- **`control.py`**: Polls `GET /hydro/status?device_id=<numeric_id>` (scoped to
  this device, otherwise the response is too large for the ESP32 heap) and
  executes the desired actuator state. Pushes sensor telemetry to
  `POST /sensor/data` and flow readings to `POST /hydro/flow-readings`.
  Delivers reported-state acknowledgements via `report_pending_states()`
  (`POST /hydro/actuator/{id}/reported-state`). Polling doubles as the
  heartbeat. Backs off on repeated 401s.
- **`scheduler.py`**: Fetches each actuator's schedules from
  `GET /hydro/config`, caches them in flash (`schedule.json`), and runs them
  locally when the backend or WiFi is unreachable. Also owns async WiFi
  reconnect, the backend reachability probe and NTP sync. `main.py` is now a
  `uasyncio` app.
- **`flow_sensor.py`**: Pulse-counting flow sensors (YF-S201 style) on native
  GPIOs, reported per pump.
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
  never be energized simultaneously. If a door pin write fails midway,
  both pins are driven de-energized before the error is re-raised.
- Re-commanding a channel to the state it's already in is a no-op —
  this prevents a backend that re-sends the same desired state every
  poll cycle from continually resetting the safety-sweep timer (and from
  flooding the backend with identical reported-state acknowledgements).
- Any unhandled exception in the main loop forces all relays off
  (`actuators.all_off()`) and continues; a fatal error at the top level
  forces all relays off and resets the board (see `main.py`).
- Offline mode never touches the sliding door. If the safety sweep force-offs a
  channel, the scheduler won't re-energize it until its schedule window ends.
  The scheduler only runs with a valid (NTP-synced) clock.
- Backend rows with `is_active: false` are forced OFF. A channel whose row
  disappears from `/hydro/status` is forced OFF on the next poll. The sliding
  door is exempt from both rules (only `stop` and `DOOR_MAX_RUN_S` act on it).


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
    re-creates duplicate rows for the same device. Also used to resolve
    each actuator's numeric id (Device.actuator_ids_by_type).

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
  → REQUIRED scope. Unscoped, the backend returns every device with full
    automation blocks (~40 KB) and the ESP32 runs out of memory parsing it.
    Polled every config.SEND_INTERVAL (10s). Desired state = manual_state if
    not null, else current_state. is_active=false forces OFF.
    pending_command "stop" is fire-once. Rows are matched to local channels
    by `pin` only (never type/name). A non-matching row is skipped and logged.
  → If still too large, set config.STATUS_QUERY_EXTRA (e.g. "&compact=1")
    once the backend offers a slim variant.

POST /hydro/actuator/{actuator_id}/reported-state
  body: { device_id (NUMERIC), reported_state (bool), error (str|null) }
  → actuator_id is the backend's NUMERIC actuator id (from
    Device.actuator_ids_by_type), not the local type string or a pin.
    Acknowledges what the hardware actually applied after a command, so
    the backend can distinguish "commanded ON" from "really switched ON"
    (or failed to). URL base: config.REPORTED_STATE_URL.
    200/201 (any 2xx) → entry removed; 404/422 → entry dropped with a log
    line (retrying can't help); anything else → kept and retried.
    See "Reported-State Acknowledgements" below.

GET  /hydro/config?device_id=<DEVICE_CODE>
  → { actuators: [{ id, type, pin, port, schedules: [{start "HH:MM", end,
    days "mon,tue,...", on_min, off_min}] }] }
    Polled every SCHEDULE_SYNC_S while the backend is reachable. Rows are
    matched by pin/port.

POST /hydro/flow-readings
  body: { actuator_id (numeric backend id), flow_rate (L/min) }

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
| `device_id` (numeric) | `POST /actuators/bulk` body, `GET /hydro/status` query param, `POST .../reported-state` body | the `id` returned from `POST /hydro/devices` |
| `actuator_id` (numeric) | `POST /hydro/flow-readings` body, `/hydro/actuator/{actuator_id}/reported-state` URL | the actuator row's `id` (`Device.actuator_ids_by_type`) |

Valid `actuator_id` / desired-state pairs the firmware understands:

| actuator_id | supported actions |
|---|---|
| `light_1` … `light_6` | `on`, `off`, `toggle` |
| `sliding_door` | `on` (up), `off` (down), `toggle`, `stop` (HOLD mode only) |

## Reported-State Acknowledgements (`control.report_pending_states`)

The backend decides the *desired* state (via `/hydro/status`); the ESP32
confirms the *actual* state it applied to the GPIO pin. This closes the
loop so the backend can tell "commanded ON" from "hardware really
switched ON" (or failed to).

**Flow:**

1. `relay.py` queues a result in `config.PENDING_STATE_REPORTS`, keyed by
   **actuator type** (`"light_3"`, `"sliding_door"`, ...), only when a
   channel's state actually **changes** (re-commanding the same state is
   a no-op and queues nothing):

   ```python
   {"reported_state": True, "error": None}
   ```

   - Relays: `reported_state` is the logical ON/OFF state that was written
     (active-low is already accounted for).
   - PWM/mosfet channels: `reported_state` is `speed > 0`.
   - Door: `reported_state` is the commanded logical position
     (`True` = open). `stop()` and the end of a PULSE-mode pulse do **not**
     queue a report — they release the pins without asserting a position.
   - If a pin write raises, the entry becomes `reported_state: False` with
     `error: "GPIO write failed: ..."` (or `"PWM write failed: ..."`), and
     the exception still propagates as before (the channel's logical state
     is left unchanged so the next command retries the write).
   - A safety-sweep force-off of a relay queues `reported_state: False`, so
     the backend learns the hardware no longer matches what it commanded.
   - Boot: each relay's initial OFF is queued too, but it coalesces with
     whatever the first `/hydro/status` poll commands.
2. The queue is a **dict keyed by actuator type**, so rapid changes while
   offline coalesce — only the *latest* applied state per channel is
   reported after reconnect (the scheduler's offline changes are flushed
   the same way).
3. `ControlLoop.tick()` calls `report_pending_states()` right after the
   status poll (and every 200 ms tick, but it's a no-op when nothing is
   queued). It only runs while the backend is reachable, because
   `main.py` only calls `tick()` when the probe passes. For each entry:
   - Looks up the backend actuator id in `device.actuator_ids_by_type`.
     If it isn't known yet, the entry stays pending (warned once per type).
   - `POST {config.REPORTED_STATE_URL}/{actuator_id}/reported-state`
     (i.e. `/hydro/actuator/{actuator_id}/reported-state`) with:

     ```json
     {"device_id": <numeric device id>, "reported_state": true, "error": null}
     ```
   - **2xx** → entry removed (unless a newer state was queued meanwhile).
   - **401** → `_request()` re-logs-in once and retries; if that fails the
     existing 30 s → 10 min auth backoff starts and entries stay pending.
   - **404 / 422** → logged and **dropped** (a stale actuator id after a DB
     wipe, or a payload the backend rejects, can never succeed on retry).
   - **Any other status or exception** → logged; entries stay pending, the
     sweep stops, and the next attempt waits `config.RETRY_DELAY` (5 s), so
     a dead backend costs one blocking request per window, not one per
     entry.

Delivery is at-least-once and latest-state-wins.

> `device_id` in the body is the **numeric** id from `POST /hydro/devices`,
> not `config.DEVICE_CODE`.

## Setup

1. Flash MicroPython to the ESP32-WROOM (esptool + the official
   MicroPython `.bin` for your board).
2. Copy all files in this folder to the board's filesystem (e.g. via
   `mpremote cp *.py :` or `ampy`, or Thonny's file browser). You will
   need MicroPython's `urequests` and `ujson` modules available on the
   device if they aren't already part of your firmware build.
3. Create `secrets.py` with `WIFI_SSID`, `WIFI_PASSWORD`, `AUTH_USERNAME`
   and `AUTH_PASSWORD` — the WiFi and backend login credentials for this
   device. This file is gitignored; never commit it.
4. Edit `config.py`:
   - `FASTAPI_URL` to point at your backend.
   - `DEVICE_LOCATION` — set per physical board before flashing (this
     codebase is shared across every controller; see **Scaling**
     below).
   - Confirm `DOOR_MODE` matches what your physical door operator
     actually needs (see **Hardware / Wiring** above).
   - `REPORTED_STATE_URL` (already `FASTAPI_URL + "/hydro/actuator"`) must
     match the backend's reported-state route base.
   - Optionally add `PENDING_STATE_REPORTS = {}` in the runtime-state
     section (next to `ACTUATOR_STATES`). `relay.py` creates it
     automatically if it's missing, so this is for documentation only.
   - Only if you're adding a GPIO expander: `GPIO_EXPANDERS` and any
     `"mcp:..."` descriptors in `TYPE_TO_GPIO` — see **GPIO Expansion**
     above. Leave `GPIO_EXPANDERS = {}` and every pin as a plain GPIO
     number if you're not using one; nothing else changes.
5. Reset the board. `main.py` runs automatically; watch the serial
   console (115200 baud) for boot/login/registration logs.

### Security note

WiFi `SSID` / `PASSWORD` now come from `secrets.py` (`WIFI_SSID`,
`WIFI_PASSWORD`, imported by `config.py` together with `AUTH_USERNAME` /
`AUTH_PASSWORD`). `secrets.py` is gitignored — never commit it. Don't paste
JWTs or passwords into issues/chats either; a leaked token stays valid
until it expires (default 30 days) unless the backend's JWT secret or the
user's password is rotated.


## Operating the Device

Day-to-day control does **not** happen on the ESP32 itself — it happens
through whatever dashboard/app talks to the FastAPI backend. The
firmware is a dumb executor: it polls `GET /hydro/status` every
`config.SEND_INTERVAL` (10s) and does whatever the backend says, then
acknowledges what it applied via `reported-state`.

- **Turn a light on/off**: set that light's state (manual override or
  automation) on the backend/dashboard. The ESP32 picks it up on its
  next poll (worst case ~10s later) and acknowledges it right after.
- **Open/close the door**: same — command `sliding_door` via the
  backend. In `HOLD` mode the relay stays energized for the whole
  travel; send `stop` to interrupt it mid-travel, or wait for the
  `DOOR_MAX_RUN_S` (20s) safety timeout to force it off automatically.
- **Switch to local automation**: flip `config.AUTO_MODE["enabled"] =
  True`. While enabled, the ESP32 stops polling `/hydro/status`
  entirely (so the backend and local logic never fight over an
  actuator) — you'd add your own rules where `main.py` marks the
  placeholder (step 4 of the main loop).
- **Offline mode**: after `OFFLINE_AFTER_S` (30s) without a good
  `/hydro/status` poll, cached schedules drive any channel that has one.
  Backend manual overrides are ignored until it reconnects. State changes
  made while offline are acknowledged to the backend after reconnect
  (latest state per channel).
- **401 handling**: one automatic re-login. If that fails, status polls and
  telemetry back off 30s, doubling up to 10 min.
- **Silent no-ops**: `[control] executed ...` only prints when a state
  actually changes. No log line for an unchanged channel is normal.
- **Monitor what's happening**: connect over serial at 115200 baud.
  Every login, registration, command execution, and safety-sweep trip
  is logged with a `[module]` prefix (`[wifi]`, `[auth]`, `[device]`,
  `[control]`, `[actuators]`, `[sched]`, `[oled]`), which is the fastest way to
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
risking hardware. Have a qualified electrician verify the mains side.

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
| `[control] poll_status out of memory` | Response too large. Confirm the poll is scoped (`?device_id=`). Otherwise use `STATUS_QUERY_EXTRA` with a slim backend mode. |
| `[control] poll_status failed: [Errno 116] ETIMEDOUT` | The request didn't complete within `HTTP_TIMEOUT_S` (8s): backend slow/busy, or weak/flapping WiFi. Occasional one-offs are harmless (next poll recovers). If it flips the device OFFLINE repeatedly, raise `HTTP_TIMEOUT_S` / `OFFLINE_AFTER_S`, or try `wlan.config(pm=network.WLAN.PM_NONE)`. |
| `[control] auth rejected, backing off Ns` | Login/credentials failing. Check `secrets.py` and that the backend user exists. |
| `[control] unresolved status row id=… pin=…` | The row's pin isn't in `TYPE_TO_GPIO`. Check `GET /actuators/device/{id}`. |
| `[control] no backend id for '<type>' yet, keeping its reported state pending` | Actuator ids not resolved (`Device._resolve_actuator_ids()` failed or registration incomplete). Acknowledgements for that channel wait until ids resolve; check `[device] resolved actuator ids:` at boot. |
| `[control] reported-state for <type> failed, HTTP …` | Backend rejected/failed the acknowledgement; it is retried every `RETRY_DELAY` seconds. Check the backend log for the `reported-state` route. |
| `[control] reported-state for <type> rejected (HTTP 404/422), dropping` | Stale actuator id (DB wiped / actuator recreated), or the backend's schema differs from `{device_id, reported_state, error}`. Reboot to re-resolve ids; compare the body with the backend's schema. |
| `[sched] mode -> OFFLINE` while backend is up | No good status poll within `OFFLINE_AFTER_S`. See the poll errors above it. |
| `[sched] clock not set yet` | No NTP sync since boot. Check WiFi/DNS or fit a DS3231 RTC. |
| `[sched] backend actuator … not on this board` | Row's pin/port doesn't match `TYPE_TO_GPIO`. |

## Scaling This Project

- **Add a 7th light**: add one line to `TYPE_TO_GPIO` (and
  `TYPE_TO_HARDWARE`) in `config.py` — a native GPIO number if you have
  one free, or an expander descriptor (`"mcp:0:0"`) if you don't. See
  **GPIO Expansion** above. Nothing else changes — `actuators.py` and
  `relay.py` build the channel automatically either way (including its
  reported-state acknowledgements).
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

## Optional `config.py` additions (all have defaults)

```python
TZ_OFFSET_S = 7 * 3600        # schedule times are local wall-clock (Vietnam, UTC+7)
OFFLINE_AFTER_S = 30          # no good /hydro/status for this long -> offline mode
SCHEDULE_FILE = "schedule.json"
CONFIG_URL = FASTAPI_URL + "/hydro/config"   # GET ?device_id=<DEVICE_CODE>
SCHEDULE_SYNC_S = 60          # how often to refresh the cached schedule when online
WDT_TIMEOUT_MS = None         # e.g. 30000 in production; None while developing
HTTP_TIMEOUT_S = 8            # raise (e.g. 15) if the backend is slow
RETRY_DELAY = 5               # seconds between reported-state retries after a failure
REPORTED_STATE_URL = FASTAPI_URL + "/hydro/actuator"   # POST .../{actuator_id}/reported-state
PENDING_STATE_REPORTS = {}    # runtime queue; relay.py creates it if missing
```