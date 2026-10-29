import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_muse.host_v1 import HermesHost
from hermes_muse.service import Companion
from hermes_muse.store import Store


def home_targets(**chat_ids):
    """Stand-in for Hermes' cron.scheduler_delivery with the given /sethome channels."""
    module = types.ModuleType("cron.scheduler_delivery")
    module._iter_home_target_platforms = lambda: list(chat_ids)
    module._get_home_target_chat_id = lambda platform: chat_ids.get(platform, "")
    return patch.dict(sys.modules, {"cron": types.ModuleType("cron"), "cron.scheduler_delivery": module})


class TestDeliveryResolution(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.host = HermesHost(self.store, plugin_root=Path(__file__).parents[1])
        template = (Path(__file__).parents[1] / "templates/PROACTIVE_PREFERENCES.md").read_text(encoding="utf-8")
        self.store.write_text("PROACTIVE_PREFERENCES.md", template)
        self.service = Companion(self.store, self.host)

    def test_default_deliver_fallback_to_bot_chat(self):
        with home_targets():
            self.assertEqual(self.host.default_deliver(), "bot-chat")

    def test_default_deliver_without_cron_module_is_bot_chat(self):
        with patch.dict(sys.modules, {"cron": None, "cron.scheduler_delivery": None}):
            self.assertEqual(self.host.default_deliver(), "bot-chat")

    def test_default_deliver_auto_resolves_all_when_home_channel_present(self):
        with home_targets(telegram="123"):
            self.assertEqual(self.host.default_deliver(), "all")

    def test_plugin_load_never_asks_hermes_for_home_channels(self):
        with home_targets(telegram="123"), patch("cron.scheduler_delivery._iter_home_target_platforms", side_effect=AssertionError("load-time home lookup")):
            self.assertEqual(self.host.default_deliver(detect_home=False), "bot-chat")
        self.service.preference_update({"deliver": "telegram"})
        self.assertEqual(self.host.default_deliver(detect_home=False), "telegram")

    def test_platform_without_home_chat_id_does_not_select_all(self):
        with home_targets(telegram=""):
            self.assertEqual(self.host.default_deliver(), "bot-chat")

    def test_explicit_preference_overrides_home_detection(self):
        self.service.preference_update({"deliver": "telegram"})
        with home_targets(signal="+551199999999"):
            self.assertEqual(self.host.default_deliver(), "telegram")

    def test_explicit_bot_chat_and_all_are_honoured_either_way(self):
        for choice, homes in (("bot-chat", {"telegram": "123"}), ("all", {})):
            self.service.preference_update({"deliver": choice})
            with home_targets(**homes):
                self.assertEqual(self.host.default_deliver(), choice)

    def test_null_restores_automatic_selection(self):
        self.service.preference_update({"deliver": "telegram"})
        self.service.preference_update({"deliver": None})
        with home_targets(telegram="123"):
            self.assertEqual(self.host.default_deliver(), "all")

    def test_invalid_deliver_preference_raises_with_repair_hint(self):
        for bad in (123, "   ", "bot-chat,all"):
            with self.assertRaises(Exception) as caught:
                self.service.preference_update({"deliver": bad})
            self.assertEqual(caught.exception.code, "invalid_argument")
            self.assertEqual(caught.exception.details["field"], "data.deliver")
            self.assertIn("Fix:", str(caught.exception))
        self.assertIsNone(self.service.preferences()["deliver"])

    def test_muse_manage_contract_accepts_deliver(self):
        from hermes_muse.contracts import validate
        validate("preferences", {"deliver": "all", "signal_id": "s", "request_id": "r", "expected_revision": "absent"})


class TestNonBotChatNotificationFlow(unittest.TestCase):
    """A job delivering to `all` or a platform keeps the notice flow intact; only the Bot Chat envelope is skipped."""

    def setUp(self):
        import contextlib
        from test_companion import CompanionTests
        self.contextlib = contextlib
        self.case = CompanionTests("test_default_allows_independent_notices_and_explicit_limit_still_applies")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def reconcile(self, outcome, queue_status=None):
        case = self.case
        ledger = types.ModuleType("cron.executions")
        ledger.get_execution = lambda eid: {"id": "attempt-one", "status": "completed", "delivery_outcome": outcome}
        ledger.latest_execution = ledger.get_execution
        queue = types.ModuleType("cron.delivery_queue")
        queue.get_status = lambda eid: {"status": queue_status}
        with patch.dict(sys.modules, {"cron.executions": ledger, "cron.delivery_queue": queue}), \
                patch("hermes_muse.host_v1.profile_scope", lambda home: self.contextlib.nullcontext()):
            HermesHost(case.store).reconcile()

    def test_notice_flows_to_sent_for_all_and_platform_destinations(self):
        for destination, outcome, queue_status, expected in (
                ("all", "delivered", None, "sent"), ("telegram", "delivered", None, "sent"),
                ("all", "queued", "pending", "transport_queued"), ("telegram", "queued", "delivered", "sent")):
            with self.subTest(destination=destination, outcome=outcome):
                case = self.case
                case.host.destination = {"deliver": destination}
                row = case.notice(f"{destination}-{outcome}-{queue_status}", priority="urgent")
                text = case.dispatch(row["id"])
                self.assertIn(row["message"], text)
                self.assertNotIn("[Hermes Muse internal delivery]", text)  # raw text: no Bot Chat envelope
                sent = case.store.read("notification", row["id"])
                self.assertEqual(sent["route"], {"deliver": destination})
                self.assertEqual(sent["status"], "dispatching")
                self.reconcile(outcome, queue_status)
                self.assertEqual(case.store.read("notification", row["id"])["status"], expected)

    def test_unresolved_all_target_stays_unknown_and_is_not_replayed(self):
        case = self.case
        case.host.destination = {"deliver": "all"}
        row = case.notice(priority="urgent")
        case.dispatch(row["id"])
        self.reconcile("failed")  # /sethome removed before the fire: Hermes finds no target
        self.assertEqual(case.store.read("notification", row["id"])["status"], "unknown")
        case.service.notification_queue({"id": row["id"]})
        self.assertEqual(len(case.host.jobs), 0)
