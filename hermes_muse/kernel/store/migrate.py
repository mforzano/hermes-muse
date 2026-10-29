"""Non-destructive 0.3.1 / 0.3.16 -> Store v2 migration.

Reads muse/state.db read-only, the GOAL.md headers and muse/install.json; writes muse/state-v2.db.
Every v1 record ends up as an entity or under a reason code in the report (acceptance A2).
Run offline on a stopped profile (or a copy); --dry-run writes nothing in the home.
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from .keys import notice_key, signal_key
from .store import Store

D = 86400
BACKGROUND = ('[Cronjob "', '[ASYNC DELEGATION ', '[IMPORTANT:', '[Hermes Muse notification ',
              '[Hermes Muse background evidence]', '[Hermes Muse internal delivery]')
NOTICE = {"candidate": "drafted", "pending": "scheduled", "queued": "sending", "dispatching": "sending",
          "transport_queued": "sending", "sent": "sent", "unknown": "unknown", "failed": "failed",
          "cancelled": "withdrawn", "dismissed": "withdrawn", "expired": "expired"}
INTEREST = {"candidate": "active", "active": "active", "accepted": "active", "expired": "expired"}
SOURCE = {"checked": "ok", "partial": "limited", "error": "failing", "unavailable": "unavailable"}
LIVE = {"drafted", "scheduled"}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def signal_revision(row):
    """0.3.16 context_pages.signal_revision: the processed marker is valid only for this exact row."""
    return hashlib.sha256(canonical({k: v for k, v in row.items() if k != "upkeep"})).hexdigest()


def background(text):
    return isinstance(text, str) and text.lstrip().startswith(BACKGROUND)


def read_v1(home):
    path = Path(home) / "muse" / "state.db"
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [(kind, key, json.loads(data)) for kind, key, data in db.execute("SELECT kind, id, data FROM records ORDER BY kind, id")]
    finally:
        db.close()


def goal_headers(home):
    for path in sorted((Path(home) / "muse" / "workspace" / "goals").glob("*/GOAL.md")):
        raw = path.read_bytes()
        first = raw.decode("utf-8").partition("\n")[0].strip()
        try:
            header = json.loads(first[len("<!-- hermes-muse "):-len(" -->")]) if first.startswith("<!-- hermes-muse ") else None
        except ValueError:
            header = None
        yield path.parent.name, header, hashlib.sha256(raw).hexdigest()


def migrate(home, *, dry_run=False, now=None):
    home = Path(home)
    target = home / "muse" / "state-v2.db"
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="muse-v2-dry-") as scratch:
            return {**_migrate(home, Store(Path(scratch) / "state-v2.db"), now), "dry_run": True}
    if target.exists() and Store(target).meta("migration"):
        return {"migrated": False, "reason": "already_migrated", **Store(target).meta("migration")["report"]}
    report = _migrate(home, Store(target), now)
    (home / "muse" / "state.db").chmod(0o400)  # v1 stays as read-only history; rollback-bridge reopens it
    return report


def _migrate(home, store, now):
    now = time.time() if now is None else float(now)
    records = read_v1(home)
    skipped, merged, v1_counts = Counter(), Counter(), Counter(k for k, _, _ in records)
    cursor = {key: data for kind, key, data in records if kind == "cursor"}
    upkeep_cursor = cursor.get("memory-upkeep", 0)
    shape = "0.3.16" if any(k == "signal" and "upkeep" in d for k, _, d in records) or v1_counts["mutation_request"] else "0.3.1"

    def put(kind, natural_key, v1_kind, v1_id, payload, state, deadline=None, since=None, why=""):
        store.create(kind, natural_key, {**payload, "v1_id": v1_id}, state=state, deadline=deadline,
                     id=f"{kind}:{v1_id}", now=since if since is not None else now, dedupe=False,
                     reason=f"migrate:{v1_kind}:{why or state}")

    with store.atomic():
        notices = []
        for kind, key, row in records:
            if kind == "signal":
                if background(row.get("text")):
                    skipped["signal:background"] += 1
                    continue
                marker = row.get("upkeep")
                done = marker.get("revision") == signal_revision(row) if marker else row["created"] <= upkeep_cursor
                payload = {"session_id": row["session_id"], "created": row["created"], "excerpt": row.get("text", ""),
                           "truncated": bool(row.get("truncated")), "pointer": {"session_id": row["session_id"], "turn_id": key}}
                if done:
                    put("signal", signal_key(row["session_id"], key), kind, key, {**payload, "disposition": "context"},
                        "disposed", since=row["created"], why="processed")
                else:
                    put("signal", signal_key(row["session_id"], key), kind, key, payload, "new",
                        deadline=max(row["created"], now) + 3 * D, since=row["created"])
            elif kind == "notification":
                notices.append(row)
            elif kind == "interest":
                put(row.get("kind") if row.get("kind") == "idea" else "interest", key, kind, key,
                    {f: row.get(f) for f in ("title", "rationale", "goal_id", "session_id", "source_signal", "sources", "last_signal")},
                    INTEREST.get(row.get("status"), "stopped"), deadline=row.get("expires"), why=row.get("status"))
            elif kind == "feed":
                sources = sorted(row.get("sources") or [])
                put("feed", hashlib.sha256(canonical(sources)).hexdigest()[:24], kind, key,
                    {f: row.get(f) for f in ("title", "path", "created", "position", "feedback", "sources", "why", "goal_id", "interest_id")},
                    "published", since=row.get("created"))
            elif kind == "source_check":
                put("source", key, kind, key,
                    {f: row.get(f) for f in ("source", "status", "summary", "last_success", "cursor", "missing_resources", "finished_at")},
                    SOURCE.get(row.get("status"), "failing"), since=row.get("finished_at"), why=row.get("status"))
            elif kind == "memory_feedback":
                if row.get("classified_state") not in {"failed", "staged"} or row.get("invalidated") or row.get("reply_ref") or row.get("decision") == "silent":
                    skipped["memory_feedback:not_noticeable"] += 1
                    continue
                fresh = row.get("created", 0) >= now - D
                put("receipt", row.get("tool_call_id") or key, kind, key,
                    {f: row.get(f) for f in ("session_id", "turn_id", "tool", "classified_state", "created")},
                    "pending_notice" if fresh else "noticed", why="recent" if fresh else "already_repeated")
            elif kind == "operation_finding":
                state = {"open": "open", "repairing": "open", "resolved": "resolved"}.get(row.get("status"), "dismissed")
                put("finding", f"legacy:{key}", kind, key,
                    {f: row.get(f) for f in ("summary", "impact", "evidence", "verification", "disclosure", "updated")},
                    state, deadline=now + 14 * D if state == "open" else None, why=row.get("status"))
            elif kind == "forgotten":
                put("tombstone", key, kind, key, {"at": row}, "forgotten")
            else:
                skipped[f"{kind}:retired"] += 1  # run/session/cursor/windows/ledgers stay in the read-only v1 db

        live_v1 = sum(n.get("status") in {"candidate", "pending"} for n in notices)
        _notices(put, notices, now, merged)

        goals, goal_status = 0, {}
        for slug, header, digest in goal_headers(home):
            status = (header or {}).get("status")
            if status not in {"active", "done", "cancelled"}:
                skipped["goal:unmanaged_header"] += 1
                continue
            state = "waiting" if status == "active" and header.get("waiting_user") else status
            goal_status[slug] = status
            put("goal", slug, "goal_md", slug,
                {"revision": digest, "title": header.get("title"), "session_id": header.get("session_id")}, state,
                deadline=header.get("next_check_at") or now if state in {"active", "waiting"} else None, why=status)
            goals += 1

        watches = _watches(put, home, goal_status, now, skipped)

        report = {
            "from_shape": shape, "v1_records": dict(sorted(v1_counts.items())), "goal_files": goals, "watches": watches,
            "entities": {f"{k}:{s}": n for k, s, n in store._db.execute(
                "SELECT kind, state, COUNT(*) FROM entity GROUP BY kind, state ORDER BY kind, state")},
            "skipped": dict(sorted(skipped.items())), "notice_merged": dict(sorted(merged.items())),
            "reconcile": {"notice_live_v1": live_v1,
                          "notice_live_v2": store._db.execute("SELECT COUNT(*) FROM entity WHERE kind='notice' AND state IN ('drafted','scheduled')").fetchone()[0],
                          "signal_processed_v1_shape": sum(1 for k, _, d in records if k == "signal" and not background(d.get("text"))
                                                           and ((d["upkeep"].get("revision") == signal_revision(d)) if d.get("upkeep") else d["created"] <= upkeep_cursor)),
                          "signal_disposed_v2": store._db.execute("SELECT COUNT(*) FROM entity WHERE kind='signal' AND state='disposed'").fetchone()[0]},
        }
        accounted = store._db.execute("SELECT COUNT(*) FROM entity WHERE id NOT LIKE 'goal:%' AND id NOT LIKE 'watch:%'").fetchone()[0]
        report["unexplained"] = len(records) - accounted - sum(skipped.values()) + skipped["goal:unmanaged_header"] + skipped["watch:not_owned"]
        store.set_meta("migration", {"at": now, "report": report})
    return report


def _notices(put, notices, now, merged):
    """Map notices; keep at most one sendable notice per source_key (A2/A5)."""
    groups = defaultdict(list)
    for row in notices:
        snapshot = row.get("source_snapshot") or {}
        title = snapshot.get("title") or str(row.get("message", "")).strip().partition("\n")[0][:200]
        key, strong = notice_key(snapshot.get("item_id"), row.get("sources") or [], title, row.get("created"))
        state = "superseded" if row.get("status") == "cancelled" and row.get("superseded_by") else NOTICE.get(row.get("status"), "withdrawn")
        groups[key].append((row, state, strong))
    for key, group in groups.items():
        sent_recently = any(s in {"sent", "sending", "unknown"} and (r.get("dispatched_at") or r.get("created", 0)) >= now - 7 * D
                            for r, s, strong in group if strong)
        live = sorted((g for g in group if g[1] in LIVE), key=lambda g: (g[0].get("created", 0), g[0]["id"]))
        keep = None if sent_recently else live[-1][0]["id"] if live else None
        for row, state, strong in group:
            why = row.get("status")
            if state in LIVE and row["id"] != keep:
                state, why = "superseded", "already_sent" if sent_recently else "dup_source_key"
                merged[why] += 1
            at = row.get("dispatched_at") or row.get("created", now)
            deadline = {"drafted": row.get("expires"), "scheduled": row.get("expires"),
                        "sending": at + 2 * 3600, "unknown": at + 2 * D}.get(state)
            put("notice", key, "notification", row["id"],
                {**{f: row.get(f) for f in ("event_key", "issue_key", "message", "rationale", "sources", "priority", "topic",
                                            "goal_id", "interest_id", "expires", "created", "verified_at", "source_snapshot",
                                            "dispatched_at", "execution_id", "job_id", "delivery_outcome", "superseded_by")},
                 "strong_key": strong}, state, deadline=deadline or (now if state in LIVE else None), since=at, why=why)


def _watches(put, home, goal_status, now, skipped):
    path = Path(home) / "muse" / "install.json"
    jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs", {}) if path.exists() else {}
    count = Counter()
    for job_id, job in sorted(jobs.items()):
        if not str(job.get("key", "")).startswith("watch-"):
            continue
        owner = job.get("owner")
        if not owner:
            skipped["watch:not_owned"] += 1
            continue
        count[owner] += 1
        open_goal = goal_status.get(owner) == "active"
        # shortcut: a 0.3.x watch without expiry gets 30 days; WP4 renews it while the goal stays active.
        deadline = job.get("expires") or now + 30 * D
        put("watch", f"goal:{owner}:watch:{count[owner]}", "install_json", job_id,
            {"job_id": job_id, "goal_id": owner, "stop_condition": job.get("stop_condition"), "expires": job.get("expires")},
            "active" if open_goal else "stopped", deadline=deadline if open_goal else None, why="goal_open" if open_goal else "goal_closed")
    return sum(count.values())


def copy_home(src, dst):
    """Copy only the Muse state a migration needs (for dry runs and acceptance on copies)."""
    shutil.copytree(Path(src) / "muse", Path(dst) / "muse", ignore=shutil.ignore_patterns("*.lock", "state-v2.db*"))
