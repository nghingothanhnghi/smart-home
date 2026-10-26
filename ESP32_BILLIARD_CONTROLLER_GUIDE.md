# Billiard ESP32 Controller: Backend Matching Guide

How the central billiard ESP32 talks to the FastAPI backend (`app/billiard`). It extends the hydroponic ESP32 pattern and changes nothing in `app/hydro_system`.

> Source of truth: `controller_router.py`, `event_service.py`, `device_service.py`, `device_auth.py`, `schemas/event.py`, `schemas/device.py`, `models/device.py`, `billiard/config.py`.
> Not covered: RS485 framing, display and button drivers (firmware-only; the backend never sees them).

## Implementation status in this repository

The hydro controller remains unchanged. A separate first-pass MicroPython stack
is now available in `billiard_config.py`, `billiard_storage.py`,
`billiard_api.py`, `billiard_controller.py`, and `billiard_main.py`.

Before flashing it:

1. Add `WIFI_SSID`, `WIFI_PASSWORD`, `BILLIARD_DEVICE_ID` and `BILLIARD_DEVICE_KEY` to the untracked `secrets.py`.
2. Set `FASTAPI_URL` and provision the real `TABLE_MAP` in `billiard_config.py`.
3. Implement the RS485/display adapter as `billiard_adapter.py` with `async def run(controller)`;
   it calls `BilliardController` methods (`start_session`, `start_game`, `change_score`,
   `new_game`, `game_end`, `score_reset`, `stop_session`) and is started by `billiard_main.py`.
4. Copy `main_billiard.py` to the billiard board **as `main.py`** (MicroPython only auto-runs
   `main.py`); do not replace the hydro board's `main.py`.
```

The queue writes its sequence counter before appending an event, retains events
until the server's contiguous acknowledgement cursor advances, and records
rejected/conflicting events to `billiard_event_errors.jsonl` for an operator.

---

## 1. Hydro vs billiard: same shape, different rules

| Topic | Hydro ESP32 (running today) | Billiard ESP32 (this guide) |
| --- | --- | --- |
| Count | One ESP32 per zone/device | **One central ESP32** for all tables |
| Identity | `HydroDevice.device_id` (MAC / `esp32-dev-001`) | `BilliardDevice.device_id` (e.g. `BILLIARD-CONTROLLER-01`) |
| Auth | User JWT / device lookup by id | **`X-Device-Id` + `X-Device-Key`** headers (never a user JWT) |
| Direction | ESP32 pushes `POST /sensor/data`, polls `GET /hydro/status` | ESP32 pushes events, pulls state on boot/reconnect |
| Who decides | Backend rules engine decides actuator state | **Backend owns billing and payment; ESP32 owns scoreboard while offline** |
| Offline | Not required | Required: persistent queue, replay on reconnect |
| Registration | `POST /hydro/devices` (user JWT) | `POST /billiard/controller/register` (manager JWT), key shown once |

Reused from hydro: the "ESP32 is the initiator of every request" model, the external string `device_id`, `last_seen` heartbeat and the offline-after-silence rule. Hydro's `device_id` can seed a billiard controller (see 2.2).

---

## 2. One-time setup

### 2.1 Run the DB migration
`python migrate_billiard_phase1.py` (idempotent). Fresh databases get everything from `init_db`.

### 2.2 Register the controller (manager/admin JWT)

Optional: list hydro devices you can reuse the id/name from.

```http
GET /billiard/controller/hydro-candidates
```

Register:

```http
POST /billiard/controller/register
Authorization: Bearer <manager token>

{"device_id": "BILLIARD-CONTROLLER-01", "name": "Hall controller"}
```
or `{"hydro_device_id": 3}` to copy `device_id` and `name` from `devices_hydro`.

Response (`201`) includes **`api_key` exactly once**. Only its SHA-256 hash is stored. `409` if the `device_id` already exists.

Flash into the ESP32 `secrets.py` (not `config.py`):

```python
BILLIARD_DEVICE_ID = "BILLIARD-CONTROLLER-01"
BILLIARD_DEVICE_KEY = "<api_key>"
```

and put `API_BASE` → `FASTAPI_URL = "https://<your-api>"` in `billiard_config.py`, not in `secrets.py`.

The device belongs to the registering user's `client_id`. The controller only ever sees tables of that club. A lost key means registering a new device (there is no rotate endpoint yet).

### 2.3 Tables must exist first
Create them in the POS (`POST /tables`). The controller never creates tables. The ESP32 needs a local map **RS485 address → backend `table_id`**:

```python
TABLE_MAP = {1: 11, 2: 12, 3: 13}   # rs485_address -> billiard_tables.id
```

The backend does not store RS485 addresses. Build the map from `GET /billiard/controller/state` (names `B01`, `B02`, …) once and keep it in config. This is a known gap (see 9).

---

## 3. Request rules

Every firmware call carries:

```http
X-Device-Id: BILLIARD-CONTROLLER-01
X-Device-Key: <api_key>
Content-Type: application/json
```

| Result | Meaning |
| --- | --- |
| `401` | Unknown device or wrong key. Do not retry in a tight loop; show `SYNC ERROR` |
| `403` | `/sync` body `device_id` does not match the header device |
| `422` | Body failed validation (e.g. `device_seq < 1`, batch > 500 events) |

```markdown
Any authenticated call sets `last_seen` and `status=online`. The backend reports the device
`offline` after **90 s** without a request. A cheap `GET /state` every 45 s is sufficient
and needs no `device_seq`; reserve `heartbeat` events (which consume a seq and a flash write)
for diagnostics, e.g. on connect and hourly.
```

---

## 4. Endpoints

| Method | Path | Use |
| --- | --- | --- |
| `GET` | `/billiard/controller/state` | Pull server state of all active tables (boot / reconnect) |
| `POST` | `/billiard/controller/events` | One live event (online) |
| `POST` | `/billiard/controller/sync` | Batch replay of the offline queue (max 500) |
| `GET` | `/billiard/controller/status` | **POS only** (manager JWT): device health |

`/events` and `/sync` use the same pipeline, so replays are safe in either.

### 4.1 `GET /state`

```json
{
  "device_id": "BILLIARD-CONTROLLER-01",
  "server_time": "2026-10-25T15:40:00Z",
  "server_state_version": 182,
  "tables": [
    {"table_id": 11, "name": "B01", "status": "playing",
     "session_id": 1004, "session_start_time": "2026-10-25T14:00:00Z",
     "session_ends_at": null,
     "game_id": 203, "game_number": 3, "player_a_score": 8, "player_b_score": 6},
    {"table_id": 12, "name": "B02", "status": "available",
     "session_id": null, "game_id": null}
  ]
}
```

Use `server_time` to correct the local clock, and `server_state_version` to know which queued events can be dropped. Only tables with `is_active=true` appear. `session_ends_at` is only set for timed play; otherwise show elapsed time from `session_start_time`.

### 4.2 Event envelope (`DeviceEventIn`)

```json
{
  "event_id": "7f3c...",          // unique string, max 64, idempotency key
  "event": "score_change",        // see 5
  "device_seq": 57,               // integer >= 1, monotonic per device, survives reboot
  "table_id": 11,                 // backend table id (not RS485 address)
  "timestamp": "2026-10-25T15:20:00+07:00",
  "clock_synced": true,           // was NTP valid when stamped?
  "local_session_id": "LOCAL-S-20261025-0001",
  "local_game_id": "LOCAL-G-20261025-0001",
  "data": { }
}
```

`/sync` wraps these: `{"device_id": "...", "events": [ ... up to 500 ... ]}`.

Response (`EventBatchResult`) from both endpoints:

```json
{"accepted": ["e1"], "duplicates": ["e2"],
 "rejected": [{"event_id": "e3", "error": "Game not found"}],
 "conflicts": [{"event_id": "e4", "error": "Session is not active on the server"}],
 "server_state_version": 2}
```

### 4.3 Ordering and the acknowledgement cursor
- Events are applied **sorted by `device_seq`**, never by timestamp (the ESP32 clock can be wrong offline).
- `device_seq` must be unique per device. Reusing a seq with a different `event_id` is rejected.
- `server_state_version` = highest **contiguous** `device_seq` whose events are `processed`. The firmware may delete queued events with `seq <= server_state_version` and must keep everything above.
- A `rejected` or `conflict` event does **not** advance the cursor, and neither do events after it. See 7.3.

---

## 5. Event types and `data` fields

Implemented handlers: `heartbeat`, `session_start`, `session_stop`, `game_start`, `new_game`, `game_end`, `score_change`, `score_reset`. Anything else (`time_extend`, `button_press`, `table_status`, `device_error`) is stored as `rejected` ("No handler"); do not send them yet.

How the backend finds the target:

| Target | Resolved by (first match) |
| --- | --- |
| Session | `data.session_id` → `local_session_id` map → active session on `table_id` |
| Game | `data.game_id` → `local_game_id` map → active game of the active session on `table_id` |

| Event | Required | `data` / notes |
| --- | --- | --- |
| `heartbeat` | none | `data.firmware_version` (optional, stored max 30 chars). Also send `wifi_rssi`, `uptime`, `free_memory` for your own logs |
| `session_start` | `table_id` | `local_session_id` recommended. If the POS already has an active session on that table, the device id **maps to it** (no duplicate) |
| `session_stop` | session ref | optional `score_a`, `score_b` (final score of the running game, device wins). Billing is computed by the server |
| `game_start` | session ref | `local_game_id`. Adopts an already active server game if one exists |
| `new_game` | game ref | `score_a`, `score_b` = final score of the finished game; `new_local_game_id` = id for the next game. Session and billing are untouched |
| `game_end` | game ref | optional final `score_a`/`score_b`; closes the game |
| `score_change` | game ref | **absolute** `score_a`, `score_b` (not a delta) |
| `score_reset` | game ref | none; scores become 0:0 |

Notes:
- `score_change` carries the scoreboard values, not +1/-1. Send the current A/B after each press. Negative or missing scores are rejected.
- Device wins on scores: the server overwrites its value and logs `{"server":[1,0],"device":[8,6]}` in the event payload.
- `table_id` is validated against the device's club; a foreign id is rejected.

---

## 6. Time and clock rules

| Rule | Value / source |
| --- | --- |
| Use UTC or an offset-aware ISO timestamp | any |
| `clock_synced=false` | Timestamp ignored, server time is used |
| Timestamp ahead of server | allowed up to `MAX_CLOCK_AHEAD_SECONDS` (default **120 s**), clamped to now; beyond that → `rejected` ("resync the device clock") |
| Timestamp older than | `MAX_OFFLINE_HOURS` (default **48 h**) → `rejected` |
| End time | never before start; start never in the future |

Consequences for firmware:
1. Run NTP on boot and set `clock_synced` accordingly. If NTP has never succeeded, send `false`.
2. For offline play longer than 48 h events are rejected, so alert the operator after about 24 h offline.
3. The countdown/elapsed display is local; billing uses server-computed duration from the accepted timestamps.

Fee and total shown on the display while offline are **estimates only**. The ESP32 must never send `grand_total`, fees, rates or payment status; they are ignored by design.

---

## 7. Firmware state machine

### 7.1 Boot
1. Load persisted state: `device_seq` counter, event queue, local session/game ids, scores per table.
2. Connect Wi-Fi, run NTP, set `clock_synced`.
3. `GET /state`. Reconcile per table:
   - Server has a session, device has none → adopt it (store `session_id`, `game_id`).
   - Device has a local session the server doesn't know → keep it; its `session_start` is still queued.
   - Scores: **device wins** if it has unsynced score events; otherwise adopt server scores.
4. Flush the queue with `/sync`, then resume live `/events`.

### 7.2 Event queue (persistent)

```python
# event_queue.py (MicroPython sketch)
def enqueue(kind, table_id, data=None, local_session_id=None, local_game_id=None):
    seq = storage.next_seq()                  # write the counter BEFORE the event
    ev = {"event_id": uuid4_hex(), "event": kind, "device_seq": seq,
          "table_id": table_id, "timestamp": now_iso(),
          "clock_synced": clock.is_synced(),
          "local_session_id": local_session_id, "local_game_id": local_game_id,
          "data": data or {}}
    storage.append_event(ev)                  # LittleFS, one line per event
    return ev
```

Rules: persist state transitions, not countdown ticks. Never delete before the server acks. Reuse the same `event_id` and `device_seq` on retry.

### 7.3 Sync loop

```python
def sync():
    batch = queue.peek(500)
    if not batch: return
    r = post("/billiard/controller/sync", {"device_id": DEVICE_ID, "events": batch})
    # r["accepted"] + r["duplicates"] are safe
    queue.drop_up_to(r["server_state_version"])
    for item in r["rejected"] + r["conflicts"]:
        error_log.append(item)                # keep for the operator, never auto-delete
```

Important: `server_state_version` stops at the first non-processed seq. Events after a rejected one are applied but stay un-acked, and will be resent as `duplicates` (harmless). To avoid an ever-growing queue, **move a rejected/conflict event to the local error log and mark it "resolved locally"; if it is permanently invalid, ask a manager to fix the data and resend**. A rejected event keeps its `event_id`, so retrying the same event after the server state is fixed (e.g. session started in POS) is the supported recovery path.

Retry/backoff: 2 s, 4 s, 8 s … capped at 60 s. A network error is not a rejection; keep the queue untouched.

```markdown
Flush the queued backlog **before** reconciling with `GET /state`, and only adopt server
state for tables that have no un-acked local events. Otherwise a session stopped offline
is re-adopted from stale server state. Use batches of ~20 events on the device; 500 is the
server limit, not an ESP32-safe size.
```

### 7.4 Live (online) mode
After each button action: update local display immediately, `enqueue`, then `POST /events`. If it fails, the event stays in the queue (same code path as offline).

### 7.5 Local ids
- `LOCAL-S-YYYYMMDD-NNNN`, `LOCAL-G-YYYYMMDD-NNNN`, max 64 chars, unique per device.
- Attach `local_session_id` to every event of that session until the server mapping is confirmed by `/state` (it then returns real `session_id` / `game_id`); from then on you may send `data.session_id` / `data.game_id`.
- New game: put the id for the next game in `data.new_local_game_id` of the `new_game` event.

---

## 8. Offline replay example (table 3)

| seq | event | notes |
| --- | --- | --- |
| 10 | `session_start` | `local_session_id=LOCAL-S-…-0001`, `table_id=13` |
| 11 | `game_start` | `local_game_id=LOCAL-G-…-0001` |
| 12 | `score_change` | `{"score_a": 5, "score_b": 3}` |
| 13 | `new_game` | `{"score_a":5,"score_b":3,"new_local_game_id":"LOCAL-G-…-0002"}` on game 1 |
| 14 | `score_change` | `{"score_a":7,"score_b":4}` on game 2 |
| 15 | `session_stop` | `{"score_a":7,"score_b":4}` |

Result: one server session, two completed games, table back to `available`, bill computed by the server (e.g. 90 min × 100,000/h = 150,000 from the pricing snapshot). The cashier then pays through the Payments flow.

---

## 9. Known gaps and decisions to make

| # | Gap | Impact / suggestion |
| --- | --- | --- |
| 1 | No RS485 address field on tables | Keep `TABLE_MAP` in firmware, or add `billiard_tables.rs485_address` later |
| 2 | No API-key rotation endpoint | Re-register to replace a lost key |
| 3 | Rejected event stalls the ack cursor | Firmware must keep an error log; consider a server "skip seq" action |
| 4 | `time_extend`, `button_press`, `table_status`, `device_error` have no handler | Do not send; use `heartbeat.data` for diagnostics |
| 5 | Packages and reservations not implemented | No firmware impact yet |
| 6 | WebSocket POS push not implemented | POS polls; controller status via `/billiard/controller/status` |
| 7 | `hydro-candidates` only hides ids already used by billiard | Registering a hydro `device_id` does not change hydro rows |

## 10. Test checklist (before shipping the firmware)

1. Register, store key, `GET /state` returns your tables.
2. Online: start → score → new game → stop; check POS bill and games list.
3. Pull Wi-Fi, repeat step 2, reconnect: events replay, no duplicate sessions.
4. Reboot mid-session offline: scores, ids and queue survive.
5. Resend the same batch: all `duplicates`, scores unchanged.
6. Start the session in the POS first, then replay a device `session_start`: maps to the POS session.
7. Send a timestamp 3 h in the future: `rejected`; send `clock_synced=false`: accepted with server time.
8. Wrong key → `401`; mismatched `device_id` in `/sync` → `403`.
9. Silence for 90 s: `/billiard/controller/status` shows `offline`.
