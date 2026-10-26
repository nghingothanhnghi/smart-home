"""LittleFS-backed durable state and event queue for billiard firmware.

Durability rules (power can drop at any instruction):

* Whole-file updates go to ``<file>.tmp`` and are renamed over the target.
  On LittleFS that rename is atomic.  Where it cannot replace a file (FAT),
  the old file is removed only AFTER the tmp copy is complete, and ``_recover``
  finishes the swap on the next boot.  The live file is never missing without
  a complete tmp beside it.
* The sequence counter is committed AFTER the event line is appended
  (``reserve_seq`` -> ``append_event`` -> ``commit_seq``).  A crash in between
  leaves the event in the queue with the counter one behind; boot floors the
  counter to ``max(queue seq, acked cursor) + 1``, so a seq is never reused
  and never skipped.
* The acked cursor (``server_state_version``) is saved BEFORE the queue is
  trimmed; a crash in between only leaves already-acked lines that boot trims.
"""

import os
import ujson

import billiard_config as config

_LOGGED_MAX = 100


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(text)
    try:
        os.rename(tmp, path)             # atomic replace on littlefs
    except OSError:
        _remove(path)                     # FAT can't rename over; tmp is complete here
        os.rename(tmp, path)


def _recover(path):
    """Finish or discard an interrupted _atomic_write from a previous boot."""
    tmp = path + ".tmp"
    if not _exists(tmp):
        return
    if _exists(path):
        _remove(tmp)                      # crash while writing tmp: target is still good
    else:
        try:
            os.rename(tmp, path)          # crash between remove and rename: tmp is complete
        except OSError:
            pass


def _repair_tail(path):
    """If power died mid-append the last line has no newline; the next
    append would glue onto it and corrupt a good event."""
    try:
        with open(path, "rb") as handle:
            handle.seek(-1, 2)
            last = handle.read(1)
    except OSError:
        return                            # missing or empty
    if last != b"\n":
        with open(path, "a") as handle:
            handle.write("\n")


class BilliardStorage:
    def __init__(self):
        for path in (config.STATE_FILE, config.QUEUE_FILE):
            _recover(path)
        _repair_tail(config.QUEUE_FILE)

        self.recovered_state = False
        self.state = self._load_state()
        self._trim_acked()
        self._floor_seq()
        self._logged = self._load_logged()

    # ---- state -------------------------------------------------------
    def _read_json(self, path, default):
        try:
            with open(path, "r") as handle:
                return ujson.loads(handle.read())
        except (OSError, ValueError):
            return default

    def _load_state(self):
        data = self._read_json(config.STATE_FILE, None)
        if not isinstance(data, dict):
            data = {}
            if _exists(config.STATE_FILE):
                self.recovered_state = True
                print("[billiard] state file unreadable, rebuilding")
        for key, default in (("next_seq", 1), ("local_counter", 0),
                             ("tables", None), ("server_state_version", 0)):
            if key not in data:
                data[key] = {} if key == "tables" else default
        return data

    def save_state(self):
        _atomic_write(config.STATE_FILE, ujson.dumps(self.state))

    # ---- sequence numbers --------------------------------------------
    def _floor_seq(self):
        """Never hand out a seq that the queue or the ack cursor already used."""
        floor = self.state["server_state_version"]
        for item in self._iter_queue():
            seq = item.get("device_seq", 0)
            if seq > floor:
                floor = seq
        if self.state["next_seq"] <= floor:
            self.state["next_seq"] = floor + 1
            print("[billiard] next_seq raised to", floor + 1)

    def reserve_seq(self):
        return self.state["next_seq"]

    def commit_seq(self, seq):
        """Call after the event line is safely appended (also persists table state)."""
        self.state["next_seq"] = seq + 1
        self.save_state()

    def last_seq(self):
        return self.state["next_seq"] - 1

    def cursor(self):
        return self.state["server_state_version"]

    def has_unacked(self):
        return self.last_seq() > self.cursor()

    def next_local_id(self, prefix):
        # Not persisted here: the very next commit_seq() writes the counter
        # together with the event that carries this id.
        self.state["local_counter"] += 1
        return "%s-%08d" % (prefix, self.state["local_counter"])

    # ---- per-table state ---------------------------------------------
    def table(self, table_id):
        return self.state["tables"].setdefault(str(table_id), {})

    def save_table_state(self):
        self.save_state()

    # ---- queue -------------------------------------------------------
    def _iter_queue(self, min_seq=0):
        try:
            with open(config.QUEUE_FILE, "r") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = ujson.loads(line)
                    except ValueError:
                        continue          # torn line from a power cut
                    if item.get("device_seq", 0) > min_seq:
                        yield item
        except OSError:
            return

    def append_event(self, event):
        with open(config.QUEUE_FILE, "a") as handle:
            handle.write(ujson.dumps(event) + "\n")

    def events(self, limit, min_seq=0):
        result = []
        for item in self._iter_queue(max(min_seq, 0)):
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def pending_tables(self):
        """Table ids (as strings) that still have un-acked events queued."""
        tables = set()
        for item in self._iter_queue(self.cursor()):
            if item.get("table_id"):
                tables.add(str(item["table_id"]))
        return tables

    def missing_seqs(self, base):
        """Seqs in (base, next_seq) that were reserved but have no queued event."""
        seen = set()
        for item in self._iter_queue(base):
            seen.add(item["device_seq"])
        return [s for s in range(base + 1, self.state["next_seq"]) if s not in seen]

    def _rewrite_queue(self, min_seq):
        lines = []
        for item in self._iter_queue(min_seq):
            lines.append(ujson.dumps(item) + "\n")
        _atomic_write(config.QUEUE_FILE, "".join(lines))

    def _trim_acked(self):
        cursor = self.cursor()
        if cursor <= 0:
            return
        first = None
        for item in self._iter_queue(0):
            first = item.get("device_seq", 0)
            break
        if first is not None and first <= cursor:
            self._rewrite_queue(cursor)

    def drop_up_to(self, sequence):
        """
        Remove only server-acknowledged contiguous events. Does nothing
        (no flash writes) when the cursor has not moved - a stalled queue
        re-sent every few seconds must not rewrite itself every time.
        """
        if sequence <= self.cursor():
            return False
        self.state["server_state_version"] = sequence
        if self.state["next_seq"] <= sequence:
            self.state["next_seq"] = sequence + 1   # never reuse an acked seq
        self.save_state()                           # cursor first, then trim
        self._rewrite_queue(sequence)
        return True

    # ---- operator error log ------------------------------------------
    @staticmethod
    def _error_key(item):
        return "%s|%s" % (item.get("event_id"), item.get("error"))

    def _load_logged(self):
        logged = set()
        try:
            with open(config.ERROR_FILE, "r") as handle:
                for line in handle:
                    try:
                        logged.add(self._error_key(ujson.loads(line)))
                    except ValueError:
                        continue
        except OSError:
            pass
        return logged

    def log_errors(self, items):
        """Append each (event_id, error) once. Returns how many were new."""
        new = 0
        handle = None
        try:
            for item in items or ():
                key = self._error_key(item)
                if key in self._logged:
                    continue
                if len(self._logged) >= _LOGGED_MAX:
                    self._logged = set()
                self._logged.add(key)
                if handle is None:
                    self._rotate_errors()
                    handle = open(config.ERROR_FILE, "a")
                handle.write(ujson.dumps(item) + "\n")
                new += 1
        finally:
            if handle is not None:
                handle.close()
        return new

    def _rotate_errors(self):
        try:
            if os.stat(config.ERROR_FILE)[6] > config.ERROR_FILE_MAX_BYTES:
                _remove(config.ERROR_FILE + ".old")
                os.rename(config.ERROR_FILE, config.ERROR_FILE + ".old")
        except OSError:
            pass
