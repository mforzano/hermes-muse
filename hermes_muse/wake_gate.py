"""Optional wake gate for Muse's high-frequency Cron jobs.

Off by default. When enabled, the job's pre-run script asks a System One model (TypeSafe Jev) one
yes/no question about a bounded summary of what the run would review. A low probability skips that
run, so the agent is not woken for nothing.

The gate is read-only apart from its own log line: it never advances a cursor or consumes a signal,
so a skipped run's signals are still there next time. Anything that goes wrong (no key, timeout,
HTTP or parse error, unreadable state) wakes the agent.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

from .store import PLUGIN, is_background_message

GATED_JOBS = ("proactive-watch", "memory-upkeep", "feed-pulse")
DEFAULTS = {
    "mode": "off",                     # "off" | "typesafe"
    "jobs": list(GATED_JOBS),          # which of the jobs above are gated
    "min_probability": 0.15,           # skip only when P(yes) is below this; lower = wake more often
    "timeout_seconds": 3.0,            # whole System One call; on expiry the agent is woken
    "max_skip_minutes": 120,           # never skip longer than this (0 = no limit); external sources
                                       # such as mail are only seen by the agent
    "include_message_text": True,      # false: send counts and ages only, no message text
    "model": "jev-latest",
    "base_url": "https://api.typesafe.ai/v1",
    "api_key_env": "TYPESAFE_API_KEY",
}
LOG_NAME = "hermes-muse-wake-gate.jsonl"
LOG_MAX_BYTES = 262144
BACKLOG_WAKE = 25          # as many unprocessed messages as one review batch: always run
TEXT_CHARS = 240
SOON = 7200                # "soon" for follow-ups and deadlines: two hours
LIST_LIMIT = 8

PURPOSE = {
    "proactive-watch": "A periodic patrol that checks the user's accepted follow-ups, upcoming events and "
                       "incoming messages and decides whether the user should be reminded of something.",
    "memory-upkeep": "A periodic review that turns the user's recent messages into durable memory, "
                     "goals and interests.",
    "feed-pulse": "A periodic job that writes a short article for the user's quiet reading feed when "
                  "something fits their active interests.",
}
# Judged from the summary alone, as a change since the previous run: Muse cannot see mail or calendars,
# so a standing condition (an old overdue goal, a stale source) must not read as news.
QUESTIONS = {
    "proactive-watch": {
        "instructions": "Would a run of this periodic job (`what_this_job_does`) right now have anything new "
                        "to act on or to tell the user about, judging only from this summary?",
        "criteria": {
            "true": "There is at least one new user message with substance (a request, decision, plan, "
                    "fact, preference, deadline or interest), or a follow-up or deadline falls due within "
                    "the next two hours.",
            "false": "There are no new user messages, or only acknowledgements and small talk, and no "
                     "follow-up or deadline falls due within the next two hours."}},
    "memory-upkeep": {
        "instructions": "Would a run of this periodic job (`what_this_job_does`) right now find anything "
                        "worth remembering in the new user messages of this summary?",
        "criteria": {
            "true": "A new message may hold durable information: a preference, decision, plan, fact about "
                    "the user or other people, correction, routine, or an interest or goal.",
            "false": "There are no new messages, or only acknowledgements, one-off commands and small talk."}},
    "feed-pulse": {
        "instructions": "Would a run of this periodic job (`what_this_job_does`) right now be likely to "
                        "write an article worth reading, judging only from this summary?",
        "criteria": {
            "true": "The user has recent or active interests, new messages that mention a topic of "
                    "interest, or the newest article is several hours old.",
            "false": "An article was written within the last couple of hours and nothing new about the "
                     "user's interests has appeared since."}},
}


def settings(raw):
    """Normalise the user's `plugins.hermes-muse.wake_gate` mapping; bad values fall back to defaults."""
    raw = raw if isinstance(raw, dict) else {}
    cfg = dict(DEFAULTS)
    cfg["jobs"] = list(DEFAULTS["jobs"])
    if str(raw.get("mode", "")).strip().lower() == "typesafe":
        cfg["mode"] = "typesafe"
    jobs = raw.get("jobs")
    if isinstance(jobs, (list, tuple)):
        cfg["jobs"] = [j for j in jobs if j in GATED_JOBS]
    for key, low, high in (("min_probability", 0.0, 1.0), ("timeout_seconds", 0.1, 30.0),
                           ("max_skip_minutes", 0, 10080)):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
            cfg[key] = value
    if isinstance(raw.get("include_message_text"), bool):
        cfg["include_message_text"] = raw["include_message_text"]
    for key in ("model", "base_url", "api_key_env"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            cfg[key] = value.strip()
    if not cfg["base_url"].startswith(("https://", "http://")):
        cfg["base_url"] = DEFAULTS["base_url"]
    return cfg


def log_path(home):
    return Path(home) / "logs" / LOG_NAME


def _log(home, entry):
    """One metadata-only JSON line: job, probability, decision, reason and timing. Never message text."""
    try:
        path = log_path(home)
        if os.name == "nt":
            from .content_io import Files, revision
            files = Files(home, workspace="")
            relative = "logs/" + LOG_NAME
            try:
                before = files.read(relative)
            except FileNotFoundError:
                before = None
            raw = (before or b"") + (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(raw) > LOG_MAX_BYTES:
                tail = raw[-LOG_MAX_BYTES // 2:]
                raw = tail[tail.find(b"\n") + 1:]
            files.replace(relative, raw, revision(before) if before is not None else "absent")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
        if path.stat().st_size > LOG_MAX_BYTES:
            tail = path.read_bytes()[-LOG_MAX_BYTES // 2:]
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(tail[tail.find(b"\n") + 1:])  # drop the cut first line
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
    except (OSError, ValueError):
        pass  # Logging must never decide whether the agent runs.


def last_wake(home, job):
    """Epoch of this job's latest logged wake, or None. The log is the gate's only state."""
    try:
        for line in reversed(log_path(home).read_text(encoding="utf-8").splitlines()):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("job") == job and entry.get("skipped") is False:
                return float(entry["at"])
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return None


def _records(home, kind):
    """Read-only, lock-free read of one record kind; the gate never takes the mutation lock."""
    path = Path(home) / "muse" / "state.db"
    from .content_io import readonly_database
    with readonly_database(path) as db:
        db.execute("PRAGMA query_only=ON")
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE kind=?", (kind,))]


def _record(home, kind, key, default=0):
    path = Path(home) / "muse" / "state.db"
    from .content_io import readonly_database
    with readonly_database(path) as db:
        db.execute("PRAGMA query_only=ON")
        row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
    return json.loads(row[0]) if row else default


def collect(store, job, now, since):
    """Plain data the gate decides on. Read-only."""
    home = store.home
    signals = sorted((r for r in _records(home, "signal") if not is_background_message(r.get("text"))),
                     key=lambda r: r["created"])
    cursor = _record(home, "cursor", job)
    from .fade import Fade
    goals = store.goals()
    fade = Fade(home, goals, now)
    goals = [g for g in goals if not fade.dormant(g)]  # dormant goals neither wake nor count as active
    notices = [r for r in _records(home, "notification")
               if r.get("status") in {"candidate", "pending"} and r.get("expires", 0) > now]
    interests = [r for r in _records(home, "interest")
                 if r.get("expires", 0) > now and r.get("not_before", 0) <= now
                 and r.get("status") in {"active", "candidate", "accepted"}]
    feed = _records(home, "feed")
    return {"signals": signals, "cursor": cursor, "goals": goals, "notices": notices, "interests": interests,
            "feed": feed, "since": since,
            "feed_requested": _record(home, "meta", "feed_requested")}


def must_run(job, data, now):
    """Muse's own state already requires a run: a follow-up or review that became due since the last
    wake, live notices, an explicit Feed request, or a message backlog as large as one review batch.
    These never go through the model. A goal that has been overdue for days is not a reason: Cron
    cannot clear it, so it would wake every tick."""
    since = data["since"]
    for goal in data["goals"]:
        if goal.get("status") == "active" and any(
                since < goal.get(field, 0) <= now for field in ("next_check_at", "research_review_at")):
            return "due_goal"
    if data["notices"]:
        return "live_notice"
    if job == "feed-pulse" and (data["feed_requested"] or 0) > data["cursor"]:
        return "feed_requested"
    if job == "memory-upkeep" and sum(s["created"] > data["cursor"] for s in data["signals"]) >= BACKLOG_WAKE:
        return "backlog"
    return None


def _minutes(now, then):
    return max(0, round((now - then) / 60))


def _message(row, now, include_text):
    item = {"minutes_ago": _minutes(now, row["created"])}
    if include_text:
        item["text"] = str(row.get("text", "")).strip()[:TEXT_CHARS]
    return item


def digest(job, data, now, local_time, include_text=True):
    """Bounded summary handed to the model: counts, ages and clipped text, never whole records."""
    # Memory-upkeep owns a processing cursor; the other jobs look at what arrived since they last ran.
    floor = data["cursor"] if job == "memory-upkeep" else max(data["cursor"], data["since"] or 0)
    new = [s for s in data["signals"] if s["created"] > floor]
    state = {
        "job": job, "what_this_job_does": PURPOSE[job], "local_time": local_time,
        "minutes_since_last_run": None if data["since"] is None else _minutes(now, data["since"]),
        "new_user_message_count": len(new),
        "newest_new_user_messages": [_message(s, now, include_text) for s in new[-LIST_LIMIT:]],
    }
    if job == "proactive-watch":
        active = [g for g in data["goals"] if g.get("status") == "active"]
        soon = sorted((g for g in active if 0 < g.get("next_check_at", 0) - now <= SOON),
                      key=lambda g: g["next_check_at"])
        state["active_goals"] = {"count": len(active), "next_check_within_2h": [
            {"title": str(g.get("title", ""))[:120] if include_text else None,
             "minutes_until_check": round((g["next_check_at"] - now) / 60)} for g in soon[:LIST_LIMIT]],
            "deadlines_within_2h": sum(0 < (g.get("deadline") or 0) - now <= SOON for g in active)}
    if job == "feed-pulse":
        recent = [f for f in data["feed"] if f.get("created", 0) > now - 86400]
        state["active_interests"] = {"count": len(data["interests"]), "titles": [
            str(i.get("title", ""))[:120] for i in data["interests"][:LIST_LIMIT]] if include_text else []}
        state["feed_articles_last_24h"] = len(recent)
        state["minutes_since_newest_article"] = (
            _minutes(now, max(f.get("created", 0) for f in data["feed"])) if data["feed"] else None)
    return state


def question(job):
    return {"type": "noul", **QUESTIONS[job]}


def ask_systemone(cfg, state, job):
    """POST one System One request and return P(yes). Raises on any failure."""
    key = os.environ.get(cfg["api_key_env"], "").strip()
    if not key:
        raise LookupError("api key missing")
    body = {"model": cfg["model"], "state": state, "questions": {"run": question(job)}}
    request = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/systemone", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                 "User-Agent": "hermes-muse"})
    with urllib.request.urlopen(request, timeout=cfg["timeout_seconds"]) as response:
        probability = float(json.load(response)["answers"]["run"]["noul"])
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability out of range")
    return probability


def evaluate(store, job, cfg, *, clock=time.time, ask=ask_systemone, local_time=None):
    """Decide whether to wake the agent. Returns (wake, probability, reason). Never raises."""
    now = clock()
    try:
        since = last_wake(store.home, job)
        if since is None:
            return True, None, "first_run"
        if cfg["max_skip_minutes"] and now - since >= cfg["max_skip_minutes"] * 60:
            return True, None, "max_skip"
        data = collect(store, job, now, since)
        reason = must_run(job, data, now)
        if reason:
            return True, None, reason
        local_time = local_time or time.strftime("%A %Y-%m-%d %H:%M", time.localtime(now))
        probability = ask(cfg, digest(job, data, now, local_time, cfg["include_message_text"]), job)
        if probability < cfg["min_probability"]:
            return False, probability, "below_threshold"
        return True, probability, "above_threshold"
    except Exception as exc:  # fail open: a gate problem must never cost a run
        return True, None, "error:" + type(exc).__name__


def apply(store, job, result, cfg, *, clock=time.time, ask=ask_systemone, local_time=None):
    """Return `result` unchanged unless the gate is on for `job` and decides to skip this run."""
    if cfg.get("mode") != "typesafe" or job not in cfg["jobs"] or not result.get("wakeAgent"):
        return result
    started = time.monotonic()
    wake, probability, reason = evaluate(store, job, cfg, clock=clock, ask=ask, local_time=local_time)
    _log(store.home, {"at": round(clock(), 3), "job": job, "probability": probability, "skipped": not wake,
                      "reason": reason, "ms": round((time.monotonic() - started) * 1000)})
    if wake:
        return result
    return {"wakeAgent": False, "job": job, "reason": "wake_gate", "probability": probability}
