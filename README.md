# ESP32 IoT Relay Controller — 6 Lights + Sliding Door (220V)

MicroPython firmware for an ESP32-WROOM devkit that controls 6 lighting
circuits and one sliding door motor (all 220V AC loads switched through
relay modules), coordinated by an existing FastAPI "hydro" backend.

The firmware is offline-capable: it caches each actuator's schedules from
the backend in flash and runs them locally when WiFi or the backend is
unreachable.

## Folder Structure

- **`main.py`**: Entry point, a `uasyncio` app. Builds the hardware layers
  (all actuators start OFF), loads the cached schedule, then runs these tasks
  under a supervisor that forces actuators off and restarts a task if it
  crashes:
  - `scheduler.run`: executes the cached schedule while offline
  - `scheduler.wifi_task`: non-blocking WiFi reconnect with backoff
  - `scheduler.probe_task`: non-blocking "is the backend reachable?" check
  - `scheduler.sync_task`: refreshes the cached schedule from `/hydro/config`
  - `scheduler.ntp_task`: clock sync (needed for schedules)
  - `io_loop`: backend registration + polling (only when the backend is
    reachable), relay safety sweep, door pulse servicing, OLED refresh
- **`config.py`**: Centralized configuration: device identity, WiFi, backend
  URLs, GPIO/pin map, GPIO-expander definitions, door mode, sensor and
  flow-sensor pins, timing constants, offline-scheduler settings. Imports
  `WIFI_SSID`, `WIFI_PASSWORD`, `AUTH_USERNAME` and `AUTH_PASSWORD` from
  `secrets.py` (see **Secrets** below).
- **`secrets.py`**: Private WiFi and backend login credentials. **Do not
  commit this file**; it is covered by `.gitignore`.
- **`device_id.py`**: Derives a stable device ID (`esp32-<chip id hex>`) from
  the ESP32's hardware unique ID. Never changes across reboots/reflashes for
  the same physical board.
- **`auth.py`**: Logs in against `POST /auth/login` (OAuth2 form-encoded
  username/password) and keeps `config.HEADERS` populated with a fresh Bearer
  JWT. Re-logs-in transparently on any `401`.
- **`device.py`**: Registers the device and its actuator list with the
  backend, resolves the backend's numeric device PK and each actuator's
  numeric id (used to tag flow readings and reported-state acks), and syncs
  `DEVICE_LOCATION` once per boot.
- **`wifi.py`**: WiFi connect (blocking and async) and reconnect with backoff.
- **`scheduler.py`**: Fetches each actuator's schedules from
  `GET /hydro/config`, caches them in flash (`schedule.json`), and runs them
  locally when the backend or WiFi is unreachable. Also owns the async WiFi
  reconnect, the backend reachability probe and NTP sync.
- **`gpio_manager.py`**: Turns a *pin descriptor string* (native GPIO number,
  or `"mcp:<unit>:<pin>"` for an I2C expander pin) into an actual pin object
  with a `machine.Pin`-shaped API. `relay.py` and `actuators.py` go through
  this instead of touching `machine.Pin`/`PWM` or an expander driver
  directly. See **GPIO Expansion** below.
- **`mcp23017.py`**: Driver for the MCP23017 16-bit I2C GPIO expander
  (digital in/out, pull-ups, optional interrupt-on-change). Used only when a
  `"mcp:..."` descriptor is configured.
- **`relay.py`**: Hardware abstraction for every actuator channel:
  active-low relay logic, per-channel safety timeout, and the interlocked
  `DoorChannel` for the sliding door (PULSE or HOLD mode). Builds every pin
  through `gpio_manager.py`.
- **`actuators.py`**: Maps relay channels to logical actuator types
  (`light_1`...`light_6`, `sliding_door`), builds the backend registration
  payload, resolves backend rows to local channels by pin/port, and executes
  commands.
- **`control.py`**: Polls `GET /hydro/status?device_id=<numeric_id>` (scoped to
  this device, otherwise the response is too large for the ESP32 heap) and
  executes the desired actuator state. Acknowledges the *real* channel state
  to `POST /hydro/actuator/{id}/reported-state`. Pushes sensor telemetry to
  `POST /sensor/data` and flow readings to `POST /hydro/flow-readings`.
  Polling doubles as the heartbeat. Backs off on repeated 401s.
- **`sensors.py`**: Optional DHT11 (temperature/humidity), analog EC/PPM
  probe and YL-83 rain sensor readers, feeding `control.py`'s sensor push.
  Disabled unless `config.ENABLE_SENSORS` is `True`.
- **`flow_sensor.py`**: Pulse-counting flow sensors (YF-S201 style) on native
  GPIOs, reported per actuator type. Disabled unless
  `config.FLOW_SENSOR_ENABLED` is `True`.
- **`oled_display.py` / `ssd1306.py`**: Local I2C OLED status screen
  (WiFi/backend/actuator/mode state). Optional: the firmware runs fine with no
  display attached; drawing is skipped safely if init fails.
- **`.gitignore`**: Ignores `secrets.py`, `__pycache__/`, `*.pyc` and
  `main-ex.py`.

## Hardware / Wiring

⚠️ **All loads here are 220V AC. This is not a beginner electronics
project: mains wiring must be done by, or reviewed by, a qualified
electrician. The ESP32 only ever switches the low-voltage side of an
opto-isolated relay module; it never touches mains directly.**

Use relay modules (or contactors driven by relay modules) rated for
your actual load current, with opto-isolation between the ESP32 GPIO
side and the relay coil/mains side. For the sliding door motor, most
installers use a small AC contactor for the motor itself, triggered by
the relay module. Don't switch a motor directly off a 5V/10A relay
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
| DHT11            | 4    | Only if `ENABLE_SENSORS` |
| EC/PPM analog    | 35   | Input-only ADC pin; only if `ENABLE_SENSORS` |
| Rain AO (YL-83)  | 32   | ⚠️ See pin conflict warning below |
| Rain DO (YL-83)  | 33   | ⚠️ See pin conflict warning below |
| Flow sensor      | 34   | `water_pump` pulse input (`FLOW_SENSOR_PINS`); input-only pin |

⚠️ **Pin conflict with the current `config.py` defaults.** With
`ENABLE_SENSORS = True`, the rain sensor pins (`RAIN_SENSOR_ADC_PIN = 32`,
`RAIN_SENSOR_DIGITAL_PIN = 33`) are the same GPIOs as **Door – OPEN (32)** and
**Light 6 (33)**. Both can't be wired to the same pin. Move the rain sensor to
free pins (or disable sensors) in `config.py` before connecting hardware, and
have an electrician verify the final wiring.

Pins 34–39 (input-only) and the flash/strapping pins (0, 2\*, 6–12, 15)
are deliberately avoided for relay outputs.

If you later add an MCP23017 GPIO expander (see **GPIO Expansion**
below), it shares the same I2C bus as the OLED (SDA 21 / SCL 22) by
default. I2C is a shared bus, so this is fine as long as each device has a
distinct address (OLED 0x3C, MCP23017 0x20–0x27 via its A0–A2 pins). Use a
separate `i2c_id`/pins in `config.GPIO_EXPANDERS` if you'd rather put it on
its own bus.

Door mode is configurable in `config.py` via `DOOR_MODE`:
- **`"HOLD"` (current default)**: the OPEN/CLOSE relay stays energized
  for the whole travel time, until an explicit `stop` command or the
  `DOOR_MAX_RUN_S` (20s) safety timeout trips. **Requires your door
  operator to accept continuous drive power while traveling, with its
  own end-limit switches.** Confirm this matches your physical motor
  controller before flashing. This mode was chosen over `"PULSE"`
  specifically so a backend `stop` command can actually interrupt a
  still-energized relay.
- `"PULSE"`: energizes the OPEN/CLOSE relay for `DOOR_PULSE_S` seconds
  (default 2s) then auto-releases (non-blocking: handled by
  `RelayManager.service_pulses()` each loop tick, not a blocking
  `sleep`). Matches commercial operators that accept a momentary
  trigger, but a `stop` command sent after the pulse has already
  self-released is a no-op. Use `HOLD` if you need `stop` to be
  meaningful.

## GPIO Expansion (`gpio_manager.py` / `mcp23017.py`)

Every pin used by `relay.py` (each light, and the door's OPEN/CLOSE
pins) is a **pin descriptor string**, not a raw GPIO number, resolved
through `gpio_manager.py`:

- `"13"` → native ESP32 GPIO 13 (every existing `config.py` value uses
  this form).
- `"mcp:0:5"` → pin 5 of the I2C GPIO expander registered as unit `"0"`
  in `config.GPIO_EXPANDERS`.

`relay.py` and `actuators.py` never import `machine.Pin`/`PWM` or an
expander driver directly, so adding capacity or changing hardware never
touches the actuator logic:

- **Add a 7th light on an MCP23017 expander**:
  1. In `config.py`, add an entry to `GPIO_EXPANDERS`, e.g.
     `"0": {"driver": "mcp23017", "i2c_id": 0, "scl": 22, "sda": 21, "addr": 0x20}`.
  2. Add `"light_7": "mcp:0:0"` to `TYPE_TO_GPIO` and
     `"light_7": "relay"` to `TYPE_TO_HARDWARE`.
  3. Done. `relay.py` builds a `RelayChannel` on that expander pin the
     same way it builds one on a native GPIO.
- **Add a different expander chip** (PCF8574, MCP23008, TCA9555, ...):
  write its driver with the same `.pin(n, mode, pull)` shape as
  `mcp23017.py`, register a builder in `gpio_manager._BACKEND_FACTORIES`
  under its own `driver` name, and give it its own descriptor prefix in
  `gpio_manager._parse()` (e.g. `"pcf:0:3"`).

**Hardware limitations on expander pins:**
- **No PWM.** `gpio_manager.get_pwm_pin()` raises a clear error at boot if a
  `TYPE_TO_HARDWARE = "mosfet"` entry points at an expander pin. Keep
  PWM-capable actuators on native GPIOs.
- **No flow sensors.** Pulse counting needs native interrupt-capable GPIOs;
  `flow_sensor.init()` skips any `"mcp:..."` descriptor.

Bench-test any new expander-backed channel the same way you would a
new relay: confirm the pin toggles correctly with the OLED/serial log
before wiring it to a live 220V load, and get an electrician's sign-off
on the mains side.

## Safety Design

- Every relay/door channel boots into the OFF/closed state
  (`RelayChannel.__init__` / `DoorChannel.__init__` force `set(False)`
  before anything else runs), whether the channel sits on a native GPIO or
  a GPIO-expander pin.
- `RelayManager.safety_sweep()` runs every `io_loop` iteration and
  force-offs (or `stop()`s, for the door) any channel that has exceeded its
  max on-time. This protects against a stuck command, a crashed backend, or a
  dropped connection leaving a light or the door motor energized
  indefinitely. Default ceiling: 12h for lights
  (`DEFAULT_MAX_ON_TIME_S`), 20s (`DOOR_MAX_RUN_S`) for the door in HOLD
  mode.
- The door's OPEN and CLOSE channels are interlocked in software
  (`DoorChannel.set`): the opposite channel is always switched off before
  the target one is switched on, so the two are never energized
  simultaneously.
- Re-commanding a channel to the state it's already in is a no-op. This
  prevents a backend that re-sends the same desired state every poll cycle
  from continually resetting the safety-sweep timer.
- Any unhandled exception in `io_loop` forces all relays off
  (`actuators.all_off()`) and continues. A crashed task is restarted by
  `supervise()` after forcing all relays off. A fatal error at the top level
  forces all relays off and resets the board (see `main.py`).
- Offline mode never touches the sliding door. If the safety sweep
  force-offs a channel, the scheduler won't re-energize it until its schedule
  window ends. The scheduler only runs with a valid (NTP-synced) clock.
- Backend rows with `is_active: false` are forced OFF. A channel whose row
  disappears from `/hydro/status` is forced OFF on the next poll. The sliding
  door is exempt from both rules (only `stop` and `DOOR_MAX_RUN_S` act on
  it).
- Reported-state acks only *report* what a channel is doing; they never
  change it. A lost or rejected ack can't energize anything.
- `WDT_TIMEOUT_MS` in `config.py` enables the hardware watchdog (fed by
  `scheduler.run`). It is `None` (disabled) during development; set it (e.g.
  `30000`) in production.

## Offline Scheduler

- A background task fetches `GET /hydro/config?device_id=<DEVICE_CODE>`
  whenever the backend is reachable and caches a compact form of each
  actuator's `schedules` in flash (`schedule.json`). Unchanged schedules are
  not rewritten, to avoid flash wear.
- Backend rows are matched to local channels by `pin`/`port`, never by
  `type`/`name`. Non-matching rows are skipped with a log line.
- Evaluation is level-based and stateless: every second the scheduler
  computes "should this channel be on right now?" from the wall clock
  (`TZ_OFFSET_S`, default UTC+7). Reboots, missed ticks and NTP jumps can't
  desynchronise it. Windows that wrap midnight and `on_min`/`off_min`
  intervals are supported.
- **Online** (a good `/hydro/status` poll within `OFFLINE_AFTER_S`, default
  30s): the backend drives the actuators.
- **Offline**: the scheduler drives every channel that has a cached
  schedule. Channels without a schedule are left alone (the 12h safety sweep
  still applies). Manual overrides can't arrive while offline.
- Reported-state acks can't be sent while offline. On reconnect, the next
  status interval acks every channel whose real state changed in the
  meantime (including schedule-driven changes).
- Needs a valid clock: no schedule is executed until NTP has synced since
  boot.

## Backend API Contract (FastAPI "hydro" backend)

Base URL: `config.FASTAPI_URL`. All authenticated requests carry
`Authorization: Bearer <JWT>` (obtained via login, see `auth.py`) in
`config.HEADERS`.

```
POST /auth/login
  body (form-urlencoded): username, password
  response: { access_token, token_type }
  → JWT stored in config.HEADERS; re-login happens automatically on any 401.

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
  → checked before bulk-registering actuators (so a reboot never re-creates
    duplicate rows), and re-fetched afterwards to resolve each actuator's
    numeric id for flow readings and reported-state acks.

POST /actuators/bulk
  body (list of HydroActuatorCreate): [
    { actuator_id, type, name, pin, port, is_active, default_state,
      device_id, sensor_key, manual_state }, ...
  ]
  → CREATE-ONLY, not an upsert. device_id here is the NUMERIC PK from
    the device-registration step, not the string DEVICE_CODE.
    Called at most once per boot, only if GET /actuators/device/{id}
    came back empty. Do not call it repeatedly: it creates duplicate rows.
  → `port` (Integer) is the real GPIO number for native pins, or a synthetic
    `9000 + unit*16 + pin` for expander pins (never collides with a real
    ESP32 GPIO 0-39). `pin` (String) always carries the human-readable
    descriptor ("13", "mcp:0:5", or "32,23" for the door).

GET  /hydro/status?device_id={numeric_id}
  → REQUIRED scope. Unscoped, the backend returns every device with full
    automation blocks (~40 KB) and the ESP32 runs out of memory parsing it.
    Polled every config.SEND_INTERVAL (10s). Desired state = manual_state if
    not null, else current_state. is_active=false forces OFF.
    pending_command "stop" is fire-once and overrides on/off for that cycle.
    Rows are matched to local channels by `pin` only (never type/name). A
    non-matching row is skipped and logged.
  → If still too large, set config.STATUS_QUERY_EXTRA (e.g. "&compact=1")
    once the backend offers a slim variant.

GET  /hydro/config?device_id=<DEVICE_CODE>
  → { actuators: [{ id, type, pin, port, schedules: [{start "HH:MM", end,
    days "mon,tue,...", on_min, off_min}] }] }
    Polled every SCHEDULE_SYNC_S while the backend is reachable.

POST /hydro/actuator/{actuator_id}/reported-state
  auth: Bearer JWT
  path: actuator_id = the backend's NUMERIC actuator id
        (Device.actuator_ids_by_type, resolved after registration)
  body: { device_id (NUMERIC device PK), reported_state (bool),
          error (str|null, max 500) }
  → Does NOT change the desired state; it only fills reported_state* so the
    dashboard can show desired vs actual.
  → 400 "Actuator does not belong to device" if device_id doesn't match.
  → Sent on boot/registration (all channels), whenever a channel's real state
    differs from the last state the backend accepted (backend command,
    safety-sweep trip, offline scheduler, reconnect catch-up), and with
    `error` set when a command could not be applied.
  → reported_state for the sliding door: true = open/up, false = closed/down.
    A `stop` doesn't change it (stop keeps the stored direction).

POST /hydro/flow-readings
  body: { actuator_id (numeric backend id), flow_rate (L/min) }

POST /sensor/data
  body (SensorDataCreateSchema): {
    device_id, data: { temperature?, humidity?, ppm?, ... }
  }
  → device_id here is the STRING DEVICE_CODE. Readings are nested under
    "data" using the backend's key names (temperature/humidity/ppm);
    control.py remaps sensors.py's temperature_c/humidity_pct/ec_ppm before
    sending. Rain fields (rain_detected, rain_raw, rain_level_pct) are sent
    under their local names. Pushed every config.SEND_INTERVAL if
    config.ENABLE_SENSORS is True. This call also triggers the backend's
    automation_service.run_control_loop, so it drives server-side automatic
    actuator decisions, not just telemetry.
```

**Two ID types, don't mix them up:**

| Field | Where used | Value |
|---|---|---|
| `device_id` (string) | `POST /hydro/devices` body, `POST /sensor/data` body, `GET /hydro/config` query | `DEVICE_CODE` from `device_id.py` |
| `device_id` (numeric) | `POST /actuators/bulk` body, `POST /hydro/actuator/{id}/reported-state` body, `GET /hydro/status` query param | the `id` returned from `POST /hydro/devices` |

**401 handling:** one automatic re-login and retry. If that fails, status
polls and telemetry back off 30s, doubling up to 10 min.

Valid `actuator_id` / action pairs the firmware understands:

| actuator_id | supported actions |
|---|---|
| `light_1` … `light_6` | `on`, `off`, `toggle` |
| `sliding_door` | `on` (open), `off` (close), `toggle`, `stop` |

Door `stop` is only meaningful in `HOLD` mode.

### Backend compatibility notes

The backend's ESP32 integration guide differs from this firmware in two
places. Neither breaks acks, but both can affect backend features:

- **Actuator types.** The guide says to keep lights as `type="light"`
  (distinguished by `name`/`group_name`) and not to invent types like
  `light_1`; unsupported types are ignored by automation and never match
  recipes. This firmware registers `light_1`…`light_6`, so backend
  automation, recipes and schedules may not apply to them (the status rows
  stay at `sensor_off`). Manual commands and cached schedules still work
  because the firmware matches rows by `pin`/`port`. If you want backend
  recipes to drive the lights, register them as `type="light"` with distinct
  `name`/`group_name` values; the firmware's pin-based matching already
  supports this, but local `TYPE_TO_GPIO` keys would still be `light_N`.
- **`pin` format.** The guide expects plain digits parseable as an int. The
  door registers `pin: "32,23"` (open,close). The firmware matches by string
  so it works, but a dashboard or backend code that does `int(pin)` on that
  row would fail.

## Setup

1. Flash MicroPython to the ESP32-WROOM (esptool + the official
   MicroPython `.bin` for your board).
2. Copy all files in this folder to the board's filesystem (e.g. via
   `mpremote cp *.py :`, `ampy`, or Thonny's file browser). You need
   MicroPython's `urequests` and `ujson` modules if they aren't already part
   of your firmware build.
3. Create `secrets.py` (see **Secrets**).
4. Edit `config.py`:
   - `FASTAPI_URL` to point at your backend.
   - `DEVICE_LOCATION`: set per physical board before flashing (this
     codebase is shared across every controller; see **Scaling**).
   - Confirm `DOOR_MODE` matches what your physical door operator
     needs (see **Hardware / Wiring**).
   - Check sensor/flow-sensor pins against your wiring (see the pin conflict
     warning above), or set `ENABLE_SENSORS` / `FLOW_SENSOR_ENABLED` to
     `False`.
   - Only if you're adding a GPIO expander: `GPIO_EXPANDERS` and any
     `"mcp:..."` descriptors in `TYPE_TO_GPIO`. Leave `GPIO_EXPANDERS = {}`
     and every pin as a plain GPIO number if you're not using one.
5. Reset the board. `main.py` runs automatically; watch the serial console
   (115200 baud) for boot/login/registration logs.

### Secrets

`secrets.py` holds everything private and is gitignored:

```python
WIFI_SSID = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"
AUTH_USERNAME = "device-user-on-backend"
AUTH_PASSWORD = "device-user-password"
```

`config.py` imports these four names. Never commit this file. Keep a
non-secret template (e.g. `secrets.example.py`) in version control if you
want one.

### Optional `config.py` names (all have defaults)

| Name | Default | Purpose |
|---|---|---|
| `HTTP_TIMEOUT_S` | 8 | Request timeout (lower to ~4 so a half-dead backend blocks the event loop less) |
| `STATUS_QUERY_EXTRA` | `""` | Extra query string for the status URL, e.g. `"&compact=1"` |
| `REPORTED_STATE_URL` | `FASTAPI_URL + "/hydro/actuator"` | Base URL for reported-state acks |
| `MAX_ACKS_PER_TICK` | 3 | Max ack POSTs per `SEND_INTERVAL`; keeps the loop responsive |
| `WIFI_CONNECT_TIMEOUT_S` | 15 | WiFi connect timeout |
| `WIFI_RETRY_BACKOFF_S` | `(2, 5, 10, 20, 30)` | WiFi reconnect backoff |
| `DEFAULT_MAX_ON_TIME_S` | 12h | Safety-sweep ceiling for relay channels |
| `OFFLINE_AFTER_S` | 30 (`3 * SEND_INTERVAL` if unset) | No good status poll for this long → offline mode |
| `SCHEDULE_FILE` | `schedule.json` | Flash cache of schedules |
| `SCHEDULE_SYNC_S` | 60 | Schedule refresh interval while online |
| `TZ_OFFSET_S` | `7 * 3600` | Schedule times are local wall-clock (Vietnam, UTC+7) |
| `PROBE_TIMEOUT_S` | 3 | Backend reachability probe timeout |
| `NTP_RESYNC_S` | 6h | NTP resync interval |
| `WDT_TIMEOUT_MS` | `None` | Hardware watchdog (disabled when `None`) |
| `OLED_WIDTH` / `OLED_HEIGHT` / `I2C_SDA_PIN` / `I2C_SCL_PIN` / `OLED_I2C_ADDR` | 128 / 64 / 21 / 22 / 0x3C | OLED wiring |

## Operating the Device

Day-to-day control does **not** happen on the ESP32 itself. It happens
through whatever dashboard/app talks to the FastAPI backend. The firmware
polls `GET /hydro/status` every `config.SEND_INTERVAL` (10s) and does
whatever the backend says.

- **Turn a light on/off**: set that light's state (manual override or
  automation) on the backend/dashboard. The ESP32 picks it up on its next
  poll (worst case ~10s later).
- **Open/close the door**: command `sliding_door` via the backend. In
  `HOLD` mode the relay stays energized for the whole travel; send `stop` to
  interrupt it mid-travel, or wait for the `DOOR_MAX_RUN_S` (20s) safety
  timeout to force it off automatically.
- **Offline mode**: after `OFFLINE_AFTER_S` (30s) without a good
  `/hydro/status` poll, cached schedules drive any channel that has one.
  Backend manual overrides are ignored until it reconnects. The door is never
  automated by the scheduler.
- **Acks**: after each status interval `control.py` compares each channel's
  real state with what the backend last accepted and POSTs only the
  differences. `[control] ack light_1 -> ON` is logged on success. No log
  line means nothing changed (normal). Failed or lost acks retry on the next
  interval. Acks are paused in `AUTO_MODE` and during 401 backoff.
- **Local automation flag**: `config.AUTO_MODE["enabled"] = True` makes
  `control.poll_status()` return nothing, so the ESP32 stops acting on
  backend commands. There is no local rules engine in the current code, so
  enabling it just freezes backend control.
- **Monitor what's happening**: connect over serial at 115200 baud. Every
  login, registration, command execution, ack, schedule change and
  safety-sweep trip is logged with a `[module]` prefix (`[wifi]`, `[auth]`,
  `[device]`, `[control]`, `[sched]`, `[actuators]`, `[flow_sensor]`,
  `[sensors]`, `[oled]`).
- **Silent no-ops**: `[control] executed ...` only prints when a state
  actually changes. No log line for an unchanged channel is normal.
- **Check status at a glance**: the OLED (if attached) refreshes every 2s
  and shows WiFi/IP, backend registration state, how many actuators are
  currently on, and whether you're in AUTO or BACKEND mode.

### Before flashing to a real door motor

Bench-test the door channel in isolation first: wire just the OPEN/CLOSE
relay pins, send `on`/`off`/`stop` commands from the backend, and confirm the
interlock and `DOOR_MAX_RUN_S` timeout behave as expected **before**
connecting the actual motor/contactor. This catches `DOOR_MODE` mismatches
without risking hardware. Have an electrician verify the mains side.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `[auth] login failed, status 422` | Backend expects a form-encoded body, not JSON. Shouldn't happen unless `auth.py` was modified; otherwise check `secrets.py` values. |
| `[control] auth rejected, backing off Ns` | Login/credentials failing. Check `secrets.py` and that the backend user exists. |
| `[device] could not confirm actuator status, skipping bulk create` | `GET /actuators/device/{id}` failed. Actuators won't register until this succeeds on a later boot/reconnect. |
| Actuators registered multiple times | Something is calling `POST /actuators/bulk` outside `device.py`'s guarded path. |
| Door `stop` command has no visible effect | `DOOR_MODE` is `"PULSE"` and the pulse already self-released before `stop` arrived. Switch to `"HOLD"`. |
| `[control] push sensor data rejected, status 422` | Sensor payload doesn't match `SensorDataCreateSchema`. Confirm readings are nested under `"data"` with backend key names. |
| `poll_status()` returns nothing even though the backend has commands queued | `Device.numeric_id` hasn't resolved yet (registration incomplete). `GET /hydro/status` is skipped until that id exists. |
| `[control] poll_status out of memory` | Response too large. Confirm the poll is scoped (`?device_id=`); otherwise use `STATUS_QUERY_EXTRA` with a slim backend mode. |
| `[control] unresolved status row id=… pin=…` / a light never responds while others do | The row's `pin`/`port` doesn't match any `TYPE_TO_GPIO` descriptor (row created manually on the dashboard, or the pin map drifted). Check `GET /actuators/device/{numeric_id}`. |
| `[control] reported-state N rejected, status 400` | `device_id` in the body isn't the numeric DB id, or the actuator id belongs to another device. Check `GET /actuators/device/{id}`. |
| `[control] reported-state N rejected, status 404` | Backend actuator id no longer exists (DB wipe or actuator deleted). Reboot so `device.py` re-resolves ids. |
| Dashboard shows reported state stale or empty | `device.actuator_ids_by_type` is empty (ids not resolved). Look for `[device] could not resolve actuator ids`. Acks start automatically once resolved. |
| Dashboard `reported_state_error` is set | A command failed on the board (e.g. `unsupported action`); the message is the one logged by `actuators.execute`. |
| `[sched] mode -> OFFLINE` while the backend is up | No good status poll within `OFFLINE_AFTER_S`. See the poll errors above it. |
| `[sched] clock not set yet` | No NTP sync since boot. Check WiFi/DNS, or fit a DS3231 RTC. |
| `[sched] backend actuator … not on this board` | Row's `pin`/`port` doesn't match `TYPE_TO_GPIO`. |
| `[flow_sensor] ... is on expander pin ..., skipping` | Flow sensors need a native GPIO. Move the pulse wire off the expander. |
| OLED stays blank | Non-fatal. Check serial for `[oled] display not available, continuing without it: ...` and verify I2C wiring/address. |
| `gpio_manager: no config.GPIO_EXPANDERS entry for unit ...` | A pin descriptor uses `"mcp:<unit>:..."` but that unit isn't in `config.GPIO_EXPANDERS`. Add it or fix the typo. |
| `gpio_manager: pin ... is on a GPIO expander, which has no PWM peripheral` | A `TYPE_TO_HARDWARE = "mosfet"` entry points at an `"mcp:..."` descriptor. Move that channel to a native GPIO. |

## Scaling This Project

- **Add a 7th light**: add one line to `TYPE_TO_GPIO` (and
  `TYPE_TO_HARDWARE`) in `config.py`: a native GPIO number if you have one
  free, or an expander descriptor (`"mcp:0:0"`) if you don't. Nothing else
  changes. Also add the pin to `ACTUATOR_STATES` and re-check the
  **Hardware / Wiring** table for conflicts.
- **Add another door / curtain**: add a new `"door"`-hardware entry in
  `config.TYPE_TO_HARDWARE` and wire up its own open/close pins, following
  `DoorChannel`'s interlocked pattern. Note that `RelayManager` currently
  builds every door channel from the single `DOOR_OPEN_PIN`/`DOOR_CLOSE_PIN`
  pair, so a second door needs a small code change.
- **Run out of native GPIOs**: add an MCP23017 (or another chip) instead of
  re-partitioning existing wiring.
- **Add sensors**: set `config.ENABLE_SENSORS = True` (and optionally
  `DHT11_PIN`, `EC_PPM_ADC_PIN`, `RAIN_SENSOR_ADC_PIN`,
  `RAIN_SENSOR_DIGITAL_PIN`), wire the hardware, and `control.py` will push
  `sensors.read_all()` results automatically.
- **Multiple devices**: `device_id.py` derives a unique ID per board, so this
  exact codebase can be flashed to every controller. Only `secrets.py` and
  `config.py`'s `DEVICE_LOCATION` (and `GPIO_EXPANDERS`/pin map, if a board
  has different hardware) differ per unit.
