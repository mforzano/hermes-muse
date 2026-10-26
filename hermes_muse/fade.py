"""Goal dormancy and fade, derived from stored data; no goal status or timestamp is added.

An active goal with no activity for goal_dormant_days is dormant; goal_fade_days later the nightly
review may cancel it, but only after its dormancy question was delivered at least a day earlier.
Activity is the latest of: creation, any goal_update (progress, owner, deadline, next check; they
all set `updated`), the end of a deadline or watch that exempted the goal, a user signal that
literally contains the goal ID or title, and the same for an active subgoal. Read-only: callers
decide what to do with the answer. Whatever cannot be read means "do not fade".
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .store import DAY, digest

DEFAULTS = {"goal_dormant_days": 7, "goal_fade_days": 7}
LIVE_OWNER = {"running", "stalling", "finalizing", "claimed"}
UNSURE = "its fade data could not be read; retry later"


def settings(home):
    """(preferences with defaults, readable). A missing file means defaults; a present but invalid one means unreadable."""
    path = Path(home) / "muse/PROACTIVE_PREFERENCES.md"
    if not path.exists():
        return dict(DEFAULTS), True
    try:
        line = path.read_text(encoding="utf-8").splitlines()[0]
        stored = json.loads(line[len("<!-- hermes-muse "):-len(" -->")])
        result = {}
        for key, default in DEFAULTS.items():
            value = stored.get(key, default)
            if value is not None and not (type(value) is int and 1 <= value <= 365):
                return dict(DEFAULTS), False
            result[key] = value
        return result, True
    except (OSError, ValueError, IndexError, AttributeError):
        return dict(DEFAULTS), False


def ask_key(goal_id, dormant_since):
    """One question per dormancy episode: revival moves dormant_since, so the next episode gets a new key."""
    return f"goal-fade-ask:{goal_id}:{time.strftime('%Y%m%d', time.gmtime(dormant_since))}"


def _native_watch_check(home):
    """Host.is_active_watch bound to this profile without opening Muse's store."""
    from .host import HermesHost

    class _Paths:
        def __init__(self, root):
            self.home = Path(root).resolve()

        def path(self, relative):
            return self.home / "muse" / relative

    return HermesHost(_Paths(home)).is_active_watch


class Fade:
    def __init__(self, home, goals, now=None, is_active_watch=None):
        self.home, self.goals = Path(home), goals
        self.now = time.time() if now is None else now
        self.cfg, readable = settings(home)
        self.unsure = not readable
        self._signals = self._records = None
        self._is_active_watch = is_active_watch

    @property
    def enabled(self):
        return self.cfg["goal_dormant_days"] is not None

    def _query(self, sql, args=()):
        try:
            with sqlite3.connect(f"file:{self.home / 'muse/state.db'}?mode=ro", uri=True, timeout=2) as db:
                db.execute("PRAGMA query_only=ON")
                return [json.loads(r[0]) for r in db.execute(sql, args)]
        except (sqlite3.Error, ValueError):
            self.unsure = True
            return []

    def signals(self):
        if self._signals is None:
            rows = self._query("SELECT data FROM records WHERE kind='signal'")
            self._signals = [(r.get("created", 0), str(r.get("text", "")).casefold()) for r in rows]
        return self._signals

    def watches(self, goal_id):
        """(job_id, expires) of every Muse watch record owned by the goal."""
        if self._records is None:
            try:
                jobs = json.loads((self.home / "muse/install.json").read_text(encoding="utf-8")).get("jobs", {})
                self._records = [(job_id, r.get("owner"), r.get("expires")) for job_id, r in jobs.items()
                                 if str(r.get("key", "")).startswith("watch-")]
            except FileNotFoundError:
                self._records = []
            except (OSError, ValueError, AttributeError):
                self._records, self.unsure = [], True
        return [(job_id, expires) for job_id, owner, expires in self._records if owner == goal_id]

    def children(self, goal):
        return [g for g in self.goals if g.get("parent_id") == goal["id"] and g.get("status") == "active"]

    def last_activity(self, goal):
        mentions = (goal["id"].casefold(), str(goal.get("title", "")).casefold())
        latest = max(goal.get("created", 0), goal.get("updated", 0))
        if not latest:
            return self.now  # a record without timestamps has an unknown age; it never fades
        # The end of a deadline or watch is activity: the clock restarts when the exemption lapses.
        ends = [t for t in (goal.get("deadline"), *(e for _, e in self.watches(goal["id"]))) if t and t <= self.now]
        latest = max([latest, *ends])
        latest = max([latest, *(s for s, text in self.signals() if s > latest and any(len(m) >= 3 and m in text for m in mentions))])
        return max([latest, *(self.last_activity(child) for child in self.children(goal))])

    def exemption(self, goal):
        if goal.get("no_fade"):
            return "it is marked no_fade"
        if (goal.get("deadline") or 0) > self.now:
            return "it has a future deadline"
        for job_id, expires in self.watches(goal["id"]):
            if expires is not None and expires <= self.now:
                continue
            try:
                check = self._is_active_watch or _native_watch_check(self.home)
                if check(job_id, goal["id"]):
                    return "it has an active watch"
            except Exception:  # unreadable watch state: do not fade
                self.unsure = True
        from .followup import owner_status
        status = owner_status(self.home, goal.get("owner"))
        if status in LIVE_OWNER:
            return "its owner is still running"
        if status == "unknown" and (goal.get("owner") or {}).get("kind") in {"native_delegation", "cron"}:
            return "its owner's status could not be read"
        for child in self.children(goal):
            reason = self.exemption(child)
            if reason:
                return f"an active subgoal is exempt ({reason[3:] if reason.startswith('it ') else reason})"
        return None

    def asked(self, key, goal_id):
        """None when the dormancy question for this goal was delivered at least a day ago, else why the cancel must wait.
        Only status "sent" counts: an expired, failed, cancelled, dismissed or unknown notice never reached the user."""
        rows = self._query("SELECT data FROM records WHERE kind='notification' AND id=?", (digest(key),))
        if not rows:
            return "the dormancy question has not been asked yet"
        row = rows[0]
        if row.get("status") != "sent" or row.get("goal_id") != goal_id:
            return "the dormancy question has not been delivered yet"
        sent = row.get("dispatched_at", row.get("created"))
        if type(sent) not in (int, float):
            self.unsure = True
            return UNSURE
        if sent > self.now - DAY:
            return "the dormancy question was sent less than a day ago"
        return None

    def of(self, goal):
        """None for a goal that is not active; otherwise state, dormant_since, fade_at, fade_due, ask_key, wait, exempt."""
        try:
            return self._of(goal)
        except Exception:  # malformed stored values (for example a text timestamp): do not fade, do not crash callers
            self.unsure = True
            return None if goal.get("status") != "active" else {"state": "active", "dormant_since": None, "fade_at": None, "fade_due": False,
                                                                  "ask_key": None, "wait": UNSURE, "exempt": UNSURE}

    def _of(self, goal):
        if goal.get("status") != "active":
            return None
        view = {"state": "active", "dormant_since": None, "fade_at": None, "fade_due": False, "ask_key": None, "wait": None, "exempt": None}
        days = self.cfg["goal_dormant_days"]
        if days is None:
            return view
        since = self.last_activity(goal) + days * DAY
        if self.now < since and not self.unsure:
            return view
        view["exempt"] = self.exemption(goal) or (UNSURE if self.unsure else None)
        if view["exempt"] or self.now < since:
            return view
        fade_days = self.cfg["goal_fade_days"]
        key = ask_key(goal["id"], since)
        view.update(state="dormant", dormant_since=since, ask_key=key, wait=self.asked(key, goal['id']))
        if fade_days is not None:
            view["fade_at"] = since + fade_days * DAY
            view["fade_due"] = self.now >= view["fade_at"] and view["wait"] is None and not self.unsure
        if self.unsure:
            view["wait"] = UNSURE
        return view

    def dormant(self, goal):
        view = self.of(goal)
        return bool(view and view["state"] == "dormant")
