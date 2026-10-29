"""Minimal receipt ledger. Never copy memory text, arguments or provider payloads."""

import json
import re
from .content_io import DomainError
from .store import DAY, digest

TOOLS = {"memory", "skill_manage", "hindsight_retain", "muse_preferences"}


def classify(tool, result):
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return (
                "submitted_unconfirmed"
                if tool == "hindsight_retain" and "success" in result.lower()
                else "unknown"
            )
    if not isinstance(result, dict):
        return "unknown"
    if result.get("staged") or result.get("proposal_staged"):
        return "staged"
    if (
        result.get("success") is False
        or result.get("error")
        or result.get("ok") is False
    ):
        return "failed"
    message = str(result.get("message", "")).lower()
    if "already exists" in message or "no duplicate" in message:
        return "duplicate"
    if tool == "hindsight_retain":
        if (
            result.get("success") is True
            and type(result.get("items_count")) is int
            and result["items_count"] > 0
            and result.get("suspended") is False
            and result.get("operation_id")
        ):
            return "saved"
        return "submitted_unconfirmed"
    if tool == "muse_preferences" and result.get("ok") is True:
        return (
            "duplicate"
            if isinstance(result.get("result"), dict)
            and result["result"].get("changed") is False
            else "saved"
        )
    if result.get("success") is True:
        entries = result.get("results")
        if (
            isinstance(entries, list)
            and entries
            and all(classify(tool, r) == "duplicate" for r in entries)
        ):
            return "duplicate"
        return "saved"
    return "unknown"


class MemoryFeedback:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    def capture(
        self,
        tool,
        args,
        result,
        *,
        session_id="",
        turn_id="",
        tool_call_id="",
        **kwargs,
    ):
        if tool == "muse_manage" and args.get("action") == "preferences":
            tool = "muse_preferences"
        if tool not in TOOLS or not all((session_id, turn_id, tool_call_id)):
            return
        signal = self.store.read("signal", turn_id, {})
        if signal.get("session_id") != session_id:
            return
        if (
            tool == "memory"
            and args.get("action")
            not in {"add", "replace", "remove", "delete", "batch"}
            and not isinstance(args.get("operations"), list)
        ):
            return
        if tool == "skill_manage" and args.get("action") in {
            "list",
            "view",
            "read",
            "search",
        }:
            return
        current = self.store.read("session", session_id, {})
        stale = current.get("turn_id") != turn_id or kwargs.get("status") in {
            "cancelled",
            "abandoned",
        }
        parsed = result
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except ValueError:
                parsed = {}
        pending_id = parsed.get("pending_id") if isinstance(parsed, dict) else None
        key = digest("memory-feedback:" + session_id + ":" + tool_call_id)
        with self.store.transaction() as db:
            if self.store.get(db, "memory_feedback", key):
                return
            # No memory bodies/target copies: destructive changes conservatively retire
            # earlier unannounced saves, including same-turn replacements/deletions.
            state = "unknown" if stale else classify(tool, result)
            if not stale and state == "saved" and (
                args.get("action") in {"replace", "remove", "delete", "edit", "batch"}
                or isinstance(args.get("operations"), list)
            ):
                for previous in self.store.rows(db, "memory_feedback"):
                    if (previous["session_id"] == session_id
                            and previous["classified_state"] == "saved"
                            and not previous.get("reply_ref") and not previous.get("invalidated")):
                        previous.update(invalidated=True, revision=previous["revision"] + 1)
                        self.store.put(db, "memory_feedback", previous["id"], previous)
            self.store.put(
                db,
                "memory_feedback",
                key,
                {
                    "id": key,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "tool_call_id": tool_call_id,
                    "tool": tool,
                    "classified_state": state,
                    "invalidated": stale,
                    "revision": 1,
                    "created": self.clock(),
                    "pending_id": pending_id,
                    "decision": "defer",
                    "reply_ref": None,
                },
            )

    def lifecycle(self, action="", skill_name="", session_id="", **kwargs):
        # No correlated tool receipt => no candidate and no inferred recipient.
        if action in {"loaded", "load"} or not session_id:
            return
        with self.store.transaction() as db:
            for row in self.store.rows(db, "memory_feedback"):
                if (
                    row["session_id"] == session_id
                    and row["tool"] == "skill_manage"
                    and self.clock() - row["created"] < 60
                ):
                    row["lifecycle_observed"] = True
                    self.store.put(db, "memory_feedback", row["id"], row)

    @staticmethod
    def informable(row, turn_id):
        return not row.get("invalidated") and (
            row["classified_state"] != "saved" or row["turn_id"] == turn_id
        )

    def candidates(self, session_id):
        current_turn = self.store.read("session", session_id, {}).get("turn_id")
        now = self.clock()
        rows = sorted(self.store.all("memory_feedback"), key=lambda r: (r.get("created", 0), r["id"]))
        return [
            {k: r[k] for k in ("id", "revision", "classified_state", "tool")}
            for r in rows
            if r["session_id"] == session_id
            and self.informable(r, current_turn)
            and not r.get("reply_ref")
            and r["decision"] != "silent"
            # Reviewed as defer means "not now"; an unsaved receipt is only news for a day. Records stay for audit.
            and not (r["decision"] == "defer" and "inform_turn_id" in r)
            and (r["classified_state"] == "saved" or now - r.get("created", now) <= DAY)
        ][-10:]

    def context(self, session_id):
        rows = self.candidates(session_id)
        if not rows:
            return ""
        return (
            "\n[Muse memory receipts] "
            + json.dumps(rows)
            + ". Use memory_feedback_review for this reply. Saved is not sent; staged awaits approval, submitted_unconfirmed is not durable proof. Routine retention stays silent. Never repeat the write."
        )

    def review(self, data, session_id, turn_id=""):
        if set(data) - {
            "receipt_id",
            "decision",
            "reason",
            "expected_revision",
            "request_id",
        }:
            raise DomainError("invalid_argument", "memory_feedback_review: unsupported data fields. Fix: send only receipt_id, decision, reason, expected_revision, request_id.")
        if data.get("decision") not in {"inform", "silent", "defer"}:
            raise DomainError("invalid_argument", "memory_feedback_review: data.decision must be one of inform, silent, defer. Fix: set data.decision to exactly one of them.", field="data.decision")
        if (
            not isinstance(data.get("reason"), str)
            or not 1 <= len(data["reason"]) <= 2000
        ):
            raise DomainError("invalid_argument", "reason is required")
        request = data.get("request_id")
        if not isinstance(request, str) or not 1 <= len(request) <= 128:
            raise DomainError("invalid_argument", "request_id required")
        with self.store.transaction() as db:
            row = self.store.get(db, "memory_feedback", data.get("receipt_id"))
            if not row or row["session_id"] != session_id:
                raise DomainError(
                    "permission_denied", "Receipt unavailable in this session"
                )
            current_turn = self.store.get(db, "session", session_id, {}).get("turn_id")
            # Check before idempotent replay: a previously valid inform decision is
            # not authority to re-announce an obsolete save in a later turn.
            if data["decision"] == "inform" and (
                turn_id != current_turn or not self.informable(row, current_turn)
            ):
                raise DomainError("scope_closed", "Saved feedback is confined to its original current turn; obsolete receipts cannot be announced")
            signature = digest(json.dumps(data, sort_keys=True))
            old = self.store.get(db, "memory_feedback_request", request)
            if old:
                if old["signature"] != signature:
                    raise DomainError("idempotency_conflict", "Request key reused")
                return old["result"]
            if data.get("expected_revision") != row["revision"]:
                raise DomainError(
                    "revision_conflict",
                    "Reread receipt revision",
                    current_revision=row["revision"],
                )
            if row.get("invalidated") and data["decision"] == "inform":
                raise DomainError(
                    "scope_closed",
                    "Late receipt cannot announce an obsolete preference",
                )
            if row.get("reply_ref") and data["decision"] == "inform":
                raise DomainError(
                    "scope_closed", "This receipt already has reply evidence"
                )
            row.update(
                decision=data["decision"],
                inform_turn_id=turn_id,
                revision=row["revision"] + 1,
            )
            self.store.put(db, "memory_feedback", row["id"], row)
            result = {
                k: row[k] for k in ("id", "revision", "classified_state", "decision")
            }
            self.store.put(
                db,
                "memory_feedback_request",
                request,
                {"signature": signature, "result": result},
            )
            return result

    def finish(self, session_id, turn_id, response="", history=()):
        # Use the real current final reply directly; no followup reply ledger.
        signal = self.store.read('signal', turn_id, {})
        if signal.get('session_id') != session_id:
            return
        import sqlite3
        path = self.store.home / 'state.db'
        if not path.is_file() or path.is_symlink():
            return
        try:
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute("SELECT id,content FROM messages WHERE session_id=? AND role='assistant' AND timestamp>=? AND (tool_calls IS NULL OR tool_calls='[]') AND (display_kind IS NULL OR display_kind!='hidden') ORDER BY id DESC LIMIT 1", (session_id, signal['created'])).fetchone()
            if not row or response and response != row[1]:
                return
            response = row[1]
            row_id = row[0]
        except sqlite3.Error:
            return
        with self.store.transaction() as db:
            for row in self.store.rows(db, "memory_feedback"):
                if (
                    row["session_id"] == session_id
                    and row.get("inform_turn_id") == turn_id
                    and row["decision"] == "inform"
                    and self.informable(row, self.store.get(db, "session", session_id, {}).get("turn_id"))
                    and not row.get("reply_ref")
                ):
                    patterns = {
                        "saved": r"记下|记住|已保存|已更新|saved|noted|remember|updated",
                        "staged": r"批准|审批|待批|approval|pending",
                        "submitted_unconfirmed": r"未确认|没法确认|不能确认|unconfirmed|cannot confirm|can.t confirm",
                        "failed": r"失败|没存|未保存|未更新|failed|not saved|didn.t save",
                        "duplicate": r"已经|重复|没变|already|duplicate|unchanged",
                        "unknown": r"未确认|不能确认|unknown|unconfirmed|cannot confirm",
                    }
                    matched = bool(
                        re.search(
                            patterns[row["classified_state"]], response or "", re.I
                        )
                    )
                    if row["classified_state"] == "saved" and re.search(
                        r"未保存|没存|not saved|didn.t save", response or "", re.I
                    ):
                        matched = False
                    row.update(
                        reply_ref={'session_id': session_id, 'message_id': row_id},
                        reply_state="recorded_unconfirmed"
                        if matched
                        else "content_unverified",
                    )
                    self.store.put(db, "memory_feedback", row["id"], row)
