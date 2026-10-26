"""Backend-neutral scoreboard state machine used by RS485 and UI drivers."""

import os
import ubinascii

import billiard_config as config
from billiard_clock import Clock
from billiard_storage import BilliardStorage

_SESSION_KEYS = ("session_id", "game_id", "local_session_id", "local_game_id")


class BilliardController:
    def __init__(self, storage=None, clock=None):
        self.storage = storage or BilliardStorage()
        self.clock = clock or Clock()
        # Highest seq already sent in this connection. Lets the sync loop send
        # only NEW events instead of re-sending a stalled backlog every tick.
        self.delivered_seq = 0
        # Read-only status for a display adapter.
        self.online = False
        self.sync_error = False
        self.alert = None

    def table_id_for_address(self, address):
        table_id = config.TABLE_MAP.get(address)
        if table_id is None:
            raise ValueError("unknown RS485 address %s" % address)
        return table_id

    def _target(self, address):
        table_id = self.table_id_for_address(address)
        return table_id, self.storage.table(table_id)

    def _event_id(self, seq):
        # <device>-<seq>-<rand>: unique per event even if the counter were
        # ever lost, so a reused seq is rejected loudly instead of being
        # silently swallowed as a "duplicate" of an older event.
        return "%s-%d-%s" % (config.DEVICE_ID[:40], seq,
                             ubinascii.hexlify(os.urandom(2)).decode())

    def _emit(self, kind, table_id, data=None, seq=None):
        payload = dict(data or {})
        local = self.storage.table(table_id) if table_id else {}
        # Once the server has told us its identifiers, include them as the
        # strongest reference while retaining local IDs for offline recovery.
        if local.get("session_id"):
            payload.setdefault("session_id", local["session_id"])
        if local.get("game_id"):
            payload.setdefault("game_id", local["game_id"])

        own_seq = seq is None
        if own_seq:
            seq = self.storage.reserve_seq()
        event = {
            "event_id": self._event_id(seq), "event": kind,
            "device_seq": seq,
            "timestamp": self.clock.now_iso(),
            "clock_synced": self.clock.synced,
            "local_session_id": local.get("local_session_id"),
            "local_game_id": local.get("local_game_id"), "data": payload,
        }
        # Heartbeats do not have a table target.  Do not invent table_id=0,
        # which would fail club/table validation on a strict backend.
        if table_id:
            event["table_id"] = table_id
        self.storage.append_event(event)
        if own_seq:
            # Commit AFTER the append; also persists any table-state change
            # made by the caller, so state and event land together.
            self.storage.commit_seq(seq)
        return event

    @staticmethod
    def _clear_game(local):
        local["game_id"] = None
        local["local_game_id"] = None

    @staticmethod
    def _clear_session(local):
        """Drop every id so the NEXT session on this table starts clean."""
        local["active"] = False
        for key in _SESSION_KEYS:
            local[key] = None
        local["score_a"] = local["score_b"] = 0

    # ---- button actions ------------------------------------------------
    def start_session(self, address):
        table_id, local = self._target(address)
        if local.get("active"):
            return None
        self._clear_session(local)        # no stale server ids from a previous session
        local["active"] = True
        local["local_session_id"] = self.storage.next_local_id("LOCAL-S")
        return self._emit("session_start", table_id)

    def start_game(self, address):
        table_id, local = self._target(address)
        if not local.get("active"):
            raise ValueError("start a session first")
        # A game adopted from /state already exists on the server; do not
        # create a duplicate merely because it has no local offline ID.
        if local.get("local_game_id") or local.get("game_id"):
            return None
        local["local_game_id"] = self.storage.next_local_id("LOCAL-G")
        local["score_a"] = local["score_b"] = 0
        return self._emit("game_start", table_id)

    def change_score(self, address, score_a, score_b):
        if score_a < 0 or score_b < 0:
            raise ValueError("scores cannot be negative")
        table_id, local = self._target(address)
        if not local.get("local_game_id") and not local.get("game_id"):
            raise ValueError("start a game first")
        local["score_a"], local["score_b"] = score_a, score_b
        return self._emit("score_change", table_id,
                          {"score_a": score_a, "score_b": score_b})

    def score_reset(self, address):
        table_id, local = self._target(address)
        if not local.get("local_game_id") and not local.get("game_id"):
            raise ValueError("start a game first")
        local["score_a"] = local["score_b"] = 0
        return self._emit("score_reset", table_id)

    def new_game(self, address, score_a=None, score_b=None):
        """Finish the current game with its final score and open the next one.
        Session and billing are untouched."""
        table_id, local = self._target(address)
        if not local.get("local_game_id") and not local.get("game_id"):
            raise ValueError("start a game first")
        final_a = local.get("score_a", 0) if score_a is None else score_a
        final_b = local.get("score_b", 0) if score_b is None else score_b
        if final_a < 0 or final_b < 0:
            raise ValueError("scores cannot be negative")
        next_id = self.storage.next_local_id("LOCAL-G")
        # Emitted with the OLD game's ids (they target the finished game) ...
        event = self._emit("new_game", table_id, {
            "score_a": final_a, "score_b": final_b, "new_local_game_id": next_id,
        })
        # ... and the table moves on to the new one.  The new game's server
        # id is unknown until /state reports it.
        local["game_id"] = None
        local["local_game_id"] = next_id
        local["score_a"] = local["score_b"] = 0
        self.storage.save_table_state()
        return event

    def game_end(self, address, score_a=None, score_b=None):
        table_id, local = self._target(address)
        if not local.get("local_game_id") and not local.get("game_id"):
            raise ValueError("start a game first")
        data = {}
        if score_a is not None and score_b is not None:
            if score_a < 0 or score_b < 0:
                raise ValueError("scores cannot be negative")
            data = {"score_a": score_a, "score_b": score_b}
        event = self._emit("game_end", table_id, data)
        self._clear_game(local)
        local["score_a"] = local["score_b"] = 0
        self.storage.save_table_state()
        return event

    def stop_session(self, address):
        table_id, local = self._target(address)
        if not local.get("active"):
            return None
        event = self._emit("session_stop", table_id, {
            "score_a": local.get("score_a", 0), "score_b": local.get("score_b", 0),
        })
        self._clear_session(local)        # event already carries the ids it needs
        self.storage.save_table_state()
        return event

    # ---- diagnostics / liveness ----------------------------------------
    def heartbeat(self, extra=None):
        """Queued diagnostic event. Liveness itself is kept by a cheap
        GET /state keepalive (see billiard_main), not by this."""
        data = {"firmware_version": config.FIRMWARE_VERSION}
        if extra:
            data.update(extra)
        return self._emit("heartbeat", 0, data)

    def fill_gaps(self, server_version):
        """Plug seqs that were reserved but never queued (e.g. a queue written
        by older firmware that saved the counter before the event). The backend
        acks only contiguous seqs, so one hole would stall the cursor forever."""
        base = max(server_version, self.storage.cursor())
        gaps = self.storage.missing_seqs(base)[:200]
        for seq in gaps:
            self._emit("heartbeat", 0,
                       {"firmware_version": config.FIRMWARE_VERSION, "gap_fill": True},
                       seq=seq)
        if gaps:
            print("[billiard] filled %d seq gap(s): %s" % (len(gaps), gaps[:10]))
        return gaps

    # ---- sync bookkeeping ----------------------------------------------
    def reset_delivery(self):
        """Forget what was sent so every un-acked event is sent again
        (reconnect, or the periodic stall retry)."""
        self.delivered_seq = 0

    def pending_events(self, limit):
        floor = max(self.delivered_seq, self.storage.cursor())
        if self.storage.last_seq() <= floor:
            return []                     # RAM-only check: no file scan when idle
        return self.storage.events(limit, floor)

    def has_unacked(self):
        return self.storage.has_unacked()

    def apply_result(self, result, sent=None):
        """Returns the number of NEW rejected/conflict entries logged."""
        self.storage.drop_up_to(result.get("server_state_version", 0))
        if sent:
            top = max(e["device_seq"] for e in sent)
            if top > self.delivered_seq:
                self.delivered_seq = top
        return self.storage.log_errors(
            list(result.get("rejected", [])) + list(result.get("conflicts", [])))

    def observe_server_time(self, server_state):
        return self.clock.set_from_server(server_state.get("server_time"))

    # ---- boot / reconnect reconciliation -------------------------------
    def reconcile(self, server_state):
        """
        Adopt server state ONLY for tables with no un-acked local events
        (device wins where it has unsynced work). Call this AFTER the queue
        was flushed, otherwise a session stopped offline would be re-adopted
        as a ghost session.
        """
        self.observe_server_time(server_state)
        self.storage.drop_up_to(server_state.get("server_state_version", 0))
        pending = self.storage.pending_tables()

        for server in server_state.get("tables", []):
            table_id = server["table_id"]
            if str(table_id) in pending:
                continue
            local = self.storage.table(table_id)
            sid = server.get("session_id")

            if sid:
                if not local.get("active"):
                    # POS (or an earlier boot) started it - adopt wholesale.
                    self._clear_session(local)
                    local["active"] = True
                local["session_id"] = sid
                local["game_id"] = server.get("game_id")
                if server.get("player_a_score") is not None:
                    local["score_a"] = server["player_a_score"]
                if server.get("player_b_score") is not None:
                    local["score_b"] = server["player_b_score"]
            elif local.get("active"):
                # Everything we sent is acked and the server has no session:
                # it was closed from the POS. Do not keep a ghost session.
                print("[billiard] table %s session ended on server, clearing" % table_id)
                self._clear_session(local)

        self.storage.save_table_state()
