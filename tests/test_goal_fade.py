"""0.3.14 goal fade: derived dormancy (7 days idle), nightly cancel 7 days later, exemptions, preferences, exclusions."""
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_muse import wake_gate
from hermes_muse.content_io import DomainError
from hermes_muse.contracts import FIELDS
from hermes_muse.fade import Fade, ask_key
from hermes_muse.followup import context as followup_context, snapshot
from hermes_muse.service import Companion
from hermes_muse.views import Views
from test_companion import Host, prepare

DAY = 86400
ROOT = Path(__file__).resolve().parents[1]
LEAK = re.compile(r"Traceback|KeyError|TypeError|AttributeError|NoneType")


class FadeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.store = prepare(temp.name)
        self.host = Host()
        self.t0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()
        self.now = self.t0
        self.service = Companion(self.store, self.host, lambda: self.now)
        self.service.signal("s0", "chat", "I want to compare bicycles")

    def goal(self, key="g", **extra):
        return self.service.goal_create({"id": key, "title": f"Goal {key} bicycle", "description": "d", "completion_criteria": "c", "signal_id": "s0", **extra})

    def at(self, days):
        self.now = self.t0 + days * DAY

    watch_active = staticmethod(lambda job_id, goal_id: False)

    def fade(self):
        return Fade(self.home, self.store.goals(), self.now, self.watch_active)

    def state(self, key="g"):
        return self.fade().of(self.store.goal(key)[0])

    def ask(self, key="g", status="sent"):
        """The nightly review's dormancy question for the goal's current episode, in the given notice state."""
        row = self.service.notification_add({"event_key": self.state(key)["ask_key"], "message": "Still want this?", "rationale": "dormant", "sources": [f"goal:{key}"],
                                             "goal_id": key, "verified_at": self.now, "expires": self.now + 3 * DAY})
        with self.store.transaction() as db:
            self.store.put(db, "notification", row["id"], {**row, "status": status, **({"dispatched_at": self.now} if status == "sent" else {})})
        return row

    def watch(self, goal_id, expires):
        self.store.write_text("install.json", json.dumps({"plugin": "hermes-muse", "installation_id": "fixture", "fixed": {}, "jobs": {
            "job1": {"key": "watch-abc", "owner": goal_id, "expires": expires}}}))


class Timeline(FadeCase):
    def test_active_day6_dormant_day7_cancelled_day14(self):
        self.goal()
        self.at(6.99)
        self.assertEqual(self.state()["state"], "active")
        self.at(7)
        dormant = self.state()
        self.assertEqual((dormant["state"], dormant["dormant_since"], dormant["fade_due"]), ("dormant", self.t0 + 7 * DAY, False))
        self.ask()
        self.at(13.99)
        self.assertFalse(self.state()["fade_due"])
        with self.assertRaises(DomainError) as early:
            self.service.fade_goal({"id": "g", "status": "cancelled"})
        self.assertIn("cancel time has not come", str(early.exception))
        self.at(14)
        self.assertTrue(self.state()["fade_due"])
        cancelled = self.service.fade_goal({"id": "g", "status": "cancelled"})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["progress"][-1]["text"], "no user activity for 14 days; auto-faded")
        self.assertEqual(self.host.paused, ["g"])  # existing cancel behaviour stops owned watches

    def test_activity_resets_the_clock(self):
        self.goal("a"); self.goal("b"); self.goal("c"); self.goal("parent"); self.goal("kid", parent_id="parent")
        self.at(5)
        self.service.goal_update({"id": "a", "progress": "Compared two models"})
        self.service.signal("s1", "chat", "Any news on goal b bicycle?")
        self.service.signal("s2", "chat", "Unrelated chatter about lunch")
        self.at(8)
        self.service.goal_update({"id": "kid", "note": "Subgoal moved"})
        self.at(10)
        self.assertEqual(self.state("a")["state"], "active")      # progress on day 5 -> dormant on day 12
        self.assertEqual(self.state("b")["state"], "active")      # mention on day 5
        self.assertEqual(self.state("c")["state"], "dormant")     # only the unrelated signal
        self.assertEqual(self.state("parent")["state"], "active")  # an active subgoal keeps its parent alive
        self.at(15.1)
        self.assertEqual(self.state("a")["state"], "dormant")
        self.assertEqual(self.state("a")["dormant_since"], self.t0 + 12 * DAY)

    def test_deadline_or_next_check_change_is_activity(self):
        self.goal()
        self.at(6)
        self.service.goal_update({"id": "g", "next_check_at": self.now + DAY})
        self.at(12)
        self.assertEqual(self.state()["state"], "active")

    def test_revival_by_parent_goal_update(self):
        self.goal()
        self.at(8)
        self.ask()
        self.at(15)
        self.service.fade_goal({"id": "g", "status": "cancelled"})
        revived = self.service.goal_update({"id": "g", "status": "active", "note": "User asked again"})
        self.assertEqual(revived["status"], "active")
        self.assertEqual(self.state()["state"], "active")
        self.at(21.9)
        self.assertEqual(self.state()["state"], "active")
        self.at(22)
        self.assertEqual(self.state()["state"], "dormant")

    def test_records_without_timestamps_never_fade(self):
        self.assertEqual(Fade(self.home, [], self.now).of({"id": "old", "title": "Legacy", "status": "active"})["state"], "active")


class Exemptions(FadeCase):
    def test_future_deadline_watch_and_running_owner_never_fade_but_waiting_and_blocked_do(self):
        self.goal("dated", deadline=self.t0 + 30 * DAY)
        self.goal("watched")
        self.goal("owned", owner={"kind": "native_delegation", "id": "deleg_1"})
        self.goal("waiting"); self.goal("stuck"); self.goal("past", deadline=self.t0 + 2 * DAY); self.goal("pinned", no_fade=True)
        self.service.goal_update({"id": "waiting", "waiting_user": True})
        self.service.goal_update({"id": "stuck", "blocked": "needs a login"})
        self.watch("watched", self.t0 + 40 * DAY)
        self.watch_active = staticmethod(lambda job_id, goal_id: goal_id == "watched")
        self.at(20)
        with patch("hermes_muse.followup.owner_status", side_effect=lambda home, handle: "running" if handle else "unassigned"):
            self.assertIn("deadline", self.state("dated")["exempt"])
            self.assertIn("watch", self.state("watched")["exempt"])
            self.assertIn("owner", self.state("owned")["exempt"])
            self.assertIn("no_fade", self.state("pinned")["exempt"])
            for key in ("waiting", "stuck", "past"):
                self.assertEqual(self.state(key)["state"], "dormant", key)
            with self.assertRaises(DomainError) as refused:
                self.service.fade_goal({"id": "dated", "status": "cancelled"})
            self.assertIn("exempt because it has a future deadline", str(refused.exception))
        self.watch_active = staticmethod(lambda job_id, goal_id: False)  # watch paused or removed
        self.assertEqual(self.state("watched")["state"], "dormant")
        with patch("hermes_muse.followup.owner_status", return_value="completed"):
            self.assertEqual(self.state("owned")["state"], "dormant")

    def test_exemption_that_lapses_restarts_the_clock(self):
        self.goal("dated", deadline=self.t0 + 30 * DAY)
        self.goal("watched")
        self.watch("watched", self.t0 + 30 * DAY)
        self.watch_active = staticmethod(lambda job_id, goal_id: True)
        self.at(29)
        self.assertEqual((self.state("dated")["state"], self.state("watched")["state"]), ("active", "active"))
        self.at(30.1)
        self.assertEqual((self.state("dated")["state"], self.state("watched")["state"]), ("active", "active"))  # idle for 0.1 days since the exemption ended
        self.at(36.9)
        self.assertEqual(self.state("dated")["state"], "active")
        self.at(37)
        dormant = self.state("dated")
        self.assertEqual((dormant["state"], dormant["dormant_since"]), ("dormant", self.t0 + 37 * DAY))
        self.assertEqual(self.state("watched")["state"], "dormant")
        self.ask("dated")
        self.at(43.9)
        self.assertFalse(self.state("dated")["fade_due"])
        self.at(44.1)
        self.assertTrue(self.state("dated")["fade_due"])

    def test_exempt_subgoal_keeps_its_parent_and_is_never_cascaded(self):
        for key, extra in (("p1", {}), ("p2", {}), ("p3", {})):
            self.goal(key, **extra)
        self.goal("c1", parent_id="p1", deadline=self.t0 + 60 * DAY)
        self.goal("c2", parent_id="p2", no_fade=True)
        self.goal("c3", parent_id="p3")
        self.watch("c3", self.t0 + 60 * DAY)
        self.watch_active = staticmethod(lambda job_id, goal_id: goal_id == "c3")
        self.at(20)
        for parent in ("p1", "p2", "p3"):
            state = self.state(parent)
            self.assertEqual(state["state"], "active", parent)
            self.assertIn("active subgoal is exempt", state["exempt"])
            with self.assertRaises(DomainError):
                self.service.fade_goal({"id": parent, "status": "cancelled"})
            self.assertEqual(self.store.goal(parent)[0]["status"], "active")
        self.assertEqual([self.store.goal(c)[0]["status"] for c in ("c1", "c2", "c3")], ["active"] * 3)

    def test_unreadable_watch_state_means_do_not_fade(self):
        self.goal("watched")
        self.watch("watched", self.t0 + 60 * DAY)
        self.at(20)
        def broken(job_id, goal_id):
            raise ImportError("native scheduler unavailable")
        self.watch_active = staticmethod(broken)
        state = self.state("watched")
        self.assertEqual(state["state"], "active")
        self.assertIn("could not be read", state["exempt"])
        (self.home / "muse/install.json").write_text("{ not json")
        self.assertEqual(self.fade().of(self.store.goal("watched")[0])["state"], "active")

    def test_expired_watch_does_not_exempt(self):
        self.goal("watched")
        self.watch("watched", self.t0 + DAY)
        self.watch_active = staticmethod(lambda job_id, goal_id: True)
        self.at(20)
        self.assertEqual(self.state("watched")["state"], "dormant")
        self.assertEqual(self.state("watched")["dormant_since"], self.t0 + 8 * DAY)  # clock restarted when the watch expired


class Exclusions(FadeCase):
    def test_followups_hint_views_and_wake_gate(self):
        self.goal("quiet"); self.goal("busy")
        for key in ("quiet", "busy"):
            self.service.goal_update({"id": key, "next_check_at": self.t0 + 60})
        self.at(6)
        self.service.goal_update({"id": "busy", "progress": "Still going", "next_check_at": self.t0 + 6 * DAY})
        self.at(8)
        self.assertEqual([i["id"] for i in snapshot(self.home, now=self.now)["items"]], ["busy"])
        text = followup_context(self.home, now=self.now)
        self.assertNotIn("quiet", text)
        listing = Views(self.home, now=self.now).read({"kind": "goal"})["items"]
        rows = {r["id"]: r for r in listing}
        self.assertEqual(rows["quiet"]["dormant_since"], self.t0 + 7 * DAY)
        self.assertNotIn("dormant_since", rows["busy"])
        self.assertEqual([r["id"] for r in Views(self.home, now=self.now).read({"kind": "goal", "status": "dormant"})["items"]], ["quiet"])
        self.assertEqual({r["id"] for r in Views(self.home, now=self.now).read({"kind": "goal", "status": "open"})["items"]}, {"quiet", "busy"})
        data = wake_gate.collect(self.store, "proactive-watch", self.now, self.now - 3600)
        self.assertEqual([g["id"] for g in data["goals"]], ["busy"])
        with self.assertRaises(DomainError) as bad:
            Views(self.home).read({"kind": "goal", "status": "sleeping"})
        self.assertIn("open|closed|dormant", str(bad.exception))

    def test_fade_off_keeps_everything_visible(self):
        self.service.preference_update({"goal_dormant_days": None})
        self.goal()
        self.at(400)
        self.assertEqual(self.state()["state"], "active")


class Preferences(FadeCase):
    def test_days_are_configurable_and_validated(self):
        self.assertEqual((self.service.preferences()["goal_dormant_days"], self.service.preferences()["goal_fade_days"]), (7, 7))
        self.assertIn("goal_dormant_days", FIELDS["preferences"].split())
        self.assertIn("goal_fade_days", FIELDS["preferences"].split())
        self.service.preference_update({"goal_dormant_days": 3, "goal_fade_days": 2})
        self.goal()
        self.at(2.9)
        self.assertEqual(self.state()["state"], "active")
        self.at(3)
        self.assertEqual(self.state()["state"], "dormant")
        self.ask()
        self.at(4.9)
        self.assertFalse(self.state()["fade_due"])
        self.at(5)
        self.assertTrue(self.state()["fade_due"])
        self.service.preference_update({"goal_fade_days": None})  # never cancel automatically
        self.at(100)
        self.assertEqual((self.state()["state"], self.state()["fade_due"], self.state()["fade_at"]), ("dormant", False, None))
        with self.assertRaises(DomainError) as off:
            self.service.fade_goal({"id": "g", "status": "cancelled"})
        self.assertIn("fading is off", str(off.exception))

    def test_invalid_values_get_an_actionable_error(self):
        for key in ("goal_dormant_days", "goal_fade_days"):
            for bad in (0, 366, -1, "7", 1.5, True, [7]):
                with self.assertRaises(DomainError, msg=(key, bad)) as caught:
                    self.service.preference_update({key: bad})
                error = caught.exception
                self.assertEqual(error.details["field"], "data." + key)
                self.assertIn("1 to 365", error.details["expected"])
                self.assertIn("whole number", error.details["hint"])
                self.assertIn("received", error.details)
                self.assertNotRegex(str(error), LEAK)
        self.assertEqual(self.service.preferences()["goal_dormant_days"], 7)

    def test_documented(self):
        for path in ("docs/INTERFACE.md", "README.md", "skills/companion/references/goals.md"):
            self.assertIn("goal_dormant_days", (ROOT / path).read_text(encoding="utf-8"), path)


class FadeGuardInputs(FadeCase):
    def test_only_a_plain_cancel_is_accepted(self):
        self.goal()
        self.at(15)
        for data in ({"id": "g", "status": "done"}, {"id": "g", "status": "active"}, {"id": "g"}, {"id": "g", "status": "cancelled", "owner": None},
                     {"id": "g", "status": "cancelled", "progress": "x"}, {"status": "cancelled"}):
            with self.assertRaises(DomainError, msg=data) as caught:
                self.service.fade_goal(data)
            error = caught.exception
            self.assertEqual(error.details["field"], "data")
            self.assertIn("status", error.details["expected"])
            self.assertIn("Fix:", str(error))
            self.assertNotRegex(str(error), LEAK)
        self.assertEqual(self.store.goal("g")[0]["status"], "active")

    def test_unknown_and_inactive_goals_are_refused_with_a_reason(self):
        self.goal()
        self.service.goal_update({"id": "g", "status": "cancelled"})
        with self.assertRaises(DomainError) as caught:
            self.service.fade_goal({"id": "g", "status": "cancelled"})
        self.assertIn("it is not active", str(caught.exception))
        self.assertEqual(caught.exception.details["field"], "data.id")
        with self.assertRaises(ValueError):
            self.service.fade_goal({"id": "missing", "status": "cancelled"})

    def test_ask_once_per_dormancy_episode(self):
        self.goal()
        self.at(8)
        first = self.state()["ask_key"]
        self.assertRegex(first, r"^goal-fade-ask:g:\d{8}$")
        row = self.ask()
        self.assertEqual(self.service.notification_add({"event_key": first, "message": "again", "rationale": "dormant", "sources": ["goal:g"], "goal_id": "g",
                                                         "verified_at": self.now, "expires": self.now + 3 * DAY})["id"], row["id"])
        self.assertEqual(len(self.store.all("notification")), 1)
        self.at(15)
        self.service.fade_goal({"id": "g", "status": "cancelled"})
        self.service.goal_update({"id": "g", "status": "active", "note": "back"})
        self.at(30)
        second = self.state()["ask_key"]
        self.assertNotEqual(first, second)  # a revived goal that goes dormant again may be asked again
        self.ask()
        self.assertEqual(len(self.store.all("notification")), 2)


class AskBeforeCancel(FadeCase):
    def test_first_night_backlog_is_asked_then_cancelled_a_day_after_delivery(self):
        keys = [f"old{i}" for i in range(5)]
        for key in keys:
            self.goal(key)
        self.at(40)  # idle far beyond 14 days: the first night after rollout
        self.assertTrue(all(self.state(k)["state"] == "dormant" and not self.state(k)["fade_due"] for k in keys))
        for key in keys:
            with self.assertRaises(DomainError) as early:
                self.service.fade_goal({"id": key, "status": "cancelled"})
            self.assertIn("has not been asked yet", str(early.exception))
        rows = {key: self.ask(key, status="candidate") for key in keys}
        for key in keys:  # asked in this very run: neither candidate nor fresh may cancel
            with self.assertRaises(DomainError) as same_night:
                self.service.fade_goal({"id": key, "status": "cancelled"})
            self.assertIn("not been delivered yet", str(same_night.exception))
            self.assertEqual(self.store.goal(key)[0]["status"], "active")
        with self.store.transaction() as db:  # the patrol delivers them
            for row in rows.values():
                self.store.put(db, "notification", row["id"], {**row, "status": "sent", "dispatched_at": self.now})
        self.at(40.5)
        with self.assertRaises(DomainError) as soon:
            self.service.fade_goal({"id": "old0", "status": "cancelled"})
        self.assertIn("less than a day ago", str(soon.exception))
        self.at(41.1)
        for key in keys:
            self.assertEqual(self.service.fade_goal({"id": key, "status": "cancelled"})["status"], "cancelled")

    def test_listing_says_what_the_row_is_waiting_for(self):
        self.goal()
        self.at(9)
        row = Views(self.home, now=self.now).read({"kind": "goal", "status": "dormant"})["items"][0]
        self.assertIn("not been asked yet", row["fade_wait"])
        self.assertRegex(row["ask_key"], r"^goal-fade-ask:g:")


class OnlyADeliveredQuestionCounts(FadeCase):
    def idle_goal(self, key="g"):
        self.goal(key)
        self.at(40)
        return self.ask(key, status="candidate")

    def put(self, row, **changes):
        with self.store.transaction() as db:
            self.store.put(db, "notification", row["id"], {**row, **changes})

    def test_every_state_but_sent_leaves_the_goal_dormant(self):
        row = self.idle_goal()
        self.at(43)
        for status in ("candidate", "pending", "queued", "dispatching", "transport_queued", "expired", "failed", "cancelled", "dismissed", "unknown", "done", "stopped", "accepted", "suppressed"):
            self.put(row, status=status, created=self.now - 2 * DAY)
            state = self.state()
            self.assertEqual((state["state"], state["fade_due"]), ("dormant", False), status)
            with self.assertRaises(DomainError, msg=status) as caught:
                self.service.fade_goal({"id": "g", "status": "cancelled"})
            withdrawn = status in {"expired", "failed", "cancelled"}  # asked again under the next key
            self.assertIn("has not been asked yet" if withdrawn else "has not been delivered yet", str(caught.exception))
            self.assertEqual(state["ask_key"].endswith(":r1"), withdrawn, status)
            self.assertEqual(self.store.goal("g")[0]["status"], "active")
        self.put(row, status="sent", created=self.now - 2 * DAY)  # sent but never dispatched: nothing shows it reached the user
        self.assertFalse(self.state()["fade_due"])
        self.put(row, status="sent", created=self.now - 2 * DAY, dispatched_at=self.now - 2 * DAY)
        self.assertTrue(self.state()["fade_due"])

    def test_dormancy_question_cannot_be_resolved_by_topic_and_is_reasked_after_withdrawal(self):
        row = self.idle_goal()
        self.at(43)
        self.service.conversation_states = lambda **kw: {}
        with self.assertRaises(DomainError) as caught:
            self.service.notification_review({"id": row["id"], "verdict": "resolved", "reason": "a related topic moved"})
        self.assertEqual(caught.exception.details["field"], "data.verdict")
        self.assertEqual(self.service.notification_review({"id": row["id"], "verdict": "relevant", "reason": "still dormant"})["reviewed"], True)
        base = self.state()["ask_key"]
        for attempt in range(1, 4):  # withdrawn before delivery: the next key asks again, at most three times
            self.put(self.store.read("notification", row["id"]), status="cancelled")
            row = self.ask()
            self.assertEqual(row["event_key"], f"{base}:r{attempt}")
        self.put(row, status="cancelled")
        self.assertEqual(self.state()["ask_key"], f"{base}:r3")
        self.service.goal_update({"id": "g", "progress": "User revived it"})  # no longer dormant: the topic may close it
        self.put(row, status="candidate")
        self.assertTrue(self.service.notification_review({"id": row["id"], "verdict": "superseded", "reason": "goal revived"})["reviewed"])

    def test_the_day_counts_from_delivery_not_creation(self):
        row = self.idle_goal()
        self.at(43)
        self.put(row, status="sent", created=self.now - 3 * DAY, dispatched_at=self.now - 3600)
        self.assertFalse(self.state()["fade_due"])
        self.assertIn("less than a day ago", self.state()["wait"])
        self.put(row, status="sent", created=self.now - 3 * DAY, dispatched_at=self.now - 25 * 3600)
        self.assertTrue(self.state()["fade_due"])
        self.put(row, status="sent", created=self.now - 3 * DAY)  # no dispatched_at: there is no fallback to created
        self.assertFalse(self.state()["fade_due"])
        self.assertIn("has not been delivered yet", self.state()["wait"])

    def test_a_notice_for_another_goal_does_not_count(self):
        row = self.idle_goal()
        self.goal("other")
        self.at(43)
        self.put(row, status="sent", created=self.now - 2 * DAY, goal_id="other")
        self.assertFalse(self.state()["fade_due"])
        self.assertIn("has not been delivered yet", self.state()["wait"])

    def test_malformed_values_mean_do_not_fade_and_never_raise(self):
        row = self.idle_goal()
        self.at(43)
        self.put(row, status="sent", created="yesterday", dispatched_at="yesterday")
        state = self.state()
        self.assertFalse(state["fade_due"])
        self.assertIn("could not be read", state["wait"])
        self.put(row, status="sent", created=self.now - 2 * DAY, dispatched_at=self.now - 2 * DAY)
        self.assertTrue(self.state()["fade_due"])
        self.goal("w")
        self.watch("w", "never")
        self.watch_active = staticmethod(lambda job_id, goal_id: False)
        self.assertEqual(self.state("w")["state"], "active")  # a text expiry cannot be compared: no fade, no TypeError
        self.assertIn("could not be read", self.state("w")["exempt"])
        self.assertEqual([g["id"] for g in wake_gate.collect(self.store, "proactive-watch", self.now, self.now - 3600)["goals"]].count("w"), 1)

    def test_owner_status_that_cannot_be_read_does_not_fade(self):
        self.goal("owned", owner={"kind": "cron", "id": "job-1"})
        self.goal("unowned")
        self.at(20)
        with patch("hermes_muse.followup.owner_status", return_value="unknown"):
            state = self.state("owned")
        self.assertEqual(state["state"], "active")
        self.assertIn("owner's status could not be read", state["exempt"])
        with patch("hermes_muse.followup.owner_status", return_value="unassigned"):
            self.assertEqual(self.state("unowned")["state"], "dormant")


class QuestionMustFollowDormancy(FadeCase):
    def test_a_question_staged_before_dormancy_does_not_count(self):
        self.goal()
        key = ask_key("g", self.t0 + 7 * DAY)  # the key is predictable from the goal's last activity
        row = self.service.notification_add({"event_key": key, "message": "Weather is nice today", "rationale": "x", "sources": ["goal:g"], "goal_id": "g",
                                             "verified_at": self.now, "expires": self.now + 30 * DAY})
        with self.store.transaction() as db:
            self.store.put(db, "notification", row["id"], {**row, "status": "sent", "dispatched_at": self.now})
        self.at(15)
        state = self.state()
        self.assertEqual((state["ask_key"], state["fade_due"]), (key, False))
        self.assertIn("has not been asked yet", state["wait"])
        with self.assertRaises(DomainError):
            self.service.fade_goal({"id": "g", "status": "cancelled"})
        self.assertEqual(self.store.goal("g")[0]["status"], "active")

    def test_bad_record_is_logged_and_does_not_affect_later_goals(self):
        self.goal("bad"); self.goal("good")
        self.at(10)
        goals = self.store.goals()
        for goal in goals:
            if goal["id"] == "bad":
                goal["created"] = "yesterday"
        fade = Fade(self.home, goals, self.now)
        with self.assertLogs("hermes_muse.fade", "WARNING") as logged:
            first = fade.of(next(g for g in goals if g["id"] == "bad"))
        self.assertIn("goal bad", logged.output[0])
        self.assertEqual((first["state"], first["fade_due"]), ("active", False))
        self.assertEqual(fade.of(next(g for g in goals if g["id"] == "good"))["state"], "dormant")  # evaluated after the bad one

    def test_owner_that_cannot_be_read_protects_a_goal_only_for_thirty_days(self):
        self.goal("owned", owner={"kind": "native_delegation", "id": "deleg_gone"})
        __import__("hermes_muse.fade", fromlist=["_warned"])._warned.clear()
        with patch("hermes_muse.followup.owner_status", return_value="unknown"):
            self.at(29.9)
            self.assertIn("could not be read", self.state("owned")["exempt"])
            with self.assertLogs("hermes_muse.fade", "WARNING") as logged:
                self.at(30.1)
                state = self.state("owned")
            self.assertEqual(state["state"], "dormant")
            self.assertIn("still unreadable", logged.output[0])
            self.at(400)
            self.assertEqual(self.state("owned")["state"], "dormant")
            self.assertEqual(self.state("owned")["wait"], "the dormancy question has not been asked yet")  # still asked first
            self.ask("owned")
            self.at(402)
            self.assertTrue(self.state("owned")["fade_due"])


class UnreadableMeansNoFade(FadeCase):
    def test_damaged_or_invalid_preferences_do_not_turn_fading_on(self):
        self.service.preference_update({"goal_dormant_days": None})
        self.goal()
        self.at(30)
        prefs = self.home / "muse/PROACTIVE_PREFERENCES.md"
        original = prefs.read_text(encoding="utf-8")
        for damaged in ("not a header\nbody", '<!-- hermes-muse {"goal_dormant_days": 0} -->\nbody', "<!-- hermes-muse {broken -->\nbody"):
            prefs.write_text(damaged, encoding="utf-8")
            state = self.state()
            self.assertEqual(state["state"], "active", damaged)
            self.assertIn("could not be read", state["exempt"])
            with self.assertRaises(DomainError) as caught:
                self.service.fade_goal({"id": "g", "status": "cancelled"})
            self.assertIn("could not be read", str(caught.exception))
        prefs.unlink()  # a missing file simply means defaults
        self.assertEqual(self.state()["state"], "dormant")
        prefs.write_text(original, encoding="utf-8")
        self.assertEqual(self.state()["state"], "active")  # dormancy off again

    def test_unreadable_signal_store_does_not_fade(self):
        self.goal()
        self.at(30)
        with patch("hermes_muse.fade.sqlite3.connect", side_effect=__import__("sqlite3").OperationalError("database is locked")):
            state = self.state()
        self.assertEqual(state["state"], "active")
        self.assertIn("could not be read", state["exempt"])


class NoFade(FadeCase):
    def test_marker_exempts_forever_and_is_set_or_cleared_by_the_parent(self):
        self.goal("pinned", no_fade=True)
        self.goal("plain")
        self.at(400)
        self.assertEqual(self.state("pinned")["state"], "active")
        self.assertEqual(self.state("plain")["state"], "dormant")
        self.assertTrue(self.store.goal("pinned")[0]["no_fade"])
        self.assertNotIn("no_fade", self.store.goal("plain")[0])
        self.service.goal_update({"id": "plain", "no_fade": True})
        self.assertEqual(self.state("plain")["state"], "active")
        self.service.goal_update({"id": "pinned", "no_fade": False})
        self.assertEqual(self.state("pinned")["state"], "active")  # the update itself is activity
        row = next(r for r in Views(self.home, now=self.now).read({"kind": "goal"})["items"] if r["id"] == "plain")
        self.assertIs(row["no_fade"], True)

    def test_wrong_type_gets_an_actionable_error(self):
        for action in ("create", "update"):
            for bad in ("yes", 1, None, [True]):
                with self.assertRaises(DomainError, msg=(action, bad)) as caught:
                    if action == "create":
                        self.goal("x", no_fade=bad)
                    else:
                        self.goal(f"y{type(bad).__name__}{id(bad) % 1000}")
                        self.service.goal_update({"id": f"y{type(bad).__name__}{id(bad) % 1000}", "no_fade": bad})
                error = caught.exception
                self.assertEqual(error.details["field"], "data.no_fade")
                self.assertIn("true", error.details["expected"])
                self.assertIn("received", error.details)
                self.assertIn("JSON boolean", error.details["hint"])
                self.assertNotRegex(str(error), LEAK)

    def test_contract_and_docs(self):
        self.assertIn("no_fade", FIELDS["goal_create"].split())
        self.assertIn("no_fade", FIELDS["goal_update"].split())
        for path in ("docs/INTERFACE.md", "README.md", "skills/companion/references/goals.md"):
            self.assertIn("no_fade", (ROOT / path).read_text(encoding="utf-8"), path)

    def test_nightly_review_cannot_set_it(self):
        self.service.signal("n1", "chat", "Please remind me about the report")
        self.store.write_text("install.json", json.dumps({"plugin": "hermes-muse", "installation_id": "fixture", "jobs": {}, "fixed": {}}))
        with self.store.transaction() as db:
            self.store.put(db, "run", "night", {"id": "night", "job": "nightly-review", "started": 0, "status": "running", "stages": {}})
        with self.assertRaises(DomainError) as caught:
            self.service.intake_goal_create({"id": "n", "title": "Report", "description": "d", "completion_criteria": "c", "signal_id": "n1", "no_fade": True}, {"id": "night"})
        self.assertEqual(caught.exception.details["field"], "data.no_fade")
        self.assertIn("Fix:", str(caught.exception))
        self.assertFalse((self.home / "muse/workspace/goals/n").exists())
        with self.assertRaises(DomainError) as update:
            self.service.fade_goal({"id": "n", "status": "cancelled", "no_fade": False})
        self.assertEqual(update.exception.details["field"], "data")


class CronCannotMoveTheThresholds(FadeCase):
    def test_background_runs_cannot_write_fade_preferences(self):
        for data in ({"goal_dormant_days": 1}, {"goal_fade_days": 1}, {"goal_dormant_days": None, "goal_fade_days": 2}):
            with self.assertRaises(DomainError, msg=data) as caught:
                self.service.preference_update(data, cron=True)
            error = caught.exception
            self.assertIn("goal_", error.details["field"])
            self.assertIn("private parent conversation", error.details["hint"] + error.details["expected"] + str(error))
            self.assertIn("run_complete", error.details["hint"])
            self.assertIn("received", error.details)
            self.assertNotRegex(str(error), LEAK)
        self.assertEqual((self.service.preferences()["goal_dormant_days"], self.service.preferences()["goal_fade_days"]), (7, 7))
        self.assertTrue(self.service.preference_update({"feed_brief": "Gardening"}, cron=True)["changed"])  # other preferences are unchanged
        self.assertEqual(self.service.preference_update({"goal_dormant_days": 3})["goal_dormant_days"], 3)


class Prompts(unittest.TestCase):
    def test_nightly_preserves_guarded_intake_fade_and_optional_ideas(self):
        text=(ROOT/'prompts/nightly-review.md').read_text()
        self.assertLess(text.index('Goal intake:'),text.index('Goal fade:'))
        self.assertLess(text.index('Goal fade:'),text.index('Ideas:'))
        for key in ('signal_id','fade_due=true','ask_key','0–3','delivered-ask','confirmed'):
            self.assertIn(key,text)

    def test_patrol_leaves_dormant_goals_alone(self):
        self.assertIn("dormant", (ROOT / "prompts/proactive-watch.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
