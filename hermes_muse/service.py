"""Companion lifecycle rules. No model/provider or messaging dependency."""
from __future__ import annotations

import json
import time
import uuid
from datetime import timedelta

from .calendar import assemble, canonical_window, validate_window, window
from .guidance import NOTHING, fail
from .handoff import render_result
from .operations import Operations
from .runs import Runs
from .store import DAY, GOVERNANCE_JOBS, bounded, digest, epoch, identifier, is_background_message, stamp

DEFAULT_PREFERENCES = {"start_hour": 9, "end_hour": 22, "ordinary_per_day": None,
                       "time_sensitive_per_day": 3, "blocked_topics": [], "feed_brief": "",
                       "deliver": None, "weather_context": False}
# A reviewed relevant notice held this long by conversation churn/activity is released
# to the remaining gates (quiet hours, budget, snooze, topic opt-out, dedup).
HOLD_RELEASE = 3600


class Companion:
    def __init__(self, store, host, clock=time.time):
        self.store, self.host, self.clock = store, host, clock
        self.operations = Operations(store, host, clock)
        self.runs = Runs(store, host, clock)

    def preferences(self):
        text = self.store.path("PROACTIVE_PREFERENCES.md").read_text(encoding="utf-8")
        line = text.splitlines()[0]
        prefix = "<!-- hermes-muse "
        if not line.startswith(prefix) or not line.endswith(" -->"):
            raise ValueError("Preferences metadata is invalid; delivery is blocked until repaired")
        prefs = {**DEFAULT_PREFERENCES, **json.loads(line[len(prefix):-4])}
        for key, high in (("start_hour", 23), ("end_hour", 24), ("ordinary_per_day", 20), ("time_sensitive_per_day", 20)):
            if key.endswith("_per_day") and prefs[key] is None:
                continue
            if type(prefs[key]) is not int or not 0 <= prefs[key] <= high:
                raise ValueError(f"Invalid preference: {key}")
        if not isinstance(prefs["blocked_topics"], list) or any(not isinstance(x, str) for x in prefs["blocked_topics"]):
            raise ValueError("blocked_topics must be a list of topic IDs")
        if prefs["deliver"] is not None and (not isinstance(prefs["deliver"], str) or not prefs["deliver"].strip()):
            raise ValueError("deliver must be a non-empty string or null")
        if type(prefs["weather_context"]) is not bool:
            raise ValueError("weather_context must be true or false")
        return prefs

    def preference_update(self, data, *, cron=False):
        unknown = set(data) - set(DEFAULT_PREFERENCES)
        if unknown:
            raise fail("preferences", ", ".join("data." + k for k in sorted(unknown)), "is not a preference", expected="only " + ", ".join(DEFAULT_PREFERENCES),
                       fix="delete the unknown fields or correct their names")
        with self.store.transaction() as db:
            prefs = self.preferences()
            changed = any(prefs.get(key) != value for key, value in data.items())
            feed_changed = "feed_brief" in data and prefs["feed_brief"] != data["feed_brief"]
            prefs.update(data)
            # Validate before changing the file.
            for key, high in (("start_hour", 23), ("end_hour", 24), ("ordinary_per_day", 20), ("time_sensitive_per_day", 20)):
                if key.endswith("_per_day") and prefs[key] is None:
                    continue
                if type(prefs[key]) is not int or not 0 <= prefs[key] <= high:
                    raise fail("preferences", "data." + key, "is out of range", expected=f"an integer from 0 to {high}" + (" (or null for no limit)" if key.endswith("_per_day") else ""),
                               got=prefs[key], fix=f"send data.{key} as a whole number within the range")
            if not isinstance(prefs["blocked_topics"], list) or any(not isinstance(x, str) for x in prefs["blocked_topics"]):
                raise fail("preferences", "data.blocked_topics", "must be an array of topic strings", got=prefs["blocked_topics"],
                           fix='send data.blocked_topics as e.g. ["sports", "crypto"]')
            # One destination only: a comma list that includes bot-chat would skip Muse's Bot Chat envelope and receipt tracking.
            if prefs["deliver"] is not None and (not isinstance(prefs["deliver"], str) or not prefs["deliver"].strip() or "," in prefs["deliver"]):
                raise fail("preferences", "data.deliver", "must be one destination string or null", expected='null (automatic), "all", "bot-chat" or one platform such as "telegram"',
                           got=prefs["deliver"], fix='send data.deliver as one of those strings (no comma lists), or null to restore automatic selection')
            if type(prefs["weather_context"]) is not bool:
                raise fail("preferences", "data.weather_context", "must be true or false", expected="true (send the configured location to a third-party weather service) or false (default, no weather lookups)",
                           got=prefs["weather_context"], fix="send data.weather_context as the JSON boolean true or false")
            if not isinstance(prefs["feed_brief"], str) or len(prefs["feed_brief"]) > 12000:
                raise fail("preferences", "data.feed_brief", "must be text of at most 12000 characters", got=prefs["feed_brief"], fix="send a shorter plain-text brief")
            path = self.store.path("PROACTIVE_PREFERENCES.md")
            body = path.read_text(encoding="utf-8").partition("\n")[2]
            self.store.write_text("PROACTIVE_PREFERENCES.md", "<!-- hermes-muse " + json.dumps(prefs, ensure_ascii=False) + " -->\n" + body)
            explicit = self.store.get(db, "meta", "explicit_preferences", {})
            explicit.update({key: self.clock() for key in data})
            self.store.put(db, "meta", "explicit_preferences", explicit)
            if "feed_brief" in data:
                self.store.put(db, "meta", "feed_requested", self.clock())
        if feed_changed and not cron:
            self.host.wake("feed-pulse")
        return {**prefs, "changed": changed}

    def signal(self, turn_id, session_id, text, route=None):
        if is_background_message(text):
            return
        now = self.clock()
        with self.store.transaction() as db:
            if self.store.get(db, "signal", turn_id):
                return
            self.store.put(db, "signal", turn_id, {"id": turn_id, "session_id": session_id,
                           "text": str(text)[:8000], "created": now, "truncated": len(str(text)) > 8000})
            previous = self.store.get(db, "session", session_id, {})
            self.store.put(db, "session", session_id, {**previous, "id": session_id,
                           "last_signal": now, "active": True, "route": route or previous.get("route"), "turn_id": turn_id})
            # Signals are a bounded processing inbox. Durable facts belong in native memory.
            old = sorted(self.store.rows(db, "signal"), key=lambda x: x["created"])
            processed = min(self.store.get(db, "cursor", "memory-upkeep", 0), self.store.get(db, "cursor", "nightly-review", 0))
            for row in old[:-500]:
                if row["created"] <= processed:
                    self.store.delete(db, "signal", row["id"])

    def require_signal(self, db, signal_id):
        row = self.store.get(db, "signal", bounded(signal_id, 160))
        if not row or is_background_message(row.get("text")):
            raise ValueError("An original user signal is required")
        return row

    def goal_create(self, data, *, session_id=""):
        now = self.clock()
        key = identifier(data.get('id') or uuid.uuid4().hex[:12])
        with self.store.transaction() as db:
            signal = self.require_signal(db, data['signal_id']) if data.get('signal_id') else None
            origin = session_id or (signal or {}).get('session_id')
            if not origin or signal and signal['session_id'] != origin:
                raise ValueError('A private parent session is required')
            if self.store.path(f'workspace/goals/{key}/GOAL.md').exists():
                raise ValueError('Goal already exists; use goal_update')
            if self.store.get(db, 'forgotten', 'goal:' + key):
                raise ValueError('Forgotten goal ID cannot be reused')
            parent_id = data.get('parent_id')
            if parent_id:
                parent, _ = self.store.goal(parent_id)
                if parent.get('parent_id') or parent['status'] != 'active':
                    raise ValueError('Subgoals require an active top-level parent')
            from .followup import owner
            deadline = epoch(data['deadline']) if data.get('deadline') else None
            record = {'id': key, 'title': bounded(data.get('title'), 200), 'status': 'active',
                      'revision': 1, 'parent_id': parent_id, 'created': now, 'updated': now,
                      'research_review_at': min(deadline or now + 30 * DAY, now + 30 * DAY),
                      'deadline': deadline, 'external_ref': data.get('external_ref'),
                      'source_signal': (signal or {}).get('id'), 'session_id': origin, 'progress': [],
                      'owner': owner(data.get('owner')), 'next_check_at': now + 1800,
                      'waiting_user': False, 'blocked': None}
            body = f"# {record['title']}\n\n{bounded(data['description'])}\n\n## Completion criteria\n{bounded(data['completion_criteria'])}\n"
            self.store.save_goal(record, body)
            for folder in ('files', 'hidden_files'):
                self.store.path(f'workspace/goals/{key}/{folder}').mkdir(exist_ok=True)
        return record

    INTAKE_CAP = 5

    def intake_goal_create(self, data, run):
        """goal_create for the nightly review: needs an original signal and at most INTAKE_CAP goals per run."""
        if not data.get('signal_id'):
            raise fail('goal_create', 'data.signal_id', 'is required in the nightly review', expected='the ID of the user signal that holds the accepted request, taken from context(section=signals)',
                       fix='resend with data.signal_id set to that signal ID; skip the item when no signal supports it')
        def reserve(step):
            with self.store.transaction() as db:
                row = self.store.get(db, 'run', run['id'])
                stage = row['stages'].setdefault('goal_intake', {'created': 0})
                if step > 0 and stage['created'] >= self.INTAKE_CAP:
                    raise fail('goal_create', 'data', f"is not allowed: this nightly run already created {stage['created']} goals", code='size_limit',
                               expected=f'at most {self.INTAKE_CAP} new goals per nightly run', fix='stop creating goals; list the remaining candidates in the run_complete summary for the next run')
                stage.update(created=stage['created'] + step, at=self.clock())
                self.store.put(db, 'run', row['id'], row)
        reserve(1)
        try:
            # No session_id: the goal belongs to the conversation of its signal, not to the Cron session.
            return self.goal_create(data)
        except BaseException:
            reserve(-1)
            raise

    def goal_update(self, data):
        from .content_io import DomainError
        from .followup import owner
        now = self.clock()
        closing = []
        with self.store.transaction() as db:
            record, body, fingerprint = self.store.goal(identifier(data['id']), with_fingerprint=True)
            expected = data.get('expected_revision')
            if expected is not None and (type(expected) is not int or expected != record['revision']):
                raise DomainError('revision_conflict', 'Goal changed; merge with current goal', current=record,
                                  current_revision=record['revision'])
            status = data.get('status', record['status'])
            if status not in {'active', 'done', 'cancelled'}:
                raise fail("goal_update", "data.status", "is not an accepted value", expected="one of active, done, cancelled", got=status, fix="use exactly one of the listed values")
            if status == 'done' and record['status'] != 'done' and not data.get('note'):
                raise fail("goal_update", "data.note", "is required when status=done", expected="text describing the result you told the user",
                           fix="tell the user the result first, then resend with data.note (what was delivered) alongside status=done")
            if 'description' in data or 'completion_criteria' in data:
                before, separator, criteria = body.partition('## Completion criteria\n')
                if not separator:
                    raise ValueError('Goal section boundaries are ambiguous; preserve content')
                description = bounded(data['description']) if 'description' in data else before.partition('\n\n')[2].rstrip()
                criteria = bounded(data['completion_criteria']) if 'completion_criteria' in data else criteria
                body = f"# {record['title']}\n\n{description}\n\n## Completion criteria\n{criteria}\n"
            if 'owner' in data:
                record['owner'] = owner(data['owner'])
            if 'waiting_user' in data:
                if type(data['waiting_user']) is not bool:
                    raise fail("goal_update", "data.waiting_user", "must be true or false", got=data['waiting_user'], fix="send a JSON boolean without quotes")
                record['waiting_user'] = data['waiting_user']
            if 'blocked' in data:
                record['blocked'] = bounded(data['blocked'], 2000) if data['blocked'] else None
            if 'next_check_at' in data:
                record['next_check_at'] = epoch(data['next_check_at'])
            if 'research_review_at' in data:
                review = epoch(data['research_review_at'])
                if not now < review <= now + 30 * DAY:
                    raise fail("goal_update", "data.research_review_at", "must be within the next 30 days", got=data['research_review_at'],
                               fix="choose a time between now and 30 days from now (ISO-8601 with offset)")
                record['research_review_at'] = review
            for field in ('note', 'progress'):
                if data.get(field):
                    record['progress'] = (record['progress'] + [{'at': now, 'text': bounded(data[field], 2000)}])[-50:]
            record.update(status=status, updated=now, revision=record['revision'] + 1)
            self.store.save_goal(record, body, expected_hash=fingerprint)
            if status != 'active':
                closing.append(record['id'])
                if status == 'cancelled':
                    for child in self.store.goals():
                        if child.get('parent_id') == record['id'] and child['status'] == 'active':
                            child, child_body, child_hash = self.store.goal(child['id'], with_fingerprint=True)
                            child.update(status='cancelled', updated=now, revision=child['revision'] + 1)
                            self.store.save_goal(child, child_body, expected_hash=child_hash)
                            closing.append(child['id'])
                for idea in self.store.rows(db, 'interest'):
                    if idea.get('goal_id') in closing and idea['status'] in {'candidate', 'active', 'accepted'}:
                        idea['status'] = 'cancelled'
                        self.store.put(db, 'interest', idea['id'], idea)
                for notice in self.store.rows(db, 'notification'):
                    if notice.get('goal_id') in closing and notice['status'] in {'candidate', 'pending', 'queued'} and not notice.get('final_result'):
                        notice['status'] = 'cancelled'
                        self.store.put(db, 'notification', notice['id'], notice)
        failed = []
        for key in closing:
            try:
                self.host.pause_owner(key)
            except Exception:
                failed.append(key)
        if failed:
            return {**record, 'warning': f"Goal committed; watch cleanup failed for {len(failed)} goal(s): {', '.join(failed[:10])}. Inspect owned watches and retry their cleanup; do not repeat goal completion."}
        return record

    def interest_record(self, data, *, idea=False):
        now = self.clock()
        key = identifier(data.get("id") or uuid.uuid4().hex[:12])
        with self.store.transaction() as db:
            signal = self.require_signal(db, data.get("signal_id"))
            previous = self.store.get(db, "interest", key)
            if previous and signal["created"] <= previous["last_signal"]:
                raise ValueError("Only a newer real user signal can renew this interest")
            deleted_at = self.store.get(db, "forgotten", "interest:" + key, 0)
            if signal["created"] <= deleted_at:
                raise ValueError("This interest was forgotten; old evidence cannot recreate it")
            expires = min(epoch(data["expires"]), now + 14 * DAY) if data.get("expires") else signal["created"] + 14 * DAY
            if expires <= now:
                raise ValueError("Interest already expired")
            record = {"id": key, "kind": "idea" if idea else "interest", "title": bounded(data.get("title"), 300),
                      "source_signal": signal["id"], "session_id": signal["session_id"], "last_signal": signal["created"],
                      "expires": expires, "status": "candidate" if idea else "active", "rationale": str(data.get("rationale", ""))[:2000]}
            self.store.put(db, "interest", key, record)
        return record

    def idea_add(self, data):
        # Ideas may come from connected-source context without inventing a user commitment.
        goal = None
        sources = data.get("sources", [])
        if data.get("goal_id"):
            goal, _ = self.store.goal(data["goal_id"])
            if goal["status"] != "active" or goal["research_review_at"] <= self.clock():
                raise ValueError("Goal is inactive or needs review")
        elif not sources:
            raise ValueError("An independent Idea needs original context/evidence references")
        if not isinstance(sources, list) or any(not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in sources):
            raise ValueError("Idea sources must be reference strings")
        title = bounded(data["title"], 300)
        expires = min(epoch(data["expires"]), self.clock() + 14 * DAY) if data.get("expires") else self.clock() + 14 * DAY
        if expires <= self.clock():
            raise ValueError("Idea already expired")
        key = digest((goal["id"] if goal else "context:") + title)
        with self.store.transaction() as db:
            existing = self.store.get(db, "interest", key)
            if existing or self.store.get(db, "forgotten", "interest:" + key):
                return existing or {"id": key, "status": "forgotten"}
            row = {"id": key, "kind": "idea", "title": title, "goal_id": goal["id"] if goal else None,
                   "session_id": goal["session_id"] if goal else None, "sources": sources,
                   "last_signal": 0, "expires": expires, "status": "candidate",
                   "rationale": bounded(data["rationale"], 2000)}
            self.store.put(db, "interest", key, row)
        return row

    def owner_valid(self, row, db=None):
        now = self.clock()
        if row.get("goal_id"):
            goal, _ = self.store.goal(row["goal_id"])
            if goal['status'] == 'cancelled':
                return False
            if goal["status"] != "active" and not row.get("final_result"):
                return False
        if row.get("interest_id"):
            # Caller can be in a transaction; use its snapshot when supplied.
            interest = row.get("_interest")
            if interest is None:
                interest = (self.store.get(db, "interest", row["interest_id"]) if db is not None
                            else self.store.read("interest", row["interest_id"]))
            if (not interest or interest["status"] not in {"active", "accepted"}
                    or interest["expires"] <= now):
                return False
        return row.get("expires", now + 1) > now

    def conversation_states(self, db=None, *, served=False):
        sessions = self.store.rows(db, "session") if db is not None else self.store.all("session")
        reader = getattr(self.host, "conversation", None)
        from .conversation import previews
        states = {s["id"]: self.conversation_state(s["id"], db=db) for s in sessions} if reader else {}
        return previews(self.store, states, self.clock(), db, served=served)

    def conversation_state(self, sid, db=None):
        prior = self.store.get(db, 'conversation_window', digest(sid), {}) if db is not None else self.store.read('conversation_window', digest(sid), {})
        since = prior.get('since', self.clock() - DAY)
        return {**self.host.conversation(sid, since), 'session_id': sid, 'since': since}

    def conversation_read(self, data):
        from .conversation import page, descriptor
        sid = bounded(data.get("session_id"), 200)
        if not self.store.read("session", sid):
            raise ValueError("Conversation must belong to a tracked private source session")
        state = self.conversation_state(sid)
        if data.get('descriptor') or not data.get('revision'):
            return descriptor(self.store, state, self.clock())
        return page(self.store, state, data, self.clock())

    @staticmethod
    def conversation_revisions(states):
        return {sid: value["revision"] for sid, value in states.items() if value.get("available") and (value.get("total_messages") or value.get("total_text_chars") or value.get("messages"))}

    @staticmethod
    def conversation_heads(states):
        return {sid: value["head"] for sid, value in states.items() if value.get("available") and value.get("head")}

    @staticmethod
    def unread_pages(states):
        """Exact reads that serve the newest unreviewed text; nothing older is required."""
        calls = [{"action": "conversation_read", "data": {"session_id": sid, "revision": v["revision"], "offset": v["next_offset"]}}
                 for sid, v in sorted(states.items()) if v.get("available") and not v.get("complete") and v.get("next_offset") is not None]
        # Bounded response; the next review or preparation lists the rest.
        return {"read": calls[:10], "unread_sessions": len(calls)} if calls else {}

    def released(self, row):
        hold = row.get("conversation_hold_since")
        return bool(hold) and self.clock() - hold >= HOLD_RELEASE and (row.get("conversation_review") or {}).get("verdict") == "relevant"

    def conversation_block(self, row, states):
        if row.get("session_id") and not states.get(row["session_id"], {}).get("available"):
            return "conversation_unavailable"
        current = self.conversation_heads(states)
        review = row.get("conversation_review") or {}
        seen = review.get("heads")
        if seen is None:
            return "conversation_changed_or_unreviewed" if current or review else None
        # Only messages newer than the reviewed head matter; unread older history does not block.
        if any(seen.get(sid) != value for sid, value in current.items()) and not self.released(row):
            return "conversation_changed_or_unreviewed"
        return None

    def hold(self, row, reasons):
        if {"conversation_changed_or_unreviewed", "active_conversation"} & set(reasons) and row.get("conversation_review"):
            row.setdefault("conversation_hold_since", self.clock())

    def notification_review(self, data):
        """The model judges relevance; code checks that the newest conversation text was served."""
        key = identifier(data["id"])
        candidate = self.store.read("notification", key, {})
        states = self.conversation_states()
        if candidate.get("session_id") and not states.get(candidate["session_id"], {}).get("available"):
            raise ValueError("The linked conversation is unavailable; recover its history before reviewing this task notice")
        revisions = self.conversation_revisions(states)
        unread = self.unread_pages(states)
        if unread:
            # Only evidence actually returned to the caller counts as served.
            return {"reviewed": False, "reason": "The newest conversation text has not been read yet. Make exactly these conversation_read calls (follow next_offset until complete), then review again; older history is not required.",
                    **unread}
        verdict = data.get("verdict")
        if verdict not in {"relevant", "resolved", "superseded"}:
            raise fail("notification_review", "data.verdict", "is not an accepted value", expected="one of relevant, resolved, superseded", got=verdict,
                       fix="use exactly one of the listed values and add data.reason")
        reason = bounded(data.get("reason"), 2000)
        with self.store.transaction() as db:
            row = self.store.get(db, "notification", key)
            if not row or row["status"] not in {"candidate", "pending"}:
                raise ValueError("Review only unsent candidates; delivered/unknown history is immutable")
            row["conversation_review"] = {"revisions": revisions, "heads": self.conversation_heads(states),
                                          "at": self.clock(), "verdict": verdict, "reason": reason}
            if verdict != "relevant":
                row.update(status="cancelled", resolution={"at": self.clock(), "reason": reason, "basis": "native_conversation"})
                row.pop("delivery_gate", None)
            self.store.put(db, "notification", key, row)
        return {"reviewed": True, "notification": row}

    def source_snapshot(self, data, db=None):
        if not isinstance(data, dict) or set(data) - {"item_id", "title", "due", "timezone", "checked_at"}:
            raise fail("notification_add", "data.source_snapshot", "needs the original upstream item_id, title and checked_at (not a source_check record ID); optional due/timezone",
                       expected="{item_id, title, checked_at, due?, timezone?}", got=data, fix="send exactly those keys; remove any others")
        result = {k: bounded(data.get(k), 2000) for k in ("item_id", "title")}
        collection = self.store.get(db, "source_check", result["item_id"]) if db is not None else self.store.read("source_check", result["item_id"])
        if collection:
            # Source-check IDs are plugin collection identities, not upstream item IDs.
            raise ValueError("Use the original reminder/event item ID, not a source_check collection ID")
        result["title"] = data["title"]  # Preserve case and the exact provider title.
        for field in ("due", "timezone"):
            if field in data:
                result[field] = bounded(data[field], 200)
        result["checked_at"] = epoch(data["checked_at"])
        if not self.clock() - DAY <= result["checked_at"] <= self.clock() + 60:
            raise ValueError("Source snapshot verification must be recent")
        return result

    @staticmethod
    def signal_batch(signals):
        # Include timestamp ties so advancing a timestamp cursor cannot lose a sibling.
        return [r for r in signals if r["created"] <= signals[24]["created"]] if len(signals) > 25 else signals

    def context(self, view=None):
        now = self.clock()
        goals = self.store.goals()
        interests = [r for r in self.store.all("interest") if r["expires"] > now and r.get("not_before", 0) <= now and r["status"] in {"active", "candidate", "accepted"}]
        signals = sorted((r for r in self.store.all("signal") if not is_background_message(r.get("text"))), key=lambda r: r["created"])
        cursor = self.store.read("cursor", "memory-upkeep", 0)
        nightly_cursor = self.store.read("cursor", "nightly-review", 0)
        notices = self.store.all("notification")
        conversations = self.conversation_states(served=True) if view != "feed-pulse" else {}
        with self.store.transaction() as db:
            for row in notices:
                if row["status"] in {"candidate", "pending"} and row["expires"] > now:
                    row["delivery_gate"] = self.delivery_gate(row, db, now, self.preferences())
                    blocker = self.conversation_block(row, conversations) if view != "feed-pulse" else None
                    if blocker:
                        row["delivery_gate"]["blocked_by"].append(blocker)
        live = [r for r in notices if r["status"] in {"candidate", "pending", "queued", "dispatching", "transport_queued", "unknown", "failed"}
                and (r["expires"] > now or r["status"] not in {"candidate", "pending"})]
        history = sorted((r for r in notices if r not in live), key=lambda r: r["created"], reverse=True)[:30]
        return {"now": stamp(now), "calendar_window": self.calendar_window({}), "recent_conversations": conversations,
                "conversation_revisions": self.conversation_revisions(conversations), "content_access": "muse_manage content_read by object kind/id", "preferences": self.preferences(),
                "goals": [g for g in goals if g["status"] == "active" and g["research_review_at"] > now],
                "goals_needing_review": [g for g in goals if g["status"] == "active" and g["research_review_at"] <= now],
                "interests_and_ideas": interests,
                "new_user_signals": self.signal_batch([r for r in signals if r["created"] > cursor]) if view in {None, "memory-upkeep"} else [],
                "nightly_user_signals": self.signal_batch([r for r in signals if r["created"] > nightly_cursor]) if view in {None, "nightly-review"} else [],
                "review_backlog": {job: {"remaining": sum(r["created"] > value for r in signals), "through": stamp(now)}
                                   for job, value in (("memory-upkeep", cursor), ("nightly-review", nightly_cursor))},
                "recent_user_context": [r for r in signals if r["created"] > now - DAY]
                                       if view == "proactive-watch" or str(view).startswith("watch-") else [],
                "review_cursors": {job: self.store.read("cursor", job, 0) for job in ("memory-upkeep", "nightly-review", "proactive-watch", "feed-pulse")},
                "source_checks": self.store.all("source_check"),
                "legacy_source_checks": len(self.store.all("source_check_legacy")),
                "research": [{"id": b["id"], "goal_id": b.get("goal_id"), "mode": b.get("mode", "legacy"),
                              "created": b["created"]} for b in sorted(self.store.all("research"), key=lambda x: x["created"], reverse=True)[:20]],
                "operation_review": self.operations.summary() if view != "feed-pulse" else None,
                "task_runs": self.runs.recent(),
                "notifications": live,
                "notification_history": history,
                "feed": self.feed_list({"limit": 15})["items"]}

    def review_complete(self, data, *, require_window=False):
        job = data.get("job")
        if job not in {"memory-upkeep", "nightly-review", "feed-pulse", "proactive-watch", *GOVERNANCE_JOBS}:
            raise ValueError("Unknown review job")
        if 'window' in data and job not in {'memory-upkeep', 'nightly-review'}:
            raise ValueError('window is only valid for memory-upkeep/nightly-review')
        up_to = epoch(data["up_to"])
        now = self.clock()
        through = epoch(data.get("through", now))
        if up_to > through or through > now:
            raise ValueError("Review cursor must not exceed its snapshot or the current time")
        with self.store.transaction() as db:
            old = self.store.get(db, "cursor", job, 0)
            if require_window and job in {"memory-upkeep", "nightly-review"}:
                from .context_pages import check_review
                check_review(self, data, db, old, up_to, through)
            if job in {"memory-upkeep", "nightly-review"}:
                pending = sorted((s for s in self.store.rows(db, "signal") if s["created"] > old and not is_background_message(s.get("text"))), key=lambda s: s["created"])
                if not require_window and len(pending) > 25 and up_to > self.signal_batch(pending)[-1]["created"]:
                    raise ValueError("Cursor skips unseen signals; process the returned batch first")
            remaining = [r for r in pending if max(old, up_to) < r["created"] <= through] if job in {"memory-upkeep", "nightly-review"} else []
            self.store.put(db, "cursor", job, max(old, up_to))
            self.store.put(db, "meta", "last_review:" + job, {"at": now, "summary": bounded(data["summary"], 2000),
                                                           "complete": not remaining, "scope": "checkpoint", "job_complete": False, "remaining": len(remaining)})
        return {"job": job, "processed_through": stamp(max(old, up_to)), "through": stamp(through),
                "complete": not remaining, "scope": "checkpoint", "job_complete": False, "remaining": len(remaining), **({"next_user_signals": self.signal_batch(remaining)} if not require_window else {}),
                "continuation": {"action": "context", "data": {"section": "signals", "job": job, "window": data.get("window")}} if require_window else None,
                "instruction": "Continue the signal window (legacy internal callers: next_user_signals) and call review_complete with the same through; do not finish this run yet." if remaining else "This checkpoint is complete, not the whole task. Finish required artifacts/operation review, then call run_complete in the owned Cron."}

    def calendar_window(self, data):
        return window(self.host.local_now(self.clock()), data.get("mode", "rolling"))

    def calendar_view(self, data):
        from .content_io import DomainError
        from .views import Views
        view, views = assemble(data, self.host.local_now(self.clock())), Views(self.store.home)
        for item in view["coverage"]:
            # Proposed checks carry the current CAS revision so they can be submitted as-is plus a request_id.
            with self.store.transaction() as db:
                key = self.source_identity(item["source_check"], db)[0]
            try:
                item["source_check"]["expected_revision"] = views.object("source_check", key)[2]
            except DomainError as exc:
                if exc.code != "not_found":
                    raise
                item["source_check"]["expected_revision"] = "absent"
        return view

    def source_identity(self, data, db):
        """Resolve the actual write target once for both semantic CAS and mutation."""
        source = data.get("source")
        fields = {"connection", "account", "resource"}
        if not isinstance(source, dict) or set(source) != fields:
            raise fail("source_check", "data.source", "needs exactly connection, account and resource", expected="stable tool/account/resource IDs, no scan dates",
                       got=source, fix='send data.source = {"connection": ..., "account": ..., "resource": ...}')
        source = {k: bounded(source[k], 500) for k in sorted(fields)}
        key = "source-" + digest(json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        alias = self.store.get(db, "source_alias", key)
        target, seen = data.get("source_id") or key, set()
        while target not in seen:
            seen.add(target)
            binding = self.store.get(db, "source_alias", identifier(target))
            if not binding:
                break
            if any(binding.get("source", {}).get(f) != source[f] for f in ("account", "resource")):
                raise fail("source_check", "data.source_id", "points at a record with a different account/resource", got=target,
                           fix="data.source_id must name a source_check record for the same account and resource as data.source; delete data.source_id otherwise")
            target = binding["target"]
        else:
            raise fail("source_check", "data.source_id", "creates an alias cycle", got=data.get("source_id"),
                       fix="read context(section=source_check) and point data.source_id at the canonical record of this source, or delete it")
        if target == key:
            return key, source, None
        canonical = self.store.get(db, "source_check", identifier(target))
        if not canonical or any(canonical["source"][f] != source[f] for f in ("account", "resource")):
            raise fail("source_check", "data.source_id", "must identify the same account and resource as data.source; never merge different accounts/collections",
                       got=target, fix="use a source_check record id from context(section=source_check) whose source has this account and resource, or delete data.source_id")
        reason = (alias or {}).get("reason") or bounded(data.get("alias_reason"), 1000)
        return target, canonical["source"], (key, {"target": target, "source": source, "reason": reason})

    def source_check(self, data, run=None):
        """Stable source identity and coverage, using the same target as tool CAS.

        A missing started_at defaults to the bound Cron run's start, else to this call's time; an explicit value always wins.
        """
        with self.store.transaction() as db:
            key, source, binding = self.source_identity(data, db)
            now = self.clock()
            given = data.get("started_at")
            started = epoch(given) if given is not None else min(run["started"], now) if run else now
            if not 0 < started <= now:
                raise fail("source_check", "data.started_at", "must be a past or current timestamp", got=given,
                           fix="set data.started_at to when this check began (epoch seconds or ISO-8601 with offset), not a future time")
            status = data["status"]
            if status not in {"checked", "partial", "error", "unavailable"}:
                raise fail("source_check", "data.status", "is not an accepted value", expected="one of checked, partial, error, unavailable", got=status,
                           fix="use exactly one of the listed values")
            scope = bounded(data["scope"], 1000)
            summary = bounded(data["summary"], 2000)
            coverage = {}
            for field in ("expected_resources", "checked_resources"):
                values = data.get(field, [])
                if not isinstance(values, list) or len(values) > 500:
                    raise fail("source_check", "data." + field, "must be an array of at most 500 resource IDs", got=values,
                               fix=f"send data.{field} as a JSON array of resource id strings, e.g. [\"mail:inbox\"]")
                coverage[field] = sorted(set(bounded(v, 1000) for v in values))
            expected, checked = coverage["expected_resources"], coverage["checked_resources"]
            if not set(checked).issubset(expected):
                raise fail("source_check", "data.checked_resources", "contains IDs that are not in data.expected_resources", got=sorted(set(checked) - set(expected)),
                           fix="list every checked ID in data.expected_resources too (expected is the full set in scope, checked the subset actually read)")
            if status == "checked" and (not expected or checked != expected):
                status = "partial"  # A listed calendar is not a queried calendar.
            cursor = data.get("cursor")
            if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4000):
                raise fail("source_check", "data.cursor", "must be text of at most 4000 characters", got=cursor,
                           fix="send the opaque cursor as plain text, or delete data.cursor")
            observation = {}
            if "query_window" in data:
                validate_window(data["query_window"], "data.query_window", "source_check")
                if source["resource"] != data["query_window"]["source_resource"]:
                    raise fail("source_check", "data.source.resource", "must match data.query_window.source_resource (Calendar query_window must match its separate source resource)",
                               expected=data["query_window"]["source_resource"], got=source["resource"],
                               fix="use the resource named by the calendar_window result for this window, or drop query_window for a non-calendar source")
                observed = epoch(data["observed_at"])
                if started != epoch(data["query_window"]["observed_at"]) or not started <= observed <= now:
                    raise fail("source_check", "data.started_at / data.observed_at", "do not match the calendar query",
                               expected="started_at equal to query_window.observed_at, and observed_at between it and now", got=given,
                               fix="delete data.started_at and data.observed_at (Muse fills them from query_window), or copy them unchanged from the calendar_view coverage[].source_check")
                observation = {"query_window": canonical_window(data["query_window"]), "observed_at": observed}
            if binding:
                alias_key, alias = binding
                self.store.put(db, "source_alias", alias_key, alias)
                duplicate = self.store.get(db, "source_check", alias_key)
                if duplicate:
                    self.store.put(db, "source_check_alias_history", alias_key, duplicate)
                    self.store.delete(db, "source_check", alias_key)
            old = self.store.get(db, "source_check", key, {})
            if (started < old.get("started_at", 0) or
                    observation and started == old.get("started_at") and observation["observed_at"] < old.get("observed_at", 0)):
                return self.defaulted(old, given, run)
            row = {**old, "id": key, "source": source, "scope": scope, "started_at": started,
                   "finished_at": now, "status": status, "summary": summary, **coverage, **observation}
            row["missing_resources"] = sorted(set(expected) - set(checked))
            if status == "checked":
                row.update(last_success=started, cursor=cursor, last_success_scope=scope,
                           last_success_resources=checked)
                if observation:
                    row.update(last_success_window=observation["query_window"], last_success_observed_at=observation["observed_at"])
            self.store.put(db, "source_check", key, row)
        return self.defaulted(row, given, run)

    @staticmethod
    def defaulted(row, given, run):
        """Tell the caller (not the stored row) that started_at was filled in."""
        if given is not None:
            return row
        used = "the start time of this Cron run" if run else "the time of this call"
        return {**row, "started_at_defaulted": f"data.started_at was not sent, so Muse used {used}; send data.started_at only if the check began at a different time"}

    def notification_resolve(self, data):
        """Retire an unsent notice after verifying that its source no longer warrants it."""
        key = identifier(data["id"])
        verified = epoch(data["verified_at"])
        # Same 60-second clock skew as notification_add/refresh.
        if not self.clock() - DAY <= verified <= self.clock() + 60:
            raise ValueError("Resolution needs recent source verification: verified_at within the last 24 hours (epoch or ISO with timezone)")
        sources = data.get("sources")
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or len(s) > 2000 for s in sources):
            raise ValueError("Supply original resolution evidence")
        reason = bounded(data["reason"], 2000)
        with self.store.transaction() as db:
            row = self.store.get(db, "notification", key)
            if not row or row["status"] not in {"candidate", "pending"}:
                raise ValueError("Only an unsent candidate can be resolved from source evidence")
            row.update(status="cancelled", resolution={"verified_at": verified, "sources": sources, "reason": reason})
            self.store.put(db, "notification", key, row)
        return row

    def notification_add(self, data):
        now = self.clock()
        event_key = bounded(data.get("event_key"), 500)
        key = digest(event_key)
        if data.get("priority", "ordinary") not in {"ordinary", "time_sensitive", "urgent", "promised"}:
            raise fail("notification_add", "data.priority", "is not an accepted value", expected="one of ordinary, time_sensitive, urgent, promised",
                       got=data.get("priority"), fix="use exactly one of the listed values or omit it")
        sources = data.get("sources")
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or len(s) > 2000 for s in sources):
            raise ValueError("Supply original evidence source references")
        expires = epoch(data["expires"])
        verified_at = epoch(data["verified_at"])
        if not now < expires or not now - DAY <= verified_at <= now + 60:
            raise ValueError("Evidence verification must be recent and expiry in the future")
        row = {"id": key, "event_key": event_key, "message": bounded(data.get("message"), 6000),
               "rationale": bounded(data.get("rationale"), 2000), "sources": sources,
               "verified_at": verified_at, "expires": expires, "created": now,
               "priority": data.get("priority", "ordinary"), "topic": str(data.get("topic", ""))[:100],
               "goal_id": data.get("goal_id"), "interest_id": data.get("interest_id"),
               "final_result": bool(data.get("final_result")), "status": "candidate", "not_before": now,
               "session_id": data.get("session_id"), "job_id": None,
               "issue_key": bounded(data.get("issue_key") or ("signal:" + data["signal_id"] if data.get("signal_id") else event_key), 500)}
        if data.get("source_snapshot"):
            row["source_snapshot"] = self.source_snapshot(data["source_snapshot"])
            if row["source_snapshot"]["title"] not in row["message"]:
                raise ValueError("Quote the exact original title in the notice; do not expand or rename it")
        if not self.owner_valid(row):
            raise ValueError("Notification owner is inactive or expired")
        with self.store.transaction() as db:
            if row["priority"] == "promised":
                owned_promise = data.get("watch_id") and self.host.is_active_watch(data["watch_id"], row.get("goal_id"))
                if not owned_promise:
                    signal = self.require_signal(db, data.get("signal_id"))
                    row.update(session_id=signal["session_id"], source_signal=signal["id"])
            if self.store.get(db, "forgotten", "notification:" + key):
                return {"id": key, "status": "forgotten"}
            existing = self.store.get(db, "notification", key)
            if existing:
                return existing
            supersedes = data.get("supersedes", [])
            if not isinstance(supersedes, list):
                raise ValueError("supersedes must list older candidate IDs for the same issue")
            for old_key in supersedes:
                old = self.store.get(db, "notification", identifier(old_key))
                if not old or old.get("issue_key", old["event_key"]) != row["issue_key"] or old["status"] not in {"candidate", "pending"}:
                    raise ValueError("Supersede only unsent candidates for the same issue; preserve delivered history")
                old.update(status="cancelled", superseded_by=key)
                self.store.put(db, "notification", old_key, old)
            siblings = [n for n in self.store.rows(db, "notification") if n.get("issue_key", n["event_key"]) == row["issue_key"] and n["status"] in {"candidate", "pending"}]
            if siblings:
                raise ValueError("An unsent candidate already tracks this issue; refresh it or explicitly supersede it")
            self.store.put(db, "notification", key, row)
        return row

    def notification_refresh(self, data):
        key = identifier(data["id"])
        verified = epoch(data["verified_at"])
        if not self.clock() - DAY <= verified <= self.clock() + 60:
            raise ValueError("Verification time must be recent")
        sources = data.get("sources")
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or len(s) > 2000 for s in sources):
            raise ValueError("Supply rechecked original source references")
        with self.store.transaction() as db:
            row = self.store.get(db, "notification", key)
            if not row or row["status"] not in {"candidate", "pending"}:
                raise ValueError("Only a pending, not-yet-dispatched candidate can be reverified")
            if "priority" in data and data["priority"] != row["priority"]:
                priority = data["priority"]
                if priority not in {"ordinary", "time_sensitive", "urgent"} or row["priority"] == "promised":
                    raise ValueError("Reassessment cannot create or remove a user promise")
                reason = bounded(data.get("priority_reason"), 2000)
                row["priority_reviews"] = row.get("priority_reviews", []) + [{"from": row["priority"], "to": priority,
                    "reason": reason, "verified_at": verified, "sources": sources}]
                row["priority"] = priority
            row.update(verified_at=verified, sources=sources)
            if "message" in data:
                row["message"] = bounded(data["message"], 6000)
            if data.get("source_snapshot"):
                snapshot = self.source_snapshot(data["source_snapshot"], db=db)
                previous = row.get("source_snapshot")
                if previous and previous.get("item_id", previous.get("source_id")) != snapshot["item_id"]:
                    raise ValueError("A notice cannot change its original source item")
                row["source_snapshot"] = snapshot
            if row.get("source_snapshot") and row["source_snapshot"]["title"] not in row["message"]:
                raise ValueError("Notice text must preserve the exact original source title")
            row.pop("conversation_review", None)
            self.store.put(db, "notification", key, row)
        return row

    def notification_queue(self, data, delivery=None):
        """Prepare in the current patrol/watch, or leave a candidate for the next patrol."""
        key = identifier(data["id"])
        with self.store.transaction() as db:
            row = self.store.get(db, "notification", key)
            if not row:
                raise ValueError(f"Unknown notification id {key!r}; read context(section=notice) or content_read(kind=notice) for valid ids")
            if row["status"] in {"candidate", "pending"}:
                row.update(status="pending", job_id=None)
                self.store.put(db, "notification", key, row)
        if delivery:
            self.delivery_text(key, delivery=delivery)
        result = dict(self.store.read("notification", key))
        result["final_response"] = self.prepared_response(delivery) if delivery else "[SILENT]"
        if result.get("delivery_gate", {}).get("blocked_by", []) in (["conversation_unavailable"], ["conversation_changed_or_unreviewed"]):
            # Descriptors do not serve text. Only bounded conversation_read pages
            # may advance coverage on the public preparation path.
            return {"id": key, "status": result["status"], "final_response": result["final_response"],
                    "delivery_gate": result["delivery_gate"],
                    **self.unread_pages(self.conversation_states()),
                    "instruction": "The conversation has new text since this notice's review. Make the listed conversation_read calls (only the new text), check whether it already covers this matter, call notification_review, then prepare again."}
        result["instruction"] = ("Return final_response as this Cron's final answer. No extra send or Cron." if delivery
                                 else "Saved for the next patrol; no delivery task was created.")
        return result

    def finalize_delivery(self, delivery):
        """Native output hook: emit verified notice bodies, never a model's status summary."""
        now = self.clock()
        prefs = self.preferences()
        states = self.conversation_states()
        with self.store.transaction() as db:
            for row in self.store.rows(db, "notification"):
                if (not row.get("direct_delivery") or row.get("execution_id") != delivery["execution_id"]
                        or row.get("output_finalized")):
                    continue
                if row["status"] != "dispatching":
                    pass
                elif self.conversation_block(row, states):
                    row.update(status="pending", job_id=None, delivery_gate={"blocked_by": [self.conversation_block(row, states)], "next_eligible_at": None})
                    self.hold(row, row["delivery_gate"]["blocked_by"])
                elif not self.owner_valid(row, db):
                    row["status"] = "expired"
                elif row["topic"] in prefs["blocked_topics"]:
                    row["status"] = "dismissed"
                else:
                    if row.get("interest_id"):
                        interest = self.store.get(db, "interest", row["interest_id"], {})
                        row["not_before"] = max(row["not_before"], interest.get("not_before", 0))
                    row["delivery_gate"] = self.delivery_gate(row, db, now, prefs, check_budget=False)
                    if row["delivery_gate"]["blocked_by"]:
                        row.update(status="pending", job_id=None)
                        self.hold(row, row["delivery_gate"]["blocked_by"])
                    elif row["verified_at"] < now - DAY:
                        row.update(status="candidate", job_id=None, needs_reverification=True)
                    else:
                        row["output_finalized"] = True
                if row["status"] != "dispatching" and row.get("budget_key"):
                    key = row.pop("budget_key")
                    self.store.put(db, "budget", key, max(0, self.store.get(db, "budget", key, 0) - 1))
                self.store.put(db, "notification", row["id"], row)
        return self.prepared_response(delivery)

    def prepared_response(self, delivery):
        rows = [row for row in self.store.all("notification")
                if row.get("direct_delivery") and row.get("execution_id") == delivery["execution_id"]
                and row["status"] == "dispatching"]
        if not rows:
            return "[SILENT]"
        return self._render_prepared_response(rows, delivery)

    @staticmethod
    def _render_prepared_response(rows, delivery):
        destination = (delivery.get("route") or {}).get("deliver")
        parts = []
        for row in rows:
            snapshot = "Source evidence (original title and time): " + json.dumps(row["source_snapshot"], ensure_ascii=False) + "\n" if row.get("source_snapshot") else ""
            parts.append("[Hermes Muse notification " + row["id"] + "]\n" + snapshot +
                         "Why: " + row["rationale"] + "\nSources: " + ", ".join(row["sources"]) + "\n\n" + row["message"])
        return render_result("\n\n".join(parts), destination)

    def delivery_gate(self, row, db, now, prefs, *, check_budget=True):
        """Explain policy eligibility, not a promise of scheduled or successful delivery."""
        reasons, eligible = [], max(now, row["not_before"])
        if row.get("interest_id"):
            interest = self.store.get(db, "interest", row["interest_id"], {})
            eligible = max(eligible, interest.get("not_before", 0))
        if eligible > now:
            reasons.append("snoozed")
        limited = row["priority"] in {"ordinary", "time_sensitive"}
        if row["priority"] == "ordinary" and not self.released(row):
            active_until = max((s.get("last_signal", 0) + 900 for s in self.store.rows(db, "session") if s.get("active")), default=0)
            if active_until > now:
                reasons.append("active_conversation")
                eligible = max(eligible, active_until)
        if limited:
            local = self.host.local_now(now)
            if check_budget:
                limit = prefs[row["priority"] + "_per_day"]
                if limit is not None and self.store.get(db, "budget", local.date().isoformat() + ":" + row["priority"], 0) >= limit:
                    reasons.append("daily_limit")
                    if limit == 0:
                        return {"blocked_by": reasons, "next_eligible_at": None, "expires_before_eligible": True,
                                "note": "This priority is disabled by the current daily allowance."}
                    eligible = max(eligible, (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            start, end = prefs["start_hour"], prefs["end_hour"]
            def next_open(ts):
                at = self.host.local_now(ts)
                waking = start <= at.hour < end if start < end else at.hour >= start or at.hour < end
                if waking:
                    return ts
                opening = at.replace(hour=start, minute=0, second=0, microsecond=0)
                if opening.timestamp() <= ts:
                    opening += timedelta(days=1)
                return opening.timestamp()
            if next_open(now) > now:
                reasons.append("quiet_hours")
            eligible = next_open(eligible)
        return {"blocked_by": reasons, "next_eligible_at": stamp(eligible),
                "expires_before_eligible": eligible >= row["expires"],
                "note": "Earliest policy eligibility only; the next patrol must reverify and deliver. No extra Cron is created."}

    def delivery_text(self, key, delivery=None):
        now = self.clock()
        with self.store.transaction() as db:
            row = self.store.get(db, "notification", key)
            allowed_states = {"candidate", "pending"} if delivery else {"queued"}
            if not row or row["status"] not in allowed_states:
                return ""
            if row.get("interest_id"):
                row["_interest"] = self.store.get(db, "interest", row["interest_id"], {})
            if not self.owner_valid(row):
                row.pop("_interest", None)
                row["status"] = "expired"
                self.store.put(db, "notification", key, row)
                return ""
            if row.get("_interest"):
                row["not_before"] = max(row["not_before"], row["_interest"].get("not_before", 0))
            row.pop("_interest", None)
            if row["verified_at"] < now - DAY:
                row.update(status="candidate", job_id=None, needs_reverification=True)
                self.store.put(db, "notification", key, row)
                return ""
            prefs = self.preferences()
            if row["topic"] in prefs["blocked_topics"]:
                row["status"] = "dismissed"
                self.store.put(db, "notification", key, row)
                return ""
            blocker = self.conversation_block(row, self.conversation_states(db))
            if blocker:
                row.update(status="pending", job_id=None, delivery_gate={"blocked_by": [blocker], "next_eligible_at": None})
                self.hold(row, [blocker])
                self.store.put(db, "notification", key, row)
                return ""
            row["delivery_gate"] = self.delivery_gate(row, db, now, prefs)
            if row["delivery_gate"]["blocked_by"]:
                row.update(status="pending", job_id=None)
                self.hold(row, row["delivery_gate"]["blocked_by"])
                self.store.put(db, "notification", key, row)
                return ""
            if delivery:
                batch = [r for r in self.store.rows(db, "notification")
                         if r.get("direct_delivery") and r.get("execution_id") == delivery["execution_id"]
                         and r["status"] == "dispatching" and r["id"] != key]
                text = self._render_prepared_response([*batch, row], delivery)
                # Leave room for the bounded reference receipt around final_response.
                if len(json.dumps(text, ensure_ascii=False).encode()) > 256 * 1024 - 4096:
                    from .content_io import DomainError
                    raise DomainError("size_limit", "Prepared delivery exceeds the tool response budget; no delivery or budget was reserved for this item. Keep it pending and narrow the verified payload or use the next existing patrol.")
            if row["priority"] in {"ordinary", "time_sensitive"}:
                budget_key = self.host.local_now(now).date().isoformat() + ":" + row["priority"]
                self.store.put(db, "budget", budget_key, self.store.get(db, "budget", budget_key, 0) + 1)
                row["budget_key"] = budget_key
            row.update(status="dispatching", dispatched_at=now)
            if delivery:
                row.update(delivery, direct_delivery=True, output_finalized=False)
            self.store.put(db, "notification", key, row)
            return row["message"]

    def feedback(self, data):
        kind, key, action = data["kind"], identifier(data["id"]), data["action"]
        if kind not in {"notification", "interest"}:
            raise ValueError("Feedback kind must be notification or interest")
        with self.store.transaction() as db:
            self.require_signal(db, data.get("signal_id"))
            row = self.store.get(db, kind, key)
            if not row:
                raise ValueError(f"Unknown {kind} id {key!r}; read context(section={'notice' if kind == 'notification' else kind}) for valid ids")
            if action == "snooze":
                until = epoch(data["until"])
                if not self.clock() < until < row["expires"]:
                    raise ValueError("Snooze must be before expiry")
                row.update(not_before=until)
                if kind == "notification":
                    row.update(status="pending", job_id=None)
            elif action in {"done", "dismiss", "stop", "accept"}:
                row["status"] = {"done": "done", "dismiss": "dismissed", "stop": "stopped", "accept": "accepted"}[action]
            else:
                raise ValueError("Unknown feedback action")
            row["feedback"] = {"action": action, "at": self.clock(), "signal_id": data["signal_id"]}
            self.store.put(db, kind, key, row)
        if action == "stop":
            prefs = self.preferences()
            topic = row.get("topic") or key
            self.preference_update({"blocked_topics": list(dict.fromkeys(prefs["blocked_topics"] + [topic]))})
        return row

    def feed_add(self, data):
        key = digest(bounded(data["event_key"], 500))
        now = self.clock()
        if data.get("interest_id") and not self.owner_valid({"interest_id": data["interest_id"]}):
            raise ValueError("Feed interest is expired or inactive")
        if data.get("goal_id"):
            goal, _ = self.store.goal(data["goal_id"])
            if goal["status"] != "active" or goal["research_review_at"] <= now:
                raise ValueError("Feed goal is inactive or needs review")
        with self.store.transaction() as db:
            old = self.store.get(db, "feed", key)
            if old or self.store.get(db, "forgotten", "feed:" + key):
                return old or {"id": key, "status": "deleted"}
            sources = data.get("sources")
            if not isinstance(sources, list) or not sources or any(not isinstance(x, str) for x in sources):
                raise ValueError("Feed needs original source references")
            relative = f"workspace/your_files/feed/{key}.md"
            title = bounded(data["title"], 300)
            self.store.write_text(relative, f"# {title}\n\n{bounded(data['content'], 60000)}\n\n## Sources\n" + "\n".join(f"- {s}" for s in sources) + "\n")
            row = {"id": key, "title": title, "path": relative, "created": now, "position": now,
                   "sources": sources, "why": bounded(data["why"], 2000), "feedback": None,
                   "goal_id": data.get("goal_id"), "interest_id": data.get("interest_id")}
            self.store.put(db, "feed", key, row)
        return row

    def feed_list(self, data):
        from .views import Views
        return Views(self.store.home).read({**data, "kind": "feed"})

    def feed_update(self, data):
        key = identifier(data["id"])
        with self.store.transaction() as db:
            signal = self.require_signal(db, data.get("signal_id"))
            row = self.store.get(db, "feed", key)
            if not row:
                raise ValueError(f"Unknown Feed article id {key!r}; call feed_list for valid ids")
            if data.get("delete"):
                self.store.path(row["path"]).unlink(missing_ok=True)
                self.store.delete(db, "feed", key)
                self.store.put(db, "forgotten", "feed:" + key, self.clock())
                return {"id": key, "deleted": True}
            if any(k in data for k in ("title", "content", "sources")):
                sources = data.get("sources", row["sources"])
                if not isinstance(sources, list) or not sources or any(not isinstance(x, str) or not x.strip() for x in sources):
                    raise ValueError("Feed revisions need source references")
                from .content_io import Files, revision
                files = Files(self.store.home)
                raw = files.read(row["path"])
                title = bounded(data.get("title", row["title"]), 300)
                content = data.get("content")
                if content is None:
                    content = raw.decode().partition("\n\n")[2].rpartition("\n\n## Sources\n")[0]
                content = bounded(content, 60000)
                files.replace(row["path"], (f"# {title}\n\n{content}\n\n## Sources\n" + "\n".join("- " + x for x in sources) + "\n").encode(), revision(raw))
                row.update(title=title, sources=sources)
            if "feedback" in data:
                row["feedback"] = {"text": bounded(data["feedback"], 2000), "signal_id": signal["id"], "at": self.clock()}
            if "position" in data:
                row["position"] = float(data["position"])
            self.store.put(db, "feed", key, row)
        return row

    def forget(self, data):
        kind, key = data["kind"], identifier(data["id"])
        if kind not in {"interest", "notification", "feed", "goal", "operation_finding"}:
            raise ValueError("Unsupported owned record kind")
        with self.store.transaction() as db:
            self.require_signal(db, data.get("signal_id"))
        goal_source = None
        if kind == "goal":
            goal_record = self.store.goal(key)[0]
            goal_source = goal_record.get("source_signal")
            self.goal_update({"id": key, "status": "cancelled", "signal_id": data["signal_id"]})
        with self.store.transaction() as db:
            row = self.store.get(db, kind, key)
            for item in self.store.rows(db, "notification"):
                if (kind == "goal" and item.get("goal_id") == key) or (kind == "interest" and item.get("interest_id") == key):
                    self.store.delete(db, "notification", item["id"])
                    self.store.put(db, "forgotten", "notification:" + item["id"], self.clock())
            for item in self.store.rows(db, "feed"):
                if (kind == "goal" and item.get("goal_id") == key) or (kind == "interest" and item.get("interest_id") == key) or (kind == "feed" and item["id"] == key):
                    self.store.path(item["path"]).unlink(missing_ok=True)
                    self.store.delete(db, "feed", item["id"])
                    self.store.put(db, "forgotten", "feed:" + item["id"], self.clock())
            if kind == "goal":
                if goal_source:
                    self.store.delete(db, "signal", goal_source)
                self.store.put(db, "goal_tombstone", key, {"id": key, "session_id": goal_record.get("session_id"), "status": "cancelled", "forgotten_at": self.clock()})
                # Only managed goal documents; user artifacts are enumerated for explicit cleanup.
                self.store.path(f"workspace/goals/{key}/GOAL.md").unlink(missing_ok=True)
            if row and row.get("source_signal"):
                self.store.delete(db, "signal", row["source_signal"])
            self.store.delete(db, kind, key)
            self.store.put(db, "forgotten", kind + ":" + key, self.clock())
        return {"owned_record_removed": True, "remaining_review": ["native/external memory", "dated notes and alignment", "goal files and evidence", "native session history and backups"],
                "instruction": "Use the forget procedure to inspect and clean these remaining copies; do not claim complete erasure yet."}
