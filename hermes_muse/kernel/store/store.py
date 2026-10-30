"""Store v2: indexed entities, CAS state transitions, TTL exits and an event log (SQLite, stdlib only)."""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .machines import MACHINES, machine

SCHEMA = 1
EVENT_DAYS = 30
COLUMNS = "id, kind, natural_key, state, state_since, deadline, attempts, version, payload_json, updated"
DDL = """
CREATE TABLE entity(id TEXT PRIMARY KEY, kind TEXT NOT NULL, natural_key TEXT NOT NULL, state TEXT NOT NULL,
  state_since REAL NOT NULL, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
  payload_json TEXT NOT NULL, updated REAL NOT NULL);
CREATE INDEX entity_due ON entity(kind, state, deadline);
CREATE INDEX entity_key ON entity(kind, natural_key);
CREATE TABLE event(entity_id TEXT NOT NULL, at REAL NOT NULL, from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE INDEX event_entity ON event(entity_id, at);
CREATE INDEX event_at ON event(at);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def entity(row):
    keys = [c.strip() for c in COLUMNS.split(",")]
    value = dict(zip(keys, row))
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


class Store:
    """One profile's state-v2.db. Every write is a BEGIN IMMEDIATE transaction; transitions are CAS."""

    def __init__(self, path, *, readonly=False, clock=time.time):
        self.path, self.readonly, self.clock = Path(path), readonly, clock
        self._local = threading.local()
        if readonly:
            return
        with self.atomic() as store:
            db = store._db
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA:
                raise ValueError("state-v2.db is newer than this plugin; upgrade before opening")
            if version == 0:
                for statement in filter(str.strip, DDL.split(";")):
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={SCHEMA}")
        self.path.chmod(0o600)

    @classmethod
    def for_home(cls, home, **kwargs):
        return cls(Path(home) / "muse" / "state-v2.db", **kwargs)

    @property
    def _db(self):
        return getattr(self._local, "db", None)

    @contextmanager
    def atomic(self):
        """Reentrant transaction; nested calls join the outer one."""
        if self._db is not None:
            yield self
            return
        if self.readonly:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=15, isolation_level=None)
        else:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            if not self.readonly:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN" if self.readonly else "BEGIN IMMEDIATE")
            self._local.db = db
            yield self
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            self._local.db = None
            db.close()

    # -- reads -----------------------------------------------------------------------------------
    def get(self, entity_id):
        with self.atomic():
            row = self._db.execute(f"SELECT {COLUMNS} FROM entity WHERE id=?", (entity_id,)).fetchone()
        return entity(row) if row else None

    def query(self, kind, *, state=None, natural_key=None, since=None, limit=None):
        sql, args = f"SELECT {COLUMNS} FROM entity WHERE kind=?", [kind]
        if state is not None:
            states = [state] if isinstance(state, str) else list(state)
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        if natural_key is not None:
            sql, args = sql + " AND natural_key=?", args + [natural_key]
        if since is not None:
            sql, args = sql + " AND state_since>=?", args + [since]
        sql += " ORDER BY state_since DESC, id"
        if limit is not None:
            sql, args = sql + " LIMIT ?", args + [int(limit)]
        with self.atomic():
            return [entity(r) for r in self._db.execute(sql, args)]

    def due(self, now=None, *, kind=None, limit=None):
        """Non-terminal entities whose deadline has passed, oldest first (uses the kind/state/deadline index)."""
        now = self.clock() if now is None else now
        result = []
        with self.atomic():
            for name, m in MACHINES.items():
                if kind not in (None, name):
                    continue
                for state in m.ttl:
                    result += [entity(r) for r in self._db.execute(
                        f"SELECT {COLUMNS} FROM entity WHERE kind=? AND state=? AND deadline<=? ORDER BY deadline",
                        (name, state, now))]
        result.sort(key=lambda e: e["deadline"])
        return result[:limit] if limit is not None else result

    def events(self, entity_id):
        with self.atomic():
            return [dict(zip(("at", "from_state", "to_state", "reason"), r)) for r in self._db.execute(
                "SELECT at, from_state, to_state, reason FROM event WHERE entity_id=? ORDER BY at, rowid", (entity_id,))]

    def meta(self, key, default=None):
        with self.atomic():
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # -- writes ----------------------------------------------------------------------------------
    def set_meta(self, key, value):
        with self.atomic():
            self._db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def create(self, kind, natural_key, payload=None, *, state=None, deadline=None, id=None, now=None,
               reason="create", dedupe=True):
        """Insert an entity, or return the existing one (``existing=True``) for a replayed id or natural key.

        dedupe: True uses the machine's rule, False skips it, or pass (states, window seconds).
        """
        m, now = machine(kind), self.clock() if now is None else now
        state = state or m.initial
        if state not in m.states:
            raise ValueError(f"{kind} has no state {state!r}")
        natural_key = str(natural_key)
        with self.atomic():
            found = self.get(id) if id else None
            if not found and dedupe:
                states, window = m.dedupe if dedupe is True else dedupe
                sql, args = f"SELECT {COLUMNS} FROM entity WHERE kind=? AND natural_key=?", [kind, natural_key]
                if states is not None:
                    sql += f" AND state IN ({','.join('?' * len(states))})"
                    args += sorted(states)
                if window is not None:
                    sql, args = sql + " AND state_since>=?", args + [now - window]
                row = self._db.execute(sql + " ORDER BY state_since DESC LIMIT 1", args).fetchone()
                found = entity(row) if row else None
            if found:
                return {**found, "existing": True}
            entity_id = id or f"{kind}:{uuid.uuid4().hex[:16]}"
            self._db.execute(f"INSERT INTO entity({COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?)", (
                entity_id, kind, natural_key, state, now, self._deadline(m, state, deadline, now), 0, 1,
                dumps(payload or {}), now))
            self._event(entity_id, now, None, state, reason)
            return {**self.get(entity_id), "existing": False}

    def transition(self, entity_id, to_state, *, state, version, reason, deadline=None, payload=None, now=None):
        """CAS: apply only if the entity is still in ``state`` at ``version``; return the new entity or None.

        A same-state transition patches payload/deadline. ``payload`` is merged into the stored payload.
        """
        now = self.clock() if now is None else now
        with self.atomic():
            current = self.get(entity_id)
            if current is None:
                raise KeyError(entity_id)
            m = machine(current["kind"])
            if to_state != state and (state, to_state) not in m.edges:
                raise ValueError(f"{current['kind']}: {state} -> {to_state} is not a valid transition")
            if to_state == state and deadline is None and not m.terminal(state):
                deadline = current["deadline"]
            new_deadline = self._deadline(m, to_state, deadline, now)
            merged = {**current["payload"], **(payload or {})}
            changed = self._db.execute(
                "UPDATE entity SET state=?, state_since=CASE WHEN state=? THEN state_since ELSE ? END, deadline=?,"
                " version=version+1, payload_json=?, updated=? WHERE id=? AND state=? AND version=?",
                (to_state, to_state, now, new_deadline, dumps(merged), now, entity_id, state, version)).rowcount
            if not changed:
                return None
            self._event(entity_id, now, state, to_state, reason)
            return self.get(entity_id)

    def attempt(self, ids, now=None):
        """Count one more hand-out (e.g. a signal placed in a packet); tick() exits at max_attempts."""
        now = self.clock() if now is None else now
        with self.atomic():
            for entity_id in ids:
                self._db.execute("UPDATE entity SET attempts=attempts+1, version=version+1, updated=? WHERE id=?", (now, entity_id))

    def tick(self, now=None):
        """Apply every due exit exactly once; idempotent and safe to call from concurrent processes."""
        now = self.clock() if now is None else now
        moved = []
        with self.atomic():
            for kind, m in MACHINES.items():
                for state, (_, exit_state) in m.ttl.items():
                    if exit_state is None:
                        continue
                    rows = self._db.execute(f"SELECT {COLUMNS} FROM entity WHERE kind=? AND state=? AND deadline<=?",
                                            (kind, state, now)).fetchall()
                    if m.max_attempts and state == m.initial:
                        rows += self._db.execute(f"SELECT {COLUMNS} FROM entity WHERE kind=? AND state=? AND attempts>=?"
                                                 " AND deadline>?", (kind, state, m.max_attempts, now)).fetchall()
                    for row in map(entity, rows):
                        reason = "ttl" if row["deadline"] <= now else "attempts"
                        if self.transition(row["id"], exit_state, state=state, version=row["version"], reason=reason, now=now):
                            moved.append({"id": row["id"], "kind": kind, "from": state, "to": exit_state, "reason": reason})
            self._db.execute("DELETE FROM event WHERE at<?", (now - EVENT_DAYS * 86400,))
        return moved

    def _event(self, entity_id, at, from_state, to_state, reason):
        self._db.execute("INSERT INTO event VALUES(?,?,?,?,?)", (entity_id, at, from_state, to_state, str(reason)[:500]))

    @staticmethod
    def _deadline(m, state, deadline, now):
        if m.terminal(state):
            return None
        if deadline is not None:
            return float(deadline)
        seconds = m.ttl[state][0]
        if seconds is None:
            raise ValueError(f"State {state!r} needs an explicit deadline")
        return now + seconds
