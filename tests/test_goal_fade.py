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
from hermes_muse.fade import Fade
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

    def state(self, key="g"):
        return Fade(self.home, self.store.goals(), self.now).of(self.store.goal(key)[0])


class Timeline(FadeCase):
    def test_active_day6_dormant_day7_cancelled_day14(self):
        self.goal()
        self.at(6.99)
        self.assertEqual(self.state()["state"], "active")
        self.at(7)
        dormant = self.state()
        self.assertEqual((dormant["state"], dormant["dormant_since"], dormant["fade_due"]), ("dormant", self.t0 + 7 * DAY, False))
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
        self.goal("waiting"); self.goal("stuck"); self.goal("past", deadline=self.t0 + 2 * DAY)
        self.service.goal_update({"id": "waiting", "waiting_user": True})
        self.service.goal_update({"id": "stuck", "blocked": "needs a login"})
        self.store.write_text("install.json", json.dumps({"plugin": "hermes-muse", "installation_id": "fixture", "fixed": {}, "jobs": {
            "job1": {"key": "watch-abc", "owner": "watched", "expires": self.t0 + 40 * DAY}}}))
        (self.home / "cron").mkdir()
        (self.home / "cron/jobs.json").write_text(json.dumps({"jobs": [{"id": "job1", "enabled": True}]}))
        self.at(20)
        with patch("hermes_muse.followup.owner_status", side_effect=lambda home, handle: "running" if handle else "unassigned"):
            self.assertIn("deadline", self.state("dated")["exempt"])
            self.assertIn("watch", self.state("watched")["exempt"])
            self.assertIn("owner", self.state("owned")["exempt"])
            for key in ("waiting", "stuck", "past"):
                self.assertEqual(self.state(key)["state"], "dormant", key)
            with self.assertRaises(DomainError) as refused:
                self.service.fade_goal({"id": "dated", "status": "cancelled"})
            self.assertIn("exempt because it has a future deadline", str(refused.exception))
        (self.home / "cron/jobs.json").write_text(json.dumps({"jobs": [{"id": "job1", "enabled": False}]}))
        self.assertEqual(Fade(self.home, self.store.goals(), self.now).of(self.store.goal("watched")[0])["state"], "dormant")
        with patch("hermes_muse.followup.owner_status", return_value="completed"):
            self.assertEqual(self.state("owned")["state"], "dormant")

    def test_expired_watch_does_not_exempt(self):
        self.goal("watched")
        self.store.write_text("install.json", json.dumps({"plugin": "hermes-muse", "installation_id": "fixture", "fixed": {}, "jobs": {
            "job1": {"key": "watch-abc", "owner": "watched", "expires": self.t0 + DAY}}}))
        (self.home / "cron").mkdir()
        (self.home / "cron/jobs.json").write_text(json.dumps({"jobs": [{"id": "job1", "enabled": True}]}))
        self.at(20)
        self.assertEqual(self.state("watched")["state"], "dormant")


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

    def test_ask_once_uses_a_fixed_event_key(self):
        self.goal()
        ask = {"event_key": "goal-fade-ask:g", "message": "Still want this?", "rationale": "dormant", "sources": ["goal:g"], "goal_id": "g",
               "verified_at": self.now, "expires": self.now + 3 * DAY}
        first = self.service.notification_add(dict(ask))
        self.assertEqual(self.service.notification_add(dict(ask))["id"], first["id"])
        self.assertEqual(len(self.store.all("notification")), 1)


class Prompts(unittest.TestCase):
    def test_nightly_fade_step_sits_between_intake_and_ideas(self):
        paragraphs = (ROOT / "prompts/nightly-review.md").read_text(encoding="utf-8").split("\n\n")
        order = [next(i for i, p in enumerate(paragraphs) if p.startswith(start)) for start in ("Goal intake step", "Goal fade step", "Ideas step")]
        self.assertEqual(order, sorted(order))
        step = paragraphs[order[1]]
        for needle in ("status=dormant", "goal-fade-ask:", "fade_due=true", 'status="cancelled"', "never cancel a goal that is not listed"):
            self.assertIn(needle, step)
        closing = next(p for p in paragraphs if p.startswith("Complete recoverable work"))
        self.assertIn("one sentence on Goal fade", closing)
        self.assertIn("the Goal fade sentence", closing)
        self.assertIn("dormant goals are not eligible", next(p for p in paragraphs if p.startswith("Research eligible")))

    def test_patrol_leaves_dormant_goals_alone(self):
        self.assertIn("dormant", (ROOT / "prompts/proactive-watch.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
