"""LittleFS-backed durable state and event queue for billiard firmware."""

import ujson
import os

import billiard_config as config


class BilliardStorage:
    def __init__(self):
        self.state = self._read_json(config.STATE_FILE, {
            "next_seq": 1, "local_counter": 0, "tables": {}, "server_state_version": 0
        })

    def _read_json(self, path, default):
        try:
            with open(path, "r") as handle:
                return ujson.loads(handle.read())
        except (OSError, ValueError):
            return default

    def save_state(self):
        # Rename gives a substantially safer state update than writing the
        # live file directly if power is lost during a button press.
        temporary = config.STATE_FILE + ".tmp"
        with open(temporary, "w") as handle:
            handle.write(ujson.dumps(self.state))
        try:
            os.remove(config.STATE_FILE)
        except OSError:
            pass
        os.rename(temporary, config.STATE_FILE)

    def next_seq(self):
        sequence = self.state["next_seq"]
        self.state["next_seq"] = sequence + 1
        self.save_state()                 # persist counter before event append
        return sequence

    def next_local_id(self, prefix):
        self.state["local_counter"] += 1
        self.save_state()
        return "%s-%08d" % (prefix, self.state["local_counter"])

    def table(self, table_id):
        return self.state["tables"].setdefault(str(table_id), {})

    def save_table_state(self):
        self.save_state()

    def append_event(self, event):
        with open(config.QUEUE_FILE, "a") as handle:
            handle.write(ujson.dumps(event) + "\n")

    def events(self, limit):
        result = []
        try:
            with open(config.QUEUE_FILE, "r") as handle:
                for line in handle:
                    if line.strip():
                        result.append(ujson.loads(line))
                        if len(result) >= limit:
                            break
        except OSError:
            pass
        return result

    def drop_up_to(self, sequence):
        """Remove only server-acknowledged contiguous events."""
        temporary = config.QUEUE_FILE + ".tmp"
        with open(temporary, "w") as handle:
            try:
                with open(config.QUEUE_FILE, "r") as source:
                    for line in source:
                        if line.strip():
                            item = ujson.loads(line)
                            if item["device_seq"] > sequence:
                                handle.write(ujson.dumps(item) + "\n")
            except OSError:
                pass
        try:
            os.remove(config.QUEUE_FILE)
        except OSError:
            pass
        os.rename(temporary, config.QUEUE_FILE)
        self.state["server_state_version"] = max(self.state.get("server_state_version", 0), sequence)
        self.save_state()

    def log_errors(self, items):
        if not items:
            return
        with open(config.ERROR_FILE, "a") as handle:
            for item in items:
                handle.write(ujson.dumps(item) + "\n")
