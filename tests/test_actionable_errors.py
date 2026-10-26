"""Every argument failure names the field, expected shape, received value and the repair; no interpreter text leaks."""
import contextlib
import json
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hermes_muse.calendar import window
from hermes_muse.content_io import DomainError
from hermes_muse.contracts import FIELDS, validate
from hermes_muse.guidance import REQUIRED, field_kind
from hermes_muse.runtime import Runtime
from hermes_muse.service import Companion
from test_companion import Host, prepare

LEAK = re.compile(r"has no attribute|object is not|NoneType|not subscriptable|unhashable|positional argument|KeyError|TypeError|AttributeError"
                  r"|Traceback|invalid literal|string indices|must be str, not|argument of type|No module named|cannot import|object has no")
WRONG = {"str": 5, "int": "x", "bool": "x", "list": "x", "object": "x", "text": 5, "time": "not-a-time"}
SOURCE = {"connection": "mail-cli", "account": "default", "resource": "mail:inbox"}


class RuntimeHost(Host):
    """Native Cron/profile surfaces are absent in the stdlib-only unit job."""
    def reconcile(self):
        pass

    def manifest(self):
        return {"jobs": {}}

    def current_delivery(self, task_id):
        return None

    def is_active_watch(self, *args):
        return False

    def watch(self, data):
        raise ValueError("Native Cron is not available in this test")

    stop_watch = watch


class Harness(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("America/New_York"))
        store = prepare(temp.name)
        host = Host()
        host.local_now = lambda timestamp: datetime.fromtimestamp(timestamp, self.now.tzinfo)
        self.service = Companion(store, host, lambda: self.now.timestamp())
        self.runtime = Runtime(None, temp.name)
        self.addCleanup(self.runtime.close)
        self.runtime.service = self.service
        self.runtime.host = RuntimeHost()
        child = types.ModuleType("agent.delegation_context")
        child.is_delegated_child_context = lambda: False
        life = types.ModuleType("agent.subagent_lifecycle")
        life.get_active_subagent_parent = lambda: None
        life.__getattr__ = lambda name: object  # PEP 562: any lifecycle class the lazy imports ask for
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(temp.name)
        nothing = lambda home: contextlib.nullcontext()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        # Only the stubbed names are swapped: patch.dict would also drop lazily imported hermes_muse modules and detach later patches.
        for name, module in {"agent.delegation_context": child, "agent.subagent_lifecycle": life, "hermes_constants": constants}.items():
            self.stub(name, module)
        stack.enter_context(patch("hermes_muse.runtime.profile_scope", side_effect=nothing))
        stack.enter_context(patch("hermes_muse.mutations.profile_scope", side_effect=nothing))
        stack.enter_context(patch("hermes_muse.runtime.session_info", return_value={"chat_type": "private", "cron": False}))
        stack.enter_context(patch("logging.Logger.warning"))

    def stub(self, name, module):
        previous = sys.modules.get(name)
        sys.modules[name] = module
        self.addCleanup(lambda: sys.modules.pop(name, None) if previous is None else sys.modules.__setitem__(name, previous))

    def call(self, action, data=None, **top):
        args = {"action": action, **top}
        if data is not None:
            args["data"] = data
        return json.loads(self.runtime.handle(args, session_id="session-a"))

    def failure(self, action, data=None, **top):
        reply = self.call(action, data, **top)
        self.assertFalse(reply["ok"], reply)
        self.assertNotRegex(reply["error"], LEAK)
        return reply

    def check_source(self, **extra):
        return {"source": SOURCE, "scope": "unread mail", "started_at": self.now.isoformat(), "status": "checked", "summary": "ok",
                "expected_resources": ["mail:inbox"], "checked_resources": ["mail:inbox"], **extra}


class ReportedFailures(Harness):
    """The real proactive-watch failures, one by one."""

    def test_query_window_text_on_a_mail_source(self):
        reply = self.failure("source_check", self.check_source(query_window="last 24h", request_id="r", expected_revision="absent"))
        self.assertEqual((reply["code"], reply["retryable"], reply["field"]), ("invalid_argument", False, "data.query_window"))
        for phrase in ("data.query_window", "data.scope", "delete data.query_window", "Got text 'last 24h'", "fail again"):
            self.assertIn(phrase, reply["error"])
        self.assertIn("example", reply)

    def test_query_window_text_on_a_calendar_source(self):
        source = {**SOURCE, "resource": "calendars:rolling-30d"}
        reply = self.failure("source_check", self.check_source(source=source, query_window=json.dumps({"mode": "rolling"}), observed_at="2026-10-12T12:00:00-04:00",
                                                               request_id="r", expected_revision="absent"))
        self.assertIn("JSON object (not JSON text)", reply["error"])
        self.assertIn("calendar_window", reply["error"])

    def test_account_and_connection_beside_source(self):
        reply = self.failure("source_check", {"account": "default", "connection": "mail-cli", "scope": "s"})
        self.assertIn("move data.account, data.connection into data.source", reply["error"])
        self.assertIn('"resource"', reply["error"])
        self.assertIn("alias_reason", reply["error"])  # the allowed list
        self.assertEqual(reply["fields"], ["data.account", "data.connection"])

    def test_calendar_window_missing_or_incomplete(self):
        missing = self.failure("calendar_view", {})
        self.assertIn("data.window", missing["error"])
        self.assertIn("data.sources", missing["error"])
        short = self.failure("calendar_view", {"window": {"mode": "rolling"}, "sources": []})
        self.assertIn("data.window is missing keys timezone, observed_at, start, end, source_resource", short["error"])
        self.assertIn("calendar_window", short["error"])
        self.assertNotEqual(short["error"], "'window'")

    def test_calendar_window_edited_text_and_unchanged_rule(self):
        window = self.call("calendar_window")["result"]
        edited = self.failure("calendar_view", {"window": {**window, "end": "2026-10-13T12:00:00-04:00"}, "sources": []})
        self.assertIn("differs from the calendar_window result in: end", edited["error"])
        self.assertIn("unchanged", edited["error"])
        encoded = self.failure("calendar_view", {"window": json.dumps(window), "sources": []})
        self.assertIn("data.window has the wrong type", encoded["error"])
        self.assertIn("not JSON-encoded text", encoded["error"])

    def test_calendar_source_entries_are_checked_with_indexed_paths(self):
        window = self.call("calendar_window")["result"]
        text = self.failure("calendar_view", {"window": window, "sources": ["calendar"]})
        self.assertIn("data.sources[0] must be an object", text["error"])
        no_inventory = self.failure("calendar_view", {"window": window, "sources": [{"source": {**SOURCE, "resource": window["source_resource"]}, "title": "t"}]})
        self.assertIn("data.sources[0].inventory", no_inventory["error"])
        wrong_resource = self.failure("calendar_view", {"window": window, "sources": [{"source": SOURCE, "title": "t", "inventory": {"pages": []}}]})
        self.assertIn(window["source_resource"], wrong_resource["error"])

    def test_delegate_task_field(self):
        reply = self.failure("delegate", {"task": "summarise"})
        self.assertIn("data.task", reply["error"])
        self.assertIn("data.goal", reply["error"])
        self.assertIn("data.tasks", reply["error"])
        self.assertIn("goal_id", reply["error"])

    def test_revision_conflict_says_how_to_recover(self):
        data = self.check_source(request_id="first", expected_revision="absent")
        first = self.call("source_check", data)
        self.assertTrue(first["ok"], first)
        stale = self.failure("source_check", {**data, "request_id": "second"})
        self.assertEqual((stale["code"], stale["retryable"]), ("revision_conflict", True))
        for phrase in ("data.expected_revision 'absent' is stale", "context(section=source_check)", "NEW data.request_id", stale["current_revision"]):
            self.assertIn(phrase, stale["error"])
        missing = self.failure("source_check", {k: v for k, v in {**data, "request_id": "third"}.items() if k != "expected_revision"})
        self.assertEqual((missing["code"], missing["retryable"]), ("revision_required", True))
        self.assertIn("data.expected_revision=", missing["error"])
        number = self.failure("source_check", {**data, "request_id": "fourth", "expected_revision": 3})
        self.assertIn("not a number", number["error"])

    def test_source_id_and_window_service_errors_name_their_field(self):
        reply = self.failure("source_check", self.check_source(source_id="source-other", request_id="r", expected_revision="absent"))
        self.assertIn("data.source_id must identify the same account and resource", reply["error"])
        self.assertIn("delete data.source_id", reply["error"])
        window = self.service.calendar_window({})
        with self.assertRaises(DomainError) as caught:
            self.service.calendar_view({"window": {**window, "start": "2026-10-01T00:00:00-04:00"}, "sources": []})
        self.assertIn("unchanged", str(caught.exception))
        self.assertEqual(caught.exception.details["field"], "data.window")

    def test_no_request_id_names_the_field(self):
        reply = self.failure("source_check", self.check_source())
        self.assertIn("request_id required for source_check (data.request_id)", reply["error"])
        self.assertEqual(reply["field"], "data.request_id")


class RequestIdPerRun(Harness):
    """Reported 2026-10-23: a patrol keyed source_check by task-date-source; its second run of the day collided with the first."""
    def first_run(self):
        data = self.check_source(request_id="proactive-watch-20261023-mail", expected_revision="absent")
        self.assertTrue(self.call("source_check", data)["ok"])

    def test_second_run_of_the_day_with_a_date_key_conflicts_but_names_a_usable_key(self):
        self.first_run()
        reply = self.failure("source_check", self.check_source(request_id="proactive-watch-20261023-mail", expected_revision="absent",
                                                              summary="second run, different result"))
        self.assertEqual((reply["code"], reply["retryable"]), ("idempotency_conflict", False))
        suggestion = reply["suggested_request_id"]
        self.assertRegex(suggestion, r"^proactive-watch-20261023-mail-\d{8}T\d{6}[0-9a-f]{4}$")
        for phrase in (repr(suggestion), "unique per run", "same day"):
            self.assertIn(phrase, reply["error"])
        self.assertLessEqual(len(suggestion), 128)

    def test_suggested_key_is_accepted_after_the_revision_is_refreshed(self):
        self.first_run()
        args = self.check_source(request_id="proactive-watch-20261023-mail", expected_revision="absent", summary="second run")
        suggestion = self.failure("source_check", args)["suggested_request_id"]
        # The key is the only thing the conflict blames; the model then follows the stale-revision advice.
        stale = self.failure("source_check", {**args, "request_id": suggestion})
        self.assertEqual(stale["code"], "revision_conflict")
        self.assertIn("unique to this run", stale["error"])
        done = self.call("source_check", {**args, "request_id": suggestion, "expected_revision": stale["current_revision"]})
        self.assertTrue(done["ok"], done)

    def test_long_keys_still_fit_the_suggestion(self):
        key = "k" * 128
        args = self.check_source(request_id=key, expected_revision="absent")
        self.assertTrue(self.call("source_check", args)["ok"])
        reply = self.failure("source_check", {**args, "summary": "changed"})
        self.assertLessEqual(len(reply["suggested_request_id"]), 128)

    def test_missing_request_id_does_not_teach_a_date_only_key(self):
        reply = self.failure("source_check", self.check_source())
        self.assertNotRegex(reply["error"], r"-20\d{6}-[a-z]")
        self.assertIn("start time to the minute", reply["error"])
        self.assertIn("never reuse a key from an earlier run", reply["error"])

    def test_documentation_and_examples_use_run_unique_keys(self):
        root = Path(__file__).resolve().parents[1]
        date_key = re.compile(r"request_id[^\n]{0,40}?[\"'`][\w-]*-20\d{6}-\w", re.I)
        for path in [*(root / "skills").rglob("*.md"), *(root / "prompts").glob("*.md"), root / "README.md", root / "docs/INTERFACE.md"]:
            self.assertIsNone(date_key.search(path.read_text(encoding="utf-8")), path)
        for example in (json.dumps(value) for value in __import__("hermes_muse.guidance", fromlist=["EXAMPLES"]).EXAMPLES.values()):
            self.assertNotIn("<new unique key>", example)
        context = (root / "skills/companion/references/context.md").read_text(encoding="utf-8")
        self.assertIn("unique to every run", context)

    def test_calendar_view_text_entry_hint_points_at_the_json_mistake(self):
        window = self.service.calendar_window({})
        reply = self.failure("calendar_view", {"window": window, "discovery_complete": True, "sources": ["stray text, no such field"]})
        self.assertEqual(reply["field"], "data.sources[0]")
        self.assertIn("unbalanced brace", reply["error"])
        self.assertIn("make each entry an object", reply["hint"])


class ProactiveWatchBatch(Harness):
    """Reported 2026-10-23 13:00: one parallel batch of source_checks hit five different mistakes and tripped the tool-loop guard."""
    CALENDAR = {"connection": "calendar-cli", "account": "default", "resource": "calendars:rolling-30d"}

    def fixed_window(self):
        from datetime import timedelta, timezone
        return window(datetime(2026, 10, 12, 23, 0, tzinfo=timezone(timedelta(hours=8), "CST")), "rolling")

    def calendar_check(self, **extra):
        data = self.check_source(source=self.CALENDAR, expected_resources=["cal-1"], checked_resources=["cal-1"], query_window=self.fixed_window(),
                                 request_id="cal-1305", expected_revision="absent")
        data.pop("started_at")
        return {**data, **extra}

    def test_account_and_connection_repeated_beside_an_identical_source_are_ignored(self):
        data = self.check_source(request_id="dup-1", expected_revision="absent", account=SOURCE["account"], connection=SOURCE["connection"])
        reply = self.call("source_check", data)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["source"], SOURCE)

    def test_conflicting_or_orphaned_top_level_source_fields_say_what_to_delete(self):
        clash = self.failure("source_check", self.check_source(request_id="dup-2", expected_revision="absent", account="other"))
        self.assertIn("delete data.account: data.source already holds them", clash["error"])
        self.assertIn("they differ from data.source for account", clash["error"])
        orphan = self.failure("source_check", {k: v for k, v in self.check_source(request_id="dup-3", expected_revision="absent", account="x").items() if k != "source"})
        self.assertIn("data.source", orphan["error"])

    def test_calendar_check_needs_no_timestamps_and_ignores_a_retyped_timezone_note(self):
        window_ = {**self.fixed_window(), "timezone_note": "DST unavailable"}
        data = {**self.calendar_check(), "query_window": window_}
        reply = self.call("source_check", data)
        self.assertTrue(reply["ok"], reply)
        saved = reply["result"]
        self.assertEqual(saved["query_window"], self.fixed_window())
        self.assertEqual(saved["started_at"], datetime.fromisoformat(window_["observed_at"]).timestamp())

    def test_calendar_check_still_rejects_a_changed_window_and_mismatched_times(self):
        changed = self.failure("source_check", {**self.calendar_check(), "query_window": {**self.fixed_window(), "end": "2026-12-01T00:00:00+08:00"}})
        self.assertIn("differs from the calendar_window result in: end", changed["error"])
        late = self.failure("source_check", self.calendar_check(started_at=self.now.isoformat()))
        self.assertIn("delete data.started_at and data.observed_at", late["error"])

    def test_a_rejected_source_check_does_not_burn_its_request_id(self):
        bad = {**self.calendar_check(), "query_window": {**self.fixed_window(), "end": "2026-12-01T00:00:00+08:00"}}
        self.assertEqual(self.failure("source_check", bad)["code"], "invalid_argument")
        again = self.call("source_check", self.calendar_check())
        self.assertTrue(again["ok"], again)
        replay = self.call("source_check", self.calendar_check())
        self.assertTrue(replay["ok"], replay)
        conflict = self.failure("source_check", {**self.calendar_check(), "summary": "different"})
        self.assertEqual(conflict["code"], "idempotency_conflict")

    def test_truncated_revision_returns_the_current_one(self):
        self.assertTrue(self.call("source_check", self.check_source(request_id="rev-1", expected_revision="absent"))["ok"])
        actual = self.failure("source_check", self.check_source(request_id="rev-2", expected_revision="absent"))["current_revision"]
        broken = self.failure("source_check", self.check_source(request_id="rev-3", expected_revision=actual[:20] + "?"))
        self.assertEqual(broken["code"], "revision_conflict")
        self.assertIn("truncated or retyped", broken["error"])
        self.assertIn(f"data.expected_revision={actual!r}", broken["error"])
        self.assertEqual(broken["current_revision"], actual)

    def test_plural_content_kind_and_missing_object_are_actionable(self):
        kind = self.failure("content_read", {"kind": "ideas", "id": "current"})
        self.assertIn("Did you mean 'idea'?", kind["error"])
        missing = self.failure("content_read", {"kind": "idea", "id": "current"})
        self.assertEqual((missing["code"], missing["field"]), ("not_found", "data.id"))
        self.assertIn("context(section=idea)", missing["error"])
        self.assertNotIn("Object unavailable", missing["error"])


class Boundary(Harness):
    def test_top_level_arguments_and_unknown_action(self):
        moved = self.failure("context", None, section="notice")
        self.assertIn("move section inside `data`", moved["error"])
        near = self.failure("source_chek", {})
        self.assertIn("source_check", near["error"])
        encoded = self.failure("context", '{"section": "notice"}')
        self.assertIn("data must be an object", encoded["error"])
        self.assertIn("never JSON-encode", encoded["error"])

    def test_misplaced_fields_get_their_destination(self):
        self.assertIn("rename to data.expected_revision", self.failure("preferences", {"revision": "x", "signal_id": "s", "request_id": "r"})["error"])
        self.assertIn("is not accepted by goal_create", self.failure("goal_create", {"request_id": "r"})["error"])
        self.assertIn("did you mean source_check_ids", self.failure("review_complete", {"source_check_id": "x"})["error"])
        self.assertIn("data.section", self.failure("context", {"kind": "notice"})["error"])

    def test_unexpected_interpreter_exceptions_are_converted(self):
        cases = (
            (KeyError("window"), "calendar_view", {}, "data.window"),
            (KeyError("started_at"), "source_check", {}, "data.started_at"),
            (KeyError("mystery"), "source_check", {}, "mystery"),
            (AttributeError("'str' object has no attribute 'get'"), "source_check", {}, "wrong type"),
            (TypeError("string indices must be integers"), "goal_update", {}, "goal_update"),
            (IndexError("list index out of range"), "feed_add", {}, "feed_add"),
            (ZoneInfoNotFoundError("No time zone found with key Mars/Base"), "calendar_view", {}, "IANA timezone"),
        )
        for exc, action, data, expected in cases:
            with self.subTest(exc=repr(exc)):
                reply = json.loads(Runtime._error(exc, action, data))
                self.assertEqual((reply["ok"], reply["code"], reply["retryable"]), (False, "invalid_argument", False))
                self.assertIn(expected, reply["error"])
                self.assertIn(action, reply["error"])
                self.assertIn("fail again", reply["error"])
                self.assertNotRegex(reply["error"], LEAK)

    def test_text_value_errors_name_the_field(self):
        blank = json.loads(Runtime._error(ValueError("Expected nonempty text, at most 200 characters"), "goal_create", {"title": " "}))
        self.assertIn("data.title must be non-empty text", blank["error"])
        ident = json.loads(Runtime._error(ValueError("ID must be 1–80 letters, numbers, hyphens or underscores"), "goal_update", {"id": "a b"}))
        self.assertIn("data.id", ident["error"])
        stamp = json.loads(Runtime._error(ValueError("Timestamps must include a timezone"), "notification_add", {}))
        self.assertIn("ISO-8601", stamp["error"])
        other = json.loads(Runtime._error(ValueError("Goal already exists; use goal_update"), "goal_create", {}))
        self.assertIn("Goal already exists; use goal_update", other["error"])
        self.assertIn("fail again", other["error"])

    def test_retryable_is_accurate_and_native_result_survives(self):
        for code, retry in (("revision_conflict", True), ("cursor_expired", True), ("invalid_argument", False), ("permission_denied", False),
                            ("size_limit", False), ("outcome_unknown", False)):
            reply = json.loads(Runtime._error(DomainError(code, "Something specific"), "context", {}))
            self.assertEqual(reply["retryable"], retry, code)
            if code in {"invalid_argument", "permission_denied", "size_limit"}:
                self.assertIn("fail again", reply["error"])
        self.assertNotIn("fail again", json.loads(Runtime._error(DomainError("outcome_unknown", "Inspect first"), "context", {}))["error"])
        error = ValueError("Native delegation returned an error")
        error.native_result = {"error": "boom"}
        self.assertEqual(json.loads(Runtime._error(error, "delegate", {}))["native"], {"error": "boom"})

    def test_sizes_and_ranges_name_limits(self):
        long = self.failure("goal_create", {"title": "x" * 201, "description": "d", "completion_criteria": "c"})
        self.assertEqual(long["code"], "size_limit")
        self.assertIn("data.title is too long", long["error"])
        self.assertIn("at most 200 characters", long["error"])
        self.assertIn("201 characters", long["error"])
        many = self.failure("research_start", {"items": ["q"] * 25})
        self.assertIn("data.items", many["error"])
        bad_item = self.failure("research_start", {"items": [{"q": 1}]})
        self.assertIn("data.items[0]", bad_item["error"])
        hour = self.failure("preferences", {"start_hour": 99, "signal_id": "s", "request_id": "r", "expected_revision": "absent"})
        self.assertNotRegex(hour["error"], LEAK)

    def test_every_enum_names_its_values(self):
        reply = self.failure("run_complete", {"outcome": "done", "summary": "s"})
        self.assertIn("one of complete, partial", reply["error"])
        reply = self.failure("source_check", self.check_source(status="ok", request_id="r", expected_revision="absent"))
        self.assertIn("checked, partial, error, unavailable", reply["error"])
        reply = self.failure("feedback", {"kind": "notification", "id": "n", "action": "close", "signal_id": "s", "request_id": "r", "expected_revision": "x"})
        self.assertIn("dismiss", reply["error"])

    def test_timestamps_and_ids(self):
        reply = self.failure("notification_add", {"event_key": "k", "message": "m", "rationale": "r", "sources": ["s"], "verified_at": "yesterday",
                                                  "expires": 1, "request_id": "r", "expected_revision": "absent"})
        self.assertIn("data.verified_at", reply["error"])
        self.assertIn("UTC offset", reply["error"])
        reply = self.failure("goal_update", {"id": "bad id!"})
        self.assertIn("data.id is not a valid record id", reply["error"])


class AllActions(Harness):
    def invalid(self, action, data):
        with self.assertRaises(DomainError) as caught:
            validate(action, data)
        error = caught.exception
        self.assertNotRegex(str(error), LEAK)
        self.assertIn(action, str(error))
        self.assertIn("Fix:", str(error))
        self.assertIn("field", error.details)
        return error

    def test_unsupported_field_on_every_action(self):
        for action in FIELDS:
            with self.subTest(action=action):
                error = self.invalid(action, {"bogus_field": 1})
                self.assertIn("data.bogus_field", str(error))
                self.assertIn("fail again", str(error))
                if FIELDS[action]:
                    self.assertIn(FIELDS[action].split()[0], error.details["expected"])
                self.assertEqual(error.code, "invalid_argument")
                self.assertIs(error.details["retryable"], False)

    def test_wrong_type_for_every_typed_field(self):
        for action, fields in FIELDS.items():
            for name in fields.split():
                kind = field_kind(action, name)
                if kind is None or name == "query_window":
                    continue
                with self.subTest(action=action, field=name):
                    error = self.invalid(action, {name: WRONG[kind]})
                    self.assertIn("data." + name, str(error))
                    self.assertIn("Expected", str(error))
                    self.assertIn("Got", str(error))

    def test_missing_required_fields_are_all_named_at_once(self):
        for action, required in REQUIRED.items():
            with self.subTest(action=action):
                error = self.invalid(action, {})
                self.assertEqual(error.code, "invalid_argument")
                names = [n for n in required if not (action == "source_check" and n == "source")] if action != "source_check" else ["source"]
                for name in names:
                    self.assertIn("data." + name, str(error))

    def test_previously_valid_shapes_still_pass(self):
        for action, data in (
            ("notification_review", {"id": "n", "conversation_revisions": ["ignored"], "request_id": "r", "expected_revision": "x"}),
            ("review_complete", {"job": "watch-abc123", "summary": "s", "goal_id": "g", "stop_condition": "c", "source_check_ids": ["s"]}),
            ("content_write", {"kind": "note", "id": "people", "body": "", "expected_revision": "absent", "request_id": "r"}),
            ("goal_create", {"title": "t", "description": "d", "completion_criteria": "c", "deadline": None, "signal_id": "s"}),
            ("goal_update", {"id": "g", "expected_revision": 3, "status": "done", "note": "told the user", "waiting_user": False}),
            ("notification_prepare", {"id": "abc123"}),
            ("feedback", {"kind": "interest", "id": "i", "action": "snooze", "until": 1800000000, "signal_id": "s", "request_id": "r", "expected_revision": "x"}),
            ("context", {}), ("status", {"section": "notice", "limit": 25}), ("calendar_window", {"mode": "month"}),
            ("forget", {"kind": "content", "object_kind": "note", "id": "x", "expected_revision": "r", "request_id": "q", "signal_id": "s"}),
        ):
            with self.subTest(action=action):
                validate(action, data)

    def test_data_must_be_an_object_for_every_action(self):
        for action in FIELDS:
            with self.subTest(action=action):
                self.assertIn("data must be an object", str(self.invalid(action, "{}")))

    def test_garbage_values_never_leak_interpreter_text(self):
        values = ("x", 7, [], {}, None, True, ["x"], {"a": 1})
        for action, fields in FIELDS.items():
            for name in ["_none_"] + fields.split():
                for value in values:
                    data = {} if name == "_none_" else {name: value}
                    with self.subTest(action=action, field=name, value=value):
                        reply = self.call(action, data)
                        self.assertIsInstance(reply["ok"], bool)
                        if not reply["ok"]:
                            self.assertNotRegex(reply["error"], LEAK)
                            self.assertIn("retryable", reply)

    def test_nested_garbage_in_calendar_and_source_check(self):
        window = self.call("calendar_window")["result"]
        resource = window["source_resource"]
        good = {"source": {**SOURCE, "resource": resource}, "title": "t", "inventory": {"pages": [{"status": "checked", "observed_at": window["observed_at"], "items": []}]}}
        for sources in ([{**good, "inventory": "x"}], [{**good, "calendars": "x"}], [{**good, "calendars": ["x"]}], [{**good, "inventory": {"pages": "x"}}],
                        [{**good, "inventory": {"pages": ["x"]}}], [{**good, "inventory": {"pages": [{"items": 5, "observed_at": 5}]}}],
                        [{**good, "source": {"connection": 1, "account": 2, "resource": resource}}], [{**good, "source": "x"}], [None], [[]]):
            with self.subTest(sources=json.dumps(sources)[:60]):
                reply = self.call("calendar_view", {"window": window, "sources": sources})
                text = reply["error"] if not reply["ok"] else json.dumps(reply["result"]["gaps"])
                self.assertNotRegex(text, LEAK)
        for number, query in enumerate(("x", 5, [], {}, {"mode": "rolling"}, {**window, "timezone": "Mars/Base"}, {**window, "observed_at": "nope"})):
            with self.subTest(query=json.dumps(query)[:40]):
                data = self.check_source(source={**SOURCE, "resource": resource}, query_window=query, observed_at=window["observed_at"], request_id=f"q{number}", expected_revision="absent")
                reply = self.call("source_check", data)
                self.assertFalse(reply["ok"])
                self.assertNotRegex(reply["error"], LEAK)
                self.assertIn("data.query_window", reply["error"])


if __name__ == "__main__":
    unittest.main()


class NightlyIdeasStep(Harness):
    """Reported 2026-10-23: four nights in a row never reached the Ideas clause buried in the research paragraph."""
    ROOT = Path(__file__).resolve().parents[1]

    def paragraphs(self):
        return (self.ROOT / "prompts/nightly-review.md").read_text(encoding="utf-8").split("\n\n")

    def test_ideas_are_their_own_step_between_reflection_and_closing(self):
        paragraphs = self.paragraphs()
        step = next(i for i, p in enumerate(paragraphs) if p.startswith("Ideas step"))
        research = next(i for i, p in enumerate(paragraphs) if p.startswith("Research eligible active goals"))
        reflection = next(i for i, p in enumerate(paragraphs) if "Write confirmed reflection and alignment before optional follow-up research" in p)
        closing = next(i for i, p in enumerate(paragraphs) if p.startswith("Complete recoverable work"))
        self.assertTrue(research < reflection < step < closing)
        self.assertNotIn("Ideas", paragraphs[research])
        text = paragraphs[step]
        for needle in ("never ahead of them", "0-3", "no quota", "title, rationale, sources", "goal_id", "expires", "not authorization"):
            self.assertIn(needle, text)

    def test_run_complete_summary_records_the_ideas_outcome(self):
        closing = next(p for p in self.paragraphs() if p.startswith("Complete recoverable work"))
        self.assertIn("one sentence on Ideas", closing)
        self.assertIn("why none", closing)
        self.assertIn("still with the Ideas sentence", closing)

    def test_idea_add_wrong_fields_get_a_one_shot_fix(self):
        reply = self.failure("idea_add", {"text": "Try X", "kind": "ideas"})
        self.assertEqual(reply["fields"], ["data.kind", "data.text"])
        self.assertIn("data.title", reply["hint"])
        self.assertIn("data.rationale", reply["hint"])
        self.assertIn("takes no kind", reply["hint"])
        self.assertEqual(reply["example"]["data"]["title"], "<short headline>")
        self.assertIn("rename it to data.rationale", self.failure("idea_add", {"title": "t", "reason": "r"})["hint"])


class StartedAtDefault(Harness):
    """Reported 2026-10-24: weaker models sent source_check without data.started_at and repeated the rejection until the run was halted."""
    RUN_START = datetime(2026, 10, 12, 11, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()

    def minimal(self, request_id="sa-1", revision="absent", **extra):
        data = self.check_source(request_id=request_id, expected_revision=revision, **extra)
        data.pop("started_at", None)
        return {**data, **{k: v for k, v in extra.items() if k == "started_at"}}

    def in_cron_run(self, data, start=None, execution="exec-1"):
        run = {"id": execution, "job_id": "job-1", "job": "proactive-watch", "started": start or self.RUN_START, "status": "running", "stages": {}}
        with patch.object(self.service.runs, "bind", return_value=run), patch.object(self.service.operations, "track"):
            return json.loads(self.runtime.handle({"action": "source_check", "data": data}, session_id="session-a", task_id=f"cron:job-1:{execution}"))

    def test_missing_started_at_defaults_to_the_call_time(self):
        reply = self.call("source_check", self.minimal())
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["started_at"], self.now.timestamp())
        self.assertIn("time of this call", reply["result"]["started_at_defaulted"])
        self.assertNotIn("started_at_defaulted", self.service.store.read("source_check", reply["result"]["id"]))

    def test_missing_started_at_defaults_to_the_cron_run_start(self):
        reply = self.in_cron_run(self.minimal())
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["started_at"], self.RUN_START)
        self.assertIn("start time of this Cron run", reply["result"]["started_at_defaulted"])

    def test_an_explicit_started_at_wins_and_is_not_flagged(self):
        explicit = self.RUN_START + 60
        reply = self.in_cron_run(self.minimal(started_at=explicit))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["started_at"], explicit)
        self.assertNotIn("started_at_defaulted", reply["result"])

    def test_a_future_started_at_is_still_rejected(self):
        reply = self.failure("source_check", self.minimal(started_at=self.now.timestamp() + 3600))
        self.assertEqual(reply["field"], "data.started_at")
        self.assertIn("past or current", reply["error"])

    def test_an_earlier_started_at_keeps_the_stored_check_and_an_equal_one_is_accepted(self):
        first = self.call("source_check", self.minimal(started_at=self.now.timestamp()))
        earlier = self.call("source_check", self.minimal("sa-2", first["revision"], started_at=self.now.timestamp() - 600, summary="older"))
        self.assertTrue(earlier["ok"], earlier)
        self.assertEqual((earlier["result"]["started_at"], earlier["result"]["summary"]), (self.now.timestamp(), "ok"))
        equal = self.call("source_check", self.minimal("sa-3", first["revision"], started_at=self.now.timestamp(), summary="same instant"))
        self.assertTrue(equal["ok"], equal)
        self.assertEqual(equal["result"]["summary"], "same instant")

    def test_a_second_defaulted_check_in_the_same_run_is_accepted(self):
        first = self.in_cron_run(self.minimal())
        again = self.in_cron_run(self.minimal("sa-2", first["revision"], summary="second pass"))
        self.assertTrue(again["ok"], again)
        self.assertEqual((again["result"]["started_at"], again["result"]["summary"]), (self.RUN_START, "second pass"))

    def test_the_next_run_defaults_to_its_own_later_start(self):
        first = self.in_cron_run(self.minimal())
        self.service.clock = lambda: self.now.timestamp() + 1800
        later = self.in_cron_run(self.minimal("sa-2", first["revision"], summary="next run"), start=self.RUN_START + 1800, execution="exec-2")
        self.assertEqual((later["result"]["started_at"], later["result"]["summary"]), (self.RUN_START + 1800, "next run"))

    def test_replaying_a_defaulted_call_later_is_a_replay_not_a_conflict(self):
        first = self.call("source_check", self.minimal())
        self.service.clock = lambda: self.now.timestamp() + 90
        replay = self.call("source_check", self.minimal())
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["result"]["started_at"], first["result"]["started_at"])


class StrayTopLevelArguments(Harness):
    """Reported 2026-10-24: `section`, `body`, `kind`, `id`... were sent beside `action`; the host rejected them with a bare additionalProperties error."""

    def test_the_tool_schema_leaves_the_message_to_the_handler(self):
        from hermes_muse.runtime import SCHEMA
        self.assertNotIn("additionalProperties", SCHEMA["parameters"])

    def test_section_beside_context_is_moved_into_data(self):
        reply = self.failure("context", None, section="notice", limit=25)
        self.assertEqual((reply["code"], reply["retryable"]), ("invalid_argument", False))
        self.assertIn('move limit, section inside `data`, e.g. {"action": "context", "data": {"limit": 25, "section": "notice"}}', reply["error"])
        self.assertIn("fail again", reply["error"])

    def test_content_write_fields_beside_action_are_moved_into_data(self):
        reply = self.failure("content_write", {"kind": "goal"}, id="g1", body="text", request_id="r1", expected_revision="absent")
        for name in ("body", "expected_revision", "id", "request_id"):
            self.assertIn(name, reply["field"])
        self.assertIn('"kind": "goal"', reply["error"])
        self.assertIn('"body": "text"', reply["error"])

    def test_an_unknown_action_with_stray_fields_names_the_valid_ones(self):
        reply = self.failure("search", None, section="session_search")
        self.assertIn("is not a muse_manage action", reply["error"])
        self.assertIn("put every argument inside `data`", reply["error"])


class GoalIntake(Harness):
    """0.3.13: per-turn accepted-work check and the nightly goal_create cap."""
    ROOT = Path(__file__).resolve().parents[1]

    def goal(self, key, **extra):
        return {"id": key, "title": "Title " + key, "description": "Basis paraphrased", "completion_criteria": "Done when shown", **extra}

    def run_row(self):
        with self.service.store.transaction() as db:
            self.service.store.put(db, "run", "night", {"id": "night", "job": "nightly-review", "started": 0, "status": "running", "stages": {}})
        return {"id": "night"}

    def test_user_turn_hint_carries_the_check_and_recent_titles(self):
        hint = self.runtime.pre_turn(session_id="s", turn_id="t1", user_message="Please book the venue")["context"]
        self.assertIn("If you accepted a concrete request or promise in this turn, make sure an active goal covers it (context section=goal); create one if not.", hint)
        self.assertIn("Casual questions and passing mentions do not become goals.", hint)
        self.assertNotIn("created in the last 24h", hint)
        self.service.goal_create(self.goal("venue", signal_id="t1"), session_id="s")
        self.assertIn("Goals created in the last 24h: Title venue.", self.runtime.pre_turn(session_id="s", turn_id="t2", user_message="Next request")["context"])
        self.now = self.now.replace(day=self.now.day + 2)
        self.assertNotIn("created in the last 24h", self.runtime.pre_turn(session_id="s", turn_id="t3", user_message="Later request")["context"])

    def test_nightly_prompt_has_its_own_intake_step_before_ideas(self):
        paragraphs = (self.ROOT / "prompts/nightly-review.md").read_text(encoding="utf-8").split("\n\n")
        step = next(i for i, p in enumerate(paragraphs) if p.startswith("Goal intake step"))
        ideas = next(i for i, p in enumerate(paragraphs) if p.startswith("Ideas step"))
        reflection = next(i for i, p in enumerate(paragraphs) if "Write confirmed reflection and alignment" in p)
        self.assertLess(reflection, step)
        self.assertLess(step, ideas)
        for needle in ("goal_create", "signal_id", "completion_criteria", "At most 5", "active, done or cancelled", "Never goal_update", "no copied private text"):
            self.assertIn(needle.lower(), paragraphs[step].lower())
        closing = next(p for p in paragraphs if p.startswith("Complete recoverable work"))
        self.assertIn("one sentence on Goal intake", closing)

    def test_nightly_create_needs_a_signal_and_names_the_fix(self):
        with self.assertRaises(DomainError) as caught:
            self.service.intake_goal_create(self.goal("a"), self.run_row())
        error = caught.exception
        self.assertEqual(error.details["field"], "data.signal_id")
        self.assertIn("context(section=signals)", error.details["expected"])
        self.assertIn("Fix:", str(error))
        self.assertNotRegex(str(error), LEAK)

    def test_nightly_cap_is_five_and_failed_creates_do_not_consume_it(self):
        self.service.signal("u", "chat", "I would like the report")
        run = self.run_row()
        with self.assertRaises(ValueError):
            self.service.intake_goal_create(self.goal("bad", signal_id="missing"), run)
        for index in range(5):
            self.assertEqual(self.service.intake_goal_create(self.goal(f"g{index}", signal_id="u"), run)["session_id"], "chat")
        with self.assertRaises(DomainError) as caught:
            self.service.intake_goal_create(self.goal("g5", signal_id="u"), run)
        details = caught.exception.details
        self.assertEqual((caught.exception.code, details["field"]), ("size_limit", "data"))
        self.assertIn("at most 5", details["expected"])
        self.assertIn("run_complete", details["hint"])
        self.assertNotRegex(str(caught.exception), LEAK)
        self.assertEqual(len(self.service.store.goals()), 5)
