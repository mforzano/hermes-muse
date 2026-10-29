import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_companion import prepare, Host
from hermes_muse.service import Companion
from hermes_muse.views import Views
from hermes_muse.content import Content
from hermes_muse.content_io import DomainError
from hermes_muse.memory_feedback import classify, MemoryFeedback


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.new("sha256", p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def process_writer(home, data, start, results):
    from hermes_muse.store import Store
    service = Companion(Store(home), Host(), lambda: 1800000000)
    start.wait(10)
    try:
        Content(service).write(data, session_id="session")
        results.put("written")
    except DomainError as exc:
        results.put(exc.code)


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = prepare(self.home)
        self.service = Companion(self.store, Host(), lambda: 1800000000)
        self.service.signal(
            "s", "session", "Please maintain goal g and tools_note current"
        )
        self.service.goal_create(
            {
                "id": "g",
                "title": "Goal",
                "description": "Research",
                "completion_criteria": "Evidence",
                "signal_id": "s",
            }
        )
        self.views = Views(self.home)
        self.content = Content(self.service)

    def write(self, **overrides):
        data = {
            "kind": "goal_file",
            "id": "g",
            "resource": "files/report.md",
            "body": "# Result\nUseful evidence",
            "expected_revision": "absent",
            "request_id": "write-1",
            "signal_id": "s",
            **overrides,
        }
        return self.content.write(data, session_id="session")

    def test_read_does_not_initialize_and_corruption_is_not_empty(self):
        missing = self.home / "missing"
        with self.assertRaises(DomainError) as caught:
            Views(missing).read({"kind": "feed"})
        self.assertEqual(caught.exception.code, "not_installed")
        self.assertFalse(missing.exists())
        before = hashes(self.home)
        self.views.read({"kind": "goal", "id": "g"})
        self.assertEqual(before, hashes(self.home))
        (self.home / "muse/state.db").write_bytes(b"broken")
        with self.assertRaises(DomainError) as caught:
            self.views.read({"kind": "feed"})
        self.assertEqual(caught.exception.code, "source_unavailable")

    def test_all_feed_pages_snapshot_and_cross_profile_scope(self):
        for n in range(123):
            self.service.feed_add(
                {
                    "event_key": str(n),
                    "title": str(n),
                    "content": "Body",
                    "sources": ["fixture:source"],
                    "why": "Useful",
                    "goal_id": "g",
                }
            )
        before = hashes(self.home)
        page = self.views.read({"kind": "feed"})
        ids = []
        while True:
            ids.extend(r["id"] for r in page["items"])
            if not page["next_cursor"]:
                break
            page = self.views.read({"kind": "feed", "cursor": page["next_cursor"]})
        self.assertEqual(len(set(ids)), 123)
        self.assertEqual(before, hashes(self.home))
        first = self.views.read({"kind": "feed"})
        self.service.feed_add(
            {
                "event_key": "new",
                "title": "new",
                "content": "Body",
                "sources": ["fixture:source"],
                "why": "Useful",
            }
        )
        with self.assertRaises(DomainError) as caught:
            self.views.read({"kind": "feed", "cursor": first["next_cursor"]})
        self.assertEqual(caught.exception.code, "cursor_expired")
        with self.assertRaises(DomainError) as caught:
            self.views.read({"kind": "goal", "id": "g", "home": "other"})
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_cas_idempotency_and_post_commit_crash(self):
        first = self.write()
        self.assertEqual(first, self.write())
        with self.assertRaises(DomainError) as caught:
            self.write(body="different")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        with self.assertRaises(DomainError) as caught:
            self.write(request_id="new")
        self.assertEqual(caught.exception.code, "revision_conflict")
        original = self.store.write

        def crash(kind, key, value):
            if kind == "content_request" and value.get("state") == "complete":
                raise RuntimeError("after rename")
            return original(kind, key, value)

        with patch.object(self.store, "write", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.write(
                    request_id="crash",
                    body="Second",
                    expected_revision=first["revision"],
                )
        recovered = self.write(
            request_id="crash", body="Second", expected_revision=first["revision"]
        )
        self.assertEqual(
            self.views.read(
                {"kind": "goal_file", "id": "g", "resource": "files/report.md"}
            )["body"],
            "Second",
        )
        self.assertEqual(
            recovered["revision"], hashlib.new("sha256", b"Second").hexdigest()
        )

    def test_untrusted_paths_metadata_limits_and_origin(self):
        for resource in (
            "../GOAL.md",
            "files/../GOAL.md",
            "files/%2e%2e.md",
            "/tmp/x.md",
            "hidden_files/x.md",
            "files/x/y.md",
        ):
            with self.assertRaises(DomainError):
                self.write(resource=resource)
        with self.assertRaises(DomainError):
            self.write(body="<!-- hermes-muse {} -->")
        with self.assertRaises(DomainError):
            self.write(body="x" * 60001)
        (self.home / "muse/workspace/goals/g/files/link.md").symlink_to(
            self.home / "muse/TOOLS.md"
        )
        with self.assertRaises(DomainError):
            self.views.read(
                {"kind": "goal_file", "id": "g", "resource": "files/link.md"}
            )
        (self.home / "muse/workspace/goals/g/files").rename(self.home / "safe-files")
        (self.home / "muse/workspace/goals/g/files").symlink_to(
            self.home / "safe-files"
        )
        with self.assertRaises(DomainError):
            self.write()

    def test_continuation_requires_unchanged_revision(self):
        self.write(body="a" * 13000)
        page = self.views.read(
            {"kind": "goal_file", "id": "g", "resource": "files/report.md"}
        )
        with self.assertRaises(DomainError):
            self.views.read(
                {
                    "kind": "goal_file",
                    "id": "g",
                    "resource": "files/report.md",
                    "offset": 12000,
                }
            )
        self.write(
            request_id="next", body="changed", expected_revision=page["revision"]
        )
        with self.assertRaises(DomainError):
            self.views.read(
                {
                    "kind": "goal_file",
                    "id": "g",
                    "resource": "files/report.md",
                    "offset": 12000,
                    "revision": page["revision"],
                }
            )

    def test_memory_classification_and_no_body_ledger(self):
        cases = [
            ("memory", {"success": True}, "saved"),
            ("memory", {"success": True, "staged": True}, "staged"),
            ("memory", {"success": True, "proposal_staged": True}, "staged"),
            (
                "memory",
                {
                    "success": True,
                    "message": "Entry already exists (no duplicate added)",
                },
                "duplicate",
            ),
            (
                "skill_manage",
                {"success": False, "message": "all touched skills rolled back"},
                "failed",
            ),
            (
                "hindsight_retain",
                "Memory stored successfully.",
                "submitted_unconfirmed",
            ),
            (
                "hindsight_retain",
                {"success": True, "items_count": 0},
                "submitted_unconfirmed",
            ),
            ("memory", "unreadable", "unknown"),
        ]
        for tool, result, state in cases:
            self.assertEqual(classify(tool, result), state)
        ledger = MemoryFeedback(self.store, lambda: 1800000000)
        ledger.capture(
            "memory",
            {"action": "add", "content": "SECRET BODY"},
            {"success": True, "message": "SECRET BODY"},
            session_id="session",
            turn_id="s",
            tool_call_id="call",
        )
        rows = self.store.all("memory_feedback")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("SECRET BODY", json.dumps(rows))
        self.assertEqual(ledger.context("other"), "")
        ledger.capture(
            "memory",
            {"action": "add"},
            {"success": True},
            session_id="session",
            turn_id="s",
            tool_call_id="call",
        )
        self.assertEqual(len(self.store.all("memory_feedback")), 1)

    def test_memory_receipts_newest_unsaved_within_a_day_and_not_after_defer(self):
        now = [1800000000]
        ledger = MemoryFeedback(self.store, lambda: now[0])
        for call in ("zz-old", "aa-mid", "mm-new"):  # IDs sort differently from creation order
            ledger.capture("hindsight_retain", {"content": "x"}, "Memory stored successfully.",
                           session_id="session", turn_id="s", tool_call_id=call)
            now[0] += 3600
        by_call = {r["tool_call_id"]: r for r in self.store.all("memory_feedback")}
        ids = lambda: [r["id"] for r in ledger.candidates("session")]
        self.assertEqual(ids(), [by_call[c]["id"] for c in ("zz-old", "aa-mid", "mm-new")])
        ledger.review({"receipt_id": by_call["aa-mid"]["id"], "decision": "defer", "reason": "not now",
                       "expected_revision": 1, "request_id": "defer-mid"}, "session", "s")
        self.assertEqual(ids(), [by_call["zz-old"]["id"], by_call["mm-new"]["id"]])
        now[0] = by_call["zz-old"]["created"] + 86400 + 1  # unsaved receipts stop being news after a day
        self.assertEqual(ids(), [by_call["mm-new"]["id"]])
        self.assertEqual(len(self.store.all("memory_feedback")), 3)  # kept for audit

    def test_forget_tombstone_and_owned_lessons_preserve_user_text(self):
        result = self.write()
        self.service.signal("delete", "session", "Please delete g files/report.md")
        data = {
            "kind": "content",
            "object_kind": "goal_file",
            "id": "g",
            "resource": "files/report.md",
            "signal_id": "delete",
            "expected_revision": result["revision"],
            "request_id": "forget-one",
        }
        self.assertTrue(
            self.content.forget(data, session_id="session")["owned_record_removed"]
        )
        self.assertTrue(
            self.content.forget(data, session_id="session")["owned_record_removed"]
        )
        with self.assertRaises(DomainError) as caught:
            self.write(request_id="late")
        self.assertEqual(caught.exception.code, "scope_closed")
        self.service.signal(
            "lesson-signal",
            "session",
            "Update working_lessons current with this verified convention",
        )
        original = self.views.files.read("AGENTS.md")
        rev = self.views.object("working_lessons", "current")[2]
        self.content.write(
            {
                "kind": "working_lessons",
                "id": "current",
                    "expected_revision": rev,
                "request_id": "lesson",
                "body": "- Verify facts before notifying.",
                "signal_id": "lesson-signal",
            },
            session_id="session",
        )
        after = self.views.files.read("AGENTS.md")
        self.assertEqual(
            original.split(b"<!-- muse-lessons:start -->")[0],
            after.split(b"<!-- muse-lessons:start -->")[0],
        )

    def test_same_revision_concurrent_writers_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor

        first = self.write()

        def writer(n):
            try:
                return self.write(
                    request_id="parallel-" + str(n),
                    body="writer-" + str(n),
                    expected_revision=first["revision"],
                )
            except DomainError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn("revision_conflict", results)

    def test_hidden_files_and_parent_symlink_swap_are_refused(self):
        self.store.write_text(
            "workspace/goals/g/hidden_files/private.md", "Private working material"
        )
        with self.assertRaises(DomainError):
            self.views.read(
                {"kind": "goal_file", "id": "g", "resource": "hidden_files/private.md"}
            )
        self.assertEqual(
            Views(self.home, allow_hidden=True).object(
                "goal_file", "g", "hidden_files/private.md"
            )[0],
            "Private working material",
        )
        original = self.views.files.read
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "report.md").write_text("Unrelated")
        count = [0]

        def swap(relative):
            raw = original(relative)
            count[0] += 1
            if count[0] == 1:
                folder = self.home / "muse/workspace/goals/g/files"
                folder.rename(self.home / "original-files")
                folder.symlink_to(outside)
            return raw

        first = self.write()
        with patch.object(self.views.files, "read", side_effect=swap):
            with self.assertRaises(DomainError):
                self.views.files.replace(
                    "workspace/goals/g/files/report.md", b"New", first["revision"]
                )
        self.assertEqual((outside / "report.md").read_text(), "Unrelated")

    def test_cross_process_cas_and_precommit_interruption(self):
        import multiprocessing
        first = self.write()
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        children = []
        for n in range(2):
            data = dict(kind="goal_file", id="g", resource="files/report.md",
                        body="process-" + str(n),
                        expected_revision=first["revision"], request_id="process-" + str(n))
            child = context.Process(target=process_writer, args=(str(self.home), data, start, results))
            child.start()
            children.append(child)
        start.set()
        outcomes = [results.get(timeout=20) for _ in children]
        for child in children:
            child.join(20)
            self.assertEqual(child.exitcode, 0)
        self.assertCountEqual(outcomes, ["written", "revision_conflict"])
        before = self.views.read(dict(kind="goal_file", id="g", resource="files/report.md"))
        with patch("hermes_muse.content_io.check_cancelled", side_effect=DomainError("outcome_unknown", "interrupted")):
            with self.assertRaises(DomainError):
                self.write(request_id="interrupted", expected_revision=before["revision"], body="Recovered")
        self.assertEqual(self.views.files.read("workspace/goals/g/files/report.md").decode(), before["body"])
        recovered = self.write(request_id="interrupted", expected_revision=before["revision"], body="Recovered")
        self.assertTrue(recovered["changed"])

    def test_feedback_restart_cancelled_and_missing_origin(self):
        from hermes_muse.store import Store
        ledger = MemoryFeedback(self.store, lambda: 1800000000)
        kwargs = dict(session_id="session", turn_id="s", tool_call_id="cancelled")
        ledger.capture("memory", {"action": "add"}, {"success": True}, **kwargs, status="cancelled")
        row = self.store.all("memory_feedback")[0]
        self.assertTrue(row["invalidated"])
        self.assertEqual(row["classified_state"], "unknown")
        ledger.capture("memory", {"action": "add"}, {"success": True}, turn_id="s", tool_call_id="missing")
        self.assertEqual(len(self.store.all("memory_feedback")), 1)
        restarted = MemoryFeedback(Store(self.home), lambda: 1800000000)
        restarted.capture("memory", {"action": "add"}, {"success": True}, **kwargs)
        self.assertEqual(len(self.store.all("memory_feedback")), 1)
        self.assertEqual(restarted.context("session"), "")
        self.assertEqual(restarted.context("other"), "")
        with self.assertRaises(DomainError) as caught:
            restarted.review(dict(receipt_id=row["id"], expected_revision=row["revision"],
                                  request_id="late-inform", decision="inform", reason="Late callback"), "session", "s")
        self.assertEqual(caught.exception.code, "scope_closed")
