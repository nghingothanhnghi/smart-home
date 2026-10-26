## Billiard Controller (separate firmware, same repo)

A second ESP32 — **one central board for all billiard tables** — runs its own
firmware stack and talks to the FastAPI `app/billiard` backend. It shares
**nothing** with the hydro firmware: different entry point, different config,
different auth (`X-Device-Id` / `X-Device-Key` headers, never the hydro JWT).
Backend contract: `ESP32_BILLIARD_CONTROLLER_GUIDE.md`.

> **Never** copy the hydro `main.py` to a billiard board, or the billiard
> `main.py` to a hydro board. MicroPython only auto-runs `main.py`.

### Files

| File | Role |
|---|---|
| `main_billiard.py` | Copy to the board **as `main.py`**; it just imports `billiard_main`. |
| `billiard_main.py` | uasyncio sync loop: WiFi, connect sequence, queue flush, keepalive, NTP, offline alert. Starts `billiard_adapter.run(controller)` if that module exists. |
| `billiard_controller.py` | Scoreboard state machine. The RS485/display adapter calls it; it never touches the network. |
| `billiard_storage.py` | Durable state (`billiard_state.json`), event queue (`billiard_events.jsonl`) and operator error log (`billiard_event_errors.jsonl`). |
| `billiard_api.py` | HTTP client (device-key headers, timeout, `ApiError`). |
| `billiard_clock.py` | UTC clock: NTP first, backend `server_time` as fallback. |
| `billiard_config.py` | URLs, `TABLE_MAP`, tuning knobs. Does **not** import `config.py`. |
| `tests/billiard/` | Host-side tests (CPython + MicroPython shims + fake backend). **Do not copy to the board.** |

### Setup

1. Register the controller (manager JWT): `POST /billiard/controller/register`.
   The `api_key` is shown **once**.
2. Add to the untracked `secrets.py` on the board:

   ```python
   WIFI_SSID = "..."
   WIFI_PASSWORD = "..."
   BILLIARD_DEVICE_ID = "BILLIARD-CONTROLLER-01"
   BILLIARD_DEVICE_KEY = "<api_key>"
   ```

   Each name is read independently, so a missing `BILLIARD_*` name no longer
   blanks the WiFi credentials. An empty key makes every API call fail with a
   clear error rather than falling back to anything else.
3. Edit `billiard_config.py`: `FASTAPI_URL` and the real `TABLE_MAP`
   (RS485 address → `billiard_tables.id`, built from `GET /billiard/controller/state`).
4. Copy to the board: `billiard_*.py`, `secrets.py`, and `main_billiard.py` **as** `main.py`:

   ```
   mpremote cp billiard_api.py billiard_clock.py billiard_config.py billiard_controller.py billiard_main.py billiard_storage.py secrets.py :
   mpremote cp main_billiard.py :main.py
   ```
5. Watch the serial console (115200). Look for `[billiard] online, queue unacked: False`.

### RS485 / display adapter (still to write)

Create `billiard_adapter.py` with `async def run(controller)`. It is started
automatically and shares the controller with the sync loop. Call, using the
RS485 address (translated through `TABLE_MAP`):

| Call | Event sent |
|---|---|
| `start_session(addr)` / `stop_session(addr)` | `session_start` / `session_stop` (final scores) |
| `start_game(addr)` | `game_start` |
| `change_score(addr, a, b)` | `score_change` (absolute values) |
| `new_game(addr)` | `new_game` (final score of the finished game, opens the next one) |
| `game_end(addr)` | `game_end` |
| `score_reset(addr)` | `score_reset` |

For the display: `controller.online`, `controller.sync_error` (wrong key /
device id, shows `SYNC ERROR`), `controller.alert` (e.g. `OFFLINE 25h`).
Fees and totals shown while offline are estimates; the board never sends
billing fields.

### How sync works

* **Connect / reconnect order:** NTP → flush the queued backlog → `GET /state`
  → reconcile → fill any seq gaps. Flushing first matters: reconciling
  against stale server state would resurrect a session that was stopped offline.
* **Reconcile** adopts server state only for tables with **no un-acked local
  events** (device wins where it has unsynced work). It also adopts server
  scores, and clears a local session the POS already closed.
* **Live path:** one new event → `POST /events`; a backlog → `POST /sync` in
  batches of `MAX_SYNC_BATCH` (20; the backend's limit of 500 would exhaust
  ESP32 RAM).
* **Only new events are sent** on each tick. If a rejected event blocks the
  ack cursor, the un-acked backlog is re-sent every `STALL_RETRY_S` (60 s) so
  a fix made in the POS lets it through. Rejections are written to the error
  log **once** per `(event_id, error)` and the log rotates at 16 KB.
* **Liveness:** the backend marks the device offline after 90 s without any
  authenticated request. The board sends `GET /state` every `KEEPALIVE_S`
  (45 s) when otherwise quiet. That needs no seq and no flash write. A
  diagnostic `heartbeat` event (firmware version, RSSI, uptime, free memory)
  is queued only on connect and hourly, and only while online.
* **Clock:** `clock_synced` is `true` only after NTP or the backend's
  `server_time` set the RTC in this boot. Otherwise the backend ignores the
  device timestamp and bills from replay time. The backend rejects events
  older than 48 h, so the board raises `controller.alert` after 24 h offline.

### Durability

* State and queue rewrites go to `*.tmp` and are renamed over the target; an
  interrupted swap is finished or discarded on the next boot. A torn last
  queue line is repaired so it cannot corrupt the next event.
* A seq is committed **after** its event is appended; boot raises the counter
  above the highest queued seq, the acked cursor, and (on reconnect) the
  server's `server_state_version`. A seq is never reused or skipped, and a
  hole left by older firmware is plugged with a heartbeat.
* The ack cursor is saved before the queue is trimmed.
* `event_id` is `<device_id>-<seq>-<4 hex>`, stable across retries.

### Host tests

```
python3 tests/billiard/test_billiard.py
```

Runs on a PC with no hardware. They cover crash recovery, replay, stale ids,
rejected-event stalls, gap filling, reconcile rules, keepalive/flash economy
and the API timeout. They do **not** exercise real WiFi, NTP, LittleFS, the
RS485 bus or memory pressure — bench-test those on the board.

### Billiard troubleshooting

| Symptom | Likely cause |
|---|---|
| `SYNC ERROR (auth)` / `controller.sync_error` | `401`/`403`: wrong `BILLIARD_DEVICE_KEY`, unknown or mismatched device id. Retried every 60 s only. A lost key means re-registering. |
| `BILLIARD_DEVICE_KEY is not configured` | Missing in `secrets.py`. |
| `N event(s) rejected/conflicted` | See `billiard_event_errors.jsonl`. Fix the cause (e.g. start the session in the POS); the same event is retried every 60 s. |
| Board shows offline in the POS although WiFi is up | Backend unreachable, or no request for >90 s. Check `FASTAPI_URL`. |
| Offline session billed from sync time | `clock_synced` was false (NTP never succeeded). Check DNS/UDP 123. |
| `OFFLINE 25h` alert | Backend drops events older than 48 h. Restore connectivity now. |
| `unknown RS485 address` | Address missing from `TABLE_MAP`. |
| Nothing runs after boot | The board's `main.py` is not `main_billiard.py`. |
