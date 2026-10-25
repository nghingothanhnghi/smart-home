"""Backend-neutral scoreboard state machine used by RS485 and UI drivers."""

import time
import ubinascii
import machine

import billiard_config as config
from billiard_storage import BilliardStorage


class BilliardController:
    def __init__(self, storage=None):
        self.storage = storage or BilliardStorage()
        self.last_heartbeat = 0

    def table_id_for_address(self, address):
        table_id = config.TABLE_MAP.get(address)
        if table_id is None:
            raise ValueError("unknown RS485 address %s" % address)
        return table_id

    def _timestamp(self):
        # The server treats clock_synced=false timestamps as advisory.  A
        # hardware integration can replace this after NTP/RTC validation.
        parts = time.gmtime()
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % parts[:6]

    def _event_id(self):
        return ubinascii.hexlify(machine.unique_id()).decode() + ("%08x" % time.ticks_ms())

    def _emit(self, kind, table_id, data=None):
        local = self.storage.table(table_id)
        payload = data or {}
        # Once the server has told us its identifiers, include them as the
        # strongest reference while retaining local IDs for offline recovery.
        if local.get("session_id"):
            payload["session_id"] = local["session_id"]
        if local.get("game_id"):
            payload["game_id"] = local["game_id"]
        event = {
            "event_id": self._event_id(), "event": kind,
            "device_seq": self.storage.next_seq(),
            "timestamp": self._timestamp(), "clock_synced": False,
            "local_session_id": local.get("local_session_id"),
            "local_game_id": local.get("local_game_id"), "data": payload,
        }
        # Heartbeats do not have a table target.  Do not invent table_id=0,
        # which would fail club/table validation on a strict backend.
        if table_id:
            event["table_id"] = table_id
        self.storage.append_event(event)
        return event

    def start_session(self, address):
        table_id = self.table_id_for_address(address)
        local = self.storage.table(table_id)
        if local.get("active"):
            return None
        local["active"] = True
        local["local_session_id"] = self.storage.next_local_id("LOCAL-S")
        local["local_game_id"] = None
        local["score_a"] = local["score_b"] = 0
        self.storage.save_table_state()
        return self._emit("session_start", table_id)

    def start_game(self, address):
        table_id = self.table_id_for_address(address)
        local = self.storage.table(table_id)
        if not local.get("active"):
            raise ValueError("start a session first")
        # A game adopted from /state already exists on the server; do not
        # create a duplicate merely because it has no local offline ID.
        if local.get("local_game_id") or local.get("game_id"):
            return None
        if not local.get("local_game_id"):
            local["local_game_id"] = self.storage.next_local_id("LOCAL-G")
            self.storage.save_table_state()
        return self._emit("game_start", table_id)

    def change_score(self, address, score_a, score_b):
        if score_a < 0 or score_b < 0:
            raise ValueError("scores cannot be negative")
        table_id = self.table_id_for_address(address)
        local = self.storage.table(table_id)
        if not local.get("local_game_id") and not local.get("game_id"):
            raise ValueError("start a game first")
        local["score_a"], local["score_b"] = score_a, score_b
        self.storage.save_table_state()
        return self._emit("score_change", table_id, {"score_a": score_a, "score_b": score_b})

    def stop_session(self, address):
        table_id = self.table_id_for_address(address)
        local = self.storage.table(table_id)
        if not local.get("active"):
            return None
        event = self._emit("session_stop", table_id, {
            "score_a": local.get("score_a", 0), "score_b": local.get("score_b", 0),
        })
        local["active"] = False
        local["local_game_id"] = None
        self.storage.save_table_state()
        return event

    def heartbeat_due(self):
        return time.time() - self.last_heartbeat >= config.HEARTBEAT_S

    def heartbeat(self):
        self.last_heartbeat = time.time()
        return self._emit("heartbeat", 0, {"firmware_version": config.FIRMWARE_VERSION})

    def reconcile(self, server_state):
        """Adopt server sessions only where the local table has no activity."""
        for server in server_state.get("tables", []):
            table_id = server["table_id"]
            local = self.storage.table(table_id)
            if server.get("session_id") and not local.get("active"):
                local["active"] = True
                local["session_id"] = server["session_id"]
                local["game_id"] = server.get("game_id")
                local["score_a"] = server.get("player_a_score", 0)
                local["score_b"] = server.get("player_b_score", 0)
        self.storage.save_table_state()

    def apply_result(self, result):
        self.storage.drop_up_to(result.get("server_state_version", 0))
        self.storage.log_errors(result.get("rejected", []) + result.get("conflicts", []))
