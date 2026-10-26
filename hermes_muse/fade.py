"""Goal dormancy and fade, derived from stored data; no goal status or timestamp is added.

An active goal with no activity for goal_dormant_days is dormant; goal_fade_days later the nightly
review may cancel it. Activity is the latest of: creation, any goal_update (progress, owner,
deadline, next check; they all set `updated`), a user signal that literally contains the goal ID or
title, and the same for an active subgoal. Read-only: callers decide what to do with the answer.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .store import DAY

DEFAULTS = {"goal_dormant_days": 7, "goal_fade_days": 7}
LIVE_OWNER = {"running", "stalling", "finalizing", "claimed"}


def settings(home):
    """Preferences with defaults; an unreadable or invalid value falls back to its default."""
    try:
        line = (Path(home) / "muse/PROACTIVE_PREFERENCES.md").read_text(encoding="utf-8").splitlines()[0]
        stored = json.loads(line[len("<!-- hermes-muse "):-len(" -->")])
    except (OSError, ValueError, IndexError):
        stored = {}
    result = {}
    for key, default in DEFAULTS.items():
        value = stored.get(key, default)
        result[key] = value if value is None or type(value) is int and 1 <= value <= 365 else default
    return result


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Fade:
    def __init__(self, home, goals, now=None):
        self.home, self.goals = Path(home), goals
        self.now = time.time() if now is None else now
        self.cfg = settings(home)
        self._signals = self._watches = None

    @property
    def enabled(self):
        return self.cfg["goal_dormant_days"] is not None

    def signals(self):
        if self._signals is None:
            try:
                with sqlite3.connect(f"file:{self.home / 'muse/state.db'}?mode=ro", uri=True, timeout=2) as db:
                    db.execute("PRAGMA query_only=ON")
                    rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind='signal'")]
            except (sqlite3.Error, ValueError):
                rows = []
            self._signals = [(r.get("created", 0), str(r.get("text", "")).casefold()) for r in rows]
        return self._signals

    def watching(self, goal_id):
        """An unexpired watch of this goal whose native job is still enabled."""
        if self._watches is None:
            enabled = {j.get("id") for j in _json(self.home / "cron/jobs.json").get("jobs", []) if j.get("enabled")}
            self._watches = {}
            for job_id, record in _json(self.home / "muse/install.json").get("jobs", {}).items():
                if str(record.get("key", "")).startswith("watch-") and job_id in enabled and (record.get("expires") or self.now + 1) > self.now:
                    self._watches[record.get("owner")] = True
        return goal_id in self._watches

    def last_activity(self, goal):
        mentions = (goal["id"].casefold(), str(goal.get("title", "")).casefold())
        latest = max(goal.get("created", 0), goal.get("updated", 0))
        if not latest:
            return self.now  # a record without timestamps has an unknown age; it never fades
        latest = max([latest, *(s for s, text in self.signals() if s > latest and any(len(m) >= 3 and m in text for m in mentions))])
        children = [g.get("updated", 0) for g in self.goals if g.get("parent_id") == goal["id"] and g.get("status") == "active"]
        return max([latest, *children])

    def exemption(self, goal):
        if (goal.get("deadline") or 0) > self.now:
            return "it has a future deadline"
        if self.watching(goal["id"]):
            return "it has an active watch"
        from .followup import owner_status
        if owner_status(self.home, goal.get("owner")) in LIVE_OWNER:
            return "its owner is still running"
        return None

    def of(self, goal):
        """None for a goal that is not active; otherwise state, dormant_since, fade_at, fade_due, exempt."""
        if goal.get("status") != "active":
            return None
        view = {"state": "active", "dormant_since": None, "fade_at": None, "fade_due": False, "exempt": None}
        days = self.cfg["goal_dormant_days"]
        if days is None:
            return view
        since = self.last_activity(goal) + days * DAY
        if self.now < since:
            return view
        view["exempt"] = self.exemption(goal)
        if view["exempt"]:
            return view
        fade_days = self.cfg["goal_fade_days"]
        view.update(state="dormant", dormant_since=since)
        if fade_days is not None:
            view.update(fade_at=since + fade_days * DAY, fade_due=self.now >= since + fade_days * DAY)
        return view

    def dormant(self, goal):
        view = self.of(goal)
        return bool(view and view["state"] == "dormant")
