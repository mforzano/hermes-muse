"""Authorized body-only writes with durable intent and replay recovery."""

import re
import time

from .content_io import DomainError, revision
from .host_v1 import file_lock
from .views import Views, canonical, mapping, validate_tools_note

WRITABLE = {
    "goal_file",
    "tools_note",
    "note",
    "note_index",
    "working_lessons",
    "dream",
    "alignment",
    "review_report",
}


class Content:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.views = Views(self.store.home)

    def authorize(self, data, session_id, run):
        kind, key = data["kind"], data["id"]
        if kind not in WRITABLE:
            raise DomainError(
                "permission_denied", "Use the semantic action for managed state"
            )
        # Maintenance is a real, host-bound execution, never a model-supplied flag.
        if run:
            if run.get("status") not in {"running", "partial"}:
                raise DomainError(
                    "scope_closed", "This run already declared its artifacts complete"
                )
            job = run["job"]
            allowed = {
                "memory-upkeep": {
                    "note",
                    "note_index",
                    "working_lessons",
                    "tools_note",
                },
                "nightly-review": {
                    "note",
                    "note_index",
                    "working_lessons",
                    "tools_note",
                    "dream",
                    "alignment",
                    "goal_file",
                },
                "weekly-governance-review": {"review_report"},
                "monthly-system-audit": {"review_report"},
            }
            if (
                job.startswith("watch-")
                and self.service.host.manifest()["jobs"]
                .get(run["job_id"], {})
                .get("owner")
                == key
            ):
                allowed[job] = {"goal_file"}
            if kind not in allowed.get(job, set()):
                raise DomainError(
                    "permission_denied", "Content is outside this maintenance execution"
                )
            if kind == "review_report" and key != run["id"]:
                raise DomainError("permission_denied", "Report must belong to this run")
            if (
                kind == "dream"
                and key
                != self.service.host.local_now(run["started"]).date().isoformat()
            ):
                raise DomainError(
                    "permission_denied", "Dream date must belong to this run"
                )
        else:
            if kind == "goal_file":
                goal, _ = self.store.goal(key)
                if goal.get("session_id") != session_id:
                    raise DomainError(
                        "permission_denied", "Goal is outside this parent session"
                    )
                if (
                    goal["status"] != "active"
                    or goal.get("research_review_at", 0) <= self.service.clock()
                ):
                    raise DomainError(
                        "scope_closed",
                        "Goal must be active, current and owned by this parent session",
                    )
                if data.get("resource", "").startswith("hidden_files/"):
                    raise DomainError(
                        "permission_denied",
                        "Hidden artifacts require assigned maintenance scope",
                    )
                if data.get("signal_id"):
                    with self.store.transaction() as db:
                        signal = self.service.require_signal(db, data["signal_id"])
                    if signal.get("session_id") != session_id:
                        raise DomainError(
                            "permission_denied", "Signal belongs to another session"
                        )
            else:
                with self.store.transaction() as db:
                    signal = self.service.require_signal(db, data.get("signal_id"))
                current = self.store.read("session", session_id, {})
                if (
                    signal.get("session_id") != session_id
                    or current.get("turn_id") != signal["id"]
                ):
                    raise DomainError(
                        "permission_denied", "Current originating user request required"
                    )
                if (
                    key not in signal.get("text", "")
                    or key == "current"
                    and kind not in signal["text"]
                ):
                    raise DomainError(
                        "permission_denied",
                        "Explicit user request must identify this content object",
                    )

        if kind == "goal_file":
            goal, _ = self.store.goal(key)
            if goal["status"] != "active":
                raise DomainError("scope_closed", "Goal is closed")

    def write(self, data, *, session_id="", run=None):
        allowed = {
            "kind",
            "id",
            "resource",
            "body",
            "expected_revision",
            "request_id",
            "signal_id",
        }
        if set(data) - allowed:
            raise DomainError("invalid_argument", "Unknown content write fields")
        for key in (
            "kind",
            "id",
            "expected_revision",
            "request_id",
            "body",
        ):
            if not isinstance(data.get(key), str):
                raise DomainError("invalid_argument", key + " must be text")
        if not 1 <= len(data["request_id"]) <= 128:
            raise DomainError(
                "invalid_argument", "request_id must be 1..128 characters"
            )
        body = data["body"]
        if len(body) > 60000 or len(body.encode()) > 256 * 1024:
            raise DomainError("size_limit", "Body exceeds 60000 characters / 256 KiB")
        if (
            "<!-- hermes-muse" in body.casefold()
            or "<!-- muse-lessons:" in body.casefold()
        ):
            raise DomainError(
                "invalid_argument", "Managed metadata cannot be written as body"
            )
        if data["kind"] == "tools_note":
            validate_tools_note(body)
        relative = mapping(data["kind"], data["id"], data.get("resource"))
        key = revision(
            canonical([self.views.scope(), "content_write", data["request_id"]])
        )
        signature = revision(canonical(data))
        with file_lock(self.store.path("content.lock")):
            self.authorize(data, session_id, run)
            old = self.store.read("content_request", key)
            if old:
                if old["signature"] != signature:
                    raise DomainError(
                        "idempotency_conflict",
                        "request_id already has different arguments",
                    )
                if old.get("result"):
                    return old["result"]
                try:
                    current = revision(self.views.files.read(relative))
                except FileNotFoundError:
                    current = "absent"
                if current == old["new_revision"]:
                    result = old["planned_result"]
                    self.store.write(
                        "content_request",
                        key,
                        {**old, "result": result, "state": "complete"},
                    )
                    return result
                if current != old["old_revision"]:
                    raise DomainError(
                        "outcome_unknown",
                        "Pending write cannot be resolved; preserve evidence",
                    )
            if self.store.read("content_tombstone", relative):
                raise DomainError(
                    "scope_closed",
                    "Forgotten content cannot be recreated by a late write",
                )
            raw = body.encode()
            if data["kind"] == "working_lessons":
                original = self.views.files.read(relative).decode()
                match = re.search(
                    r"(?m)^## Working lessons\s*\n(.*?)(?=^## |\Z)", original, re.S
                )
                start = "<!-- muse-lessons:start -->"
                end = "<!-- muse-lessons:end -->"
                if not match or start not in match[1] or end not in match[1]:
                    raise DomainError(
                        "permission_denied",
                        "No explicitly plugin-owned lessons region; preserve user entries",
                    )
                section = match[1]
                a, b = section.index(start), section.index(end) + len(end)
                section = section[:a] + start + "\n" + body + "\n" + end + section[b:]
                raw = (
                    original[: match.start(1)] + section + original[match.end(1) :]
                ).encode()
            if data["kind"] == "note_index":
                for target in re.findall(r"\]\(([^)]+)\)", body):
                    if (
                        not re.fullmatch(r"[A-Za-z0-9_-]+\.md", target)
                        or target.upper() == "INDEX.MD"
                    ):
                        raise DomainError(
                            "invalid_argument", "Index links must name owned notes"
                        )
                    self.views.object(
                        "note",
                        ("person:" if data["id"] == "people" else "group:")
                        + target[:-3],
                    )
            planned = {
                "kind": data["kind"],
                "id": data["id"],
                "resource": data.get("resource"),
                "revision": revision(raw),
                "changed": data["expected_revision"] != revision(raw),
                "ref": {
                    "kind": "muse_object",
                    "object_kind": data["kind"],
                    "id": data["id"],
                    "resource": data.get("resource"),
                    "revision": revision(raw),
                },
                "request_id": data["request_id"],
            }
            intent = {
                "signature": signature,
                "old_revision": data["expected_revision"],
                "new_revision": revision(raw),
                "planned_result": planned,
                "state": "pending",
                "created": time.time(),
            }
            self.store.write("content_request", key, intent)
            changed = self.views.files.replace(relative, raw, data["expected_revision"])
            result = {**planned, "changed": changed}
            self.store.write(
                "content_request",
                key,
                {**intent, "state": "complete", "result": result},
            )
            return result

    def forget(self, data, *, session_id=""):
        kind = data.get("object_kind")
        if kind not in {"note", "goal_file", "dream", "alignment", "review_report"}:
            raise DomainError(
                "permission_denied",
                "Content deletion requires an explicitly supported object",
            )
        for field in ("request_id", "expected_revision", "signal_id", "id"):
            if not isinstance(data.get(field), str) or not data[field]:
                raise DomainError("invalid_argument", field + " required")
        if len(data["request_id"]) > 128:
            raise DomainError("invalid_argument", "request_id too long")
        relative = mapping(kind, data["id"], data.get("resource"))
        with self.store.transaction() as db:
            signal = self.service.require_signal(db, data["signal_id"])
        current = self.store.read("session", session_id, {})
        if (
            signal["session_id"] != session_id
            or current.get("turn_id") != signal["id"]
            or data["id"] not in signal["text"]
            or not any(
                w in signal["text"].lower()
                for w in ("forget", "delete", "remove", "忘记", "删除", "移除")
            )
        ):
            raise DomainError(
                "permission_denied",
                "Current original user request must identify the content to delete",
            )
        if kind == "goal_file":
            try:
                goal, _ = self.store.goal(data["id"])
            except ValueError:
                goal = self.store.read("goal_tombstone", data["id"], {})
            if (
                goal.get("session_id") != session_id
                or goal["status"] != "cancelled"
                and data.get("resource") not in signal["text"]
            ):
                raise DomainError(
                    "permission_denied",
                    "Cancel goal or explicitly authorize this one file deletion",
                )
        signature = revision(canonical(data))
        key = revision(canonical([self.views.scope(), "forget", data["request_id"]]))
        with file_lock(self.store.path("content.lock")):
            prior = self.store.read("content_request", key)
            if prior:
                if prior["signature"] != signature:
                    raise DomainError("idempotency_conflict", "Delete key reused")
                if prior.get("result"):
                    return prior["result"]
            try:
                raw = self.views.files.read(relative)
            except FileNotFoundError:
                if not prior:
                    raise DomainError("not_found", "Content unavailable")
                raw = None
            if raw is not None and revision(raw) != data["expected_revision"]:
                raise DomainError(
                    "revision_conflict",
                    "Content changed",
                    current_revision=revision(raw),
                )
            intent = {
                "signature": signature,
                "state": "pending",
                "object": relative,
                "action": "forget",
            }
            self.store.write("content_request", key, intent)
            self.store.write(
                "content_tombstone",
                relative,
                {"request_id": data["request_id"], "at": self.service.clock()},
            )
            if raw is not None:
                self.views.files.unlink(relative, data["expected_revision"])
            remaining = []
            if kind == "note" and data["id"].startswith(("person:", "group:")):
                category, name = data["id"].split(":", 1)
                index = (
                    "memory/"
                    + ("people" if category == "person" else "groups")
                    + "/INDEX.md"
                )
                try:
                    old = self.views.files.read(index)
                    updated = "".join(
                        line
                        for line in old.decode().splitlines(keepends=True)
                        if "](" + name + ".md)" not in line
                    ).encode()
                    self.views.files.replace(index, updated, revision(old))
                except (OSError, ValueError):
                    remaining.append("note_index:" + category)
            result = {
                "owned_record_removed": True,
                "object_kind": kind,
                "id": data["id"],
                "remaining_review": remaining,
                "partial": bool(remaining),
                "scope": "Muse object only; native memories, conversation history and backups unchanged",
            }
            self.store.write(
                "content_request",
                key,
                {**intent, "state": "complete", "result": result},
            )
            return result
