"""Actionable argument errors: field path, expected shape, what arrived, how to fix it.

Pure helpers shared by contracts.validate (up-front checks), the calendar assembler
and the single tool's error boundary. Error codes stay as they were; only the
message and extra fields (field, expected, received, hint, example) are richer.
"""
from __future__ import annotations

import difflib
import json
import re
import secrets
import time

from .content_io import DomainError

NOTHING = object()
SAME_AGAIN = "Resending the same arguments will fail again; change them as described first."
KEY_RULE = ("A request_id must be unique to this run: include this run's start time to the minute (e.g. \"{action}-20261023T1205-mail-inbox\"). "
            "A date alone is not enough, because later runs the same day send different arguments; never reuse a key from an earlier run.")
SOURCE = {"connection": "mail-cli", "account": "default", "resource": "mail:inbox"}

def fresh_key(old):
    """A ready, unused replacement for a key whose arguments changed: the old key plus the current time and a short random tail."""
    return f"{old[:100]}-{time.strftime('%Y%m%dT%H%M%S')}{secrets.token_hex(2)}"


# Minimal correct calls for the actions models get wrong most. Illustrative values only.
EXAMPLES = {
    "context": {"action": "context", "data": {"section": "notice", "limit": 25}},
    "content_read": {"action": "content_read", "data": {"kind": "notice", "id": "<id from the context row>"}},
    "goal_create": {"action": "goal_create", "data": {"signal_id": "<signal id from the turn>", "title": "Choose a bicycle",
                    "description": "Compare candidates within budget", "completion_criteria": "User confirms a purchase"}},
    "goal_update": {"action": "goal_update", "data": {"id": "<goal id>", "progress": "Shortlisted two models"}},
    "notification_add": {"action": "notification_add", "data": {
        "request_id": "notification_add-<run start YYYYMMDDTHHMM>-<label>", "expected_revision": "absent", "event_key": "<stable key for this event>",
        "message": "<what to tell the user>", "rationale": "<why it matters now>", "sources": ["<original item reference>"],
        "verified_at": "2026-01-01T09:00:00+00:00", "expires": "2026-01-02T09:00:00+00:00"}},
    "feed_add": {"action": "feed_add", "data": {"request_id": "feed_add-<run start YYYYMMDDTHHMM>-<label>", "expected_revision": "absent",
                 "event_key": "<stable key>", "title": "<title>", "content": "<article text>", "sources": ["<url or reference>"], "why": "<why relevant>"}},
    "watch_create": {"action": "watch_create", "data": {"request_id": "watch_create-<run start YYYYMMDDTHHMM>-<label>", "expected_revision": "absent",
                     "goal_id": "<active goal id>", "signal_id": "<signal id>", "schedule": "every 30m",
                     "prompt": "<what to check>", "stop_condition": "<when to stop>"}},
    "idea_add": {"action": "idea_add", "data": {"request_id": "idea_add-<run start YYYYMMDDTHHMM>-<label>", "expected_revision": "absent",
                 "title": "<short headline>", "rationale": "<why it helps this user now>", "sources": ["<signal, goal or feed reference>"],
                 "goal_id": "<optional active goal id>", "expires": "2026-01-15T09:00:00+00:00"}},
    "calendar_window": {"action": "calendar_window", "data": {"mode": "rolling"}},
    "calendar_view": {"action": "calendar_view", "data": {"window": "<the unchanged object returned by calendar_window>",
                      "discovery_complete": True, "sources": [{"source": {"connection": "calendar-cli", "account": "default", "resource": "calendars:rolling-30d"}, "title": "Work calendars",
                      "inventory": {"pages": [{"status": "checked", "observed_at": "<ISO time>", "items": [{"id": "cal-1", "title": "Work", "timezone": "UTC"}]}]},
                      "calendars": [{"resource_id": "cal-1", "window": "<same window object>", "pages": [{"status": "checked", "observed_at": "<ISO time>", "items": []}]}]}]}},
    "source_check": {"action": "source_check", "data": {
        "request_id": "source_check-<run start YYYYMMDDTHHMM>-<label>", "expected_revision": "<revision from context(section=source_check) or \"absent\">",
        "source": SOURCE, "scope": "unread mail from the last 24 hours",
        "status": "checked", "expected_resources": ["mail:inbox"], "checked_resources": ["mail:inbox"], "summary": "Read 12 messages; 2 relevant"}},
    "review_complete": {"action": "review_complete", "data": {"job": "feed-pulse", "up_to": "2026-01-01T09:00:00+00:00", "summary": "Reviewed new signals"}},
    "run_complete": {"action": "run_complete", "data": {"outcome": "partial", "summary": "Checked mail", "gaps": ["Calendar unreachable; retry next run"]}},
    "delegate": {"action": "delegate", "data": {"goal": "<one self-contained task>", "context": "<facts the worker needs>"}},
    "research_start": {"action": "research_start", "data": {"items": ["<question 1>", "<question 2>"]}},
    "feedback": {"action": "feedback", "data": {"request_id": "feedback-<run start YYYYMMDDTHHMM>-<label>", "expected_revision": "<revision>",
                 "kind": "notification", "id": "<notice id>", "action": "dismiss", "signal_id": "<signal id>"}},
}

# Where a commonly misplaced field belongs: (action, field) -> fix text.
MISPLACED = {
    ("delegate", "task"): "delegate has no `task`: put one task's instructions in data.goal (text), or a batch in data.tasks=[{\"goal\": ..., \"context\": ...}]",
    ("delegate", "prompt"): "put the instructions in data.goal (text) or data.tasks=[{\"goal\": ...}]",
    ("delegate", "instruction"): "put the instructions in data.goal (text) or data.tasks=[{\"goal\": ...}]",
    ("delegate", "instructions"): "put the instructions in data.goal (text) or data.tasks=[{\"goal\": ...}]",
    ("delegate", "model"): "model/route/credentials are chosen by the host and cannot be passed; delete it",
    ("delegate", "toolsets"): "toolsets/transport settings are chosen by the host and cannot be passed; delete it",
    ("source_check", "window"): "mail/reminder sources put their time range in data.scope (text); only calendar_view coverage carries data.query_window (the unchanged calendar_window object)",
    ("source_check", "id"): "the record id is derived from data.source; to merge identities pass data.source_id (same account and resource)",
    ("source_check", "source_check_id"): "the record id is derived from data.source; to merge identities pass data.source_id (same account and resource)",
    ("calendar_view", "query_window"): "rename it to data.window",
    ("calendar_view", "calendars"): "rename it to data.sources (one entry per connection/account, each with its own `calendars` reads)",
    ("calendar_view", "mode"): "mode belongs to calendar_window; call calendar_window first and pass its result as data.window",
    ("review_complete", "source_check"): "pass the saved source_check record IDs in data.source_check_ids (array of text)",
    ("idea_add", "text"): "idea_add has no `text`: put a short headline in data.title and the reasoning in data.rationale (both required text)",
    ("idea_add", "content"): "put a short headline in data.title and the reasoning in data.rationale (both required text)",
    ("idea_add", "description"): "put a short headline in data.title and the reasoning in data.rationale (both required text)",
    ("idea_add", "reason"): "rename it to data.rationale",
    ("idea_add", "why"): "rename it to data.rationale",
    ("idea_add", "source"): "rename it to data.sources (an array of references)",
    ("idea_add", "kind"): "idea_add always creates an Idea, so it takes no kind; delete it (reading uses content_read kind=\"idea\", singular, never \"ideas\")",
    ("idea_add", "type"): "idea_add always creates an Idea, so it takes no kind; delete it",
    ("watch_create", "interval"): "use data.schedule, e.g. \"every 30m\"",
    ("watch_create", "cron"): "use data.schedule, e.g. \"every 30m\"",
    ("context", "kind"): "use data.section (e.g. \"notice\"); call context with no data to list valid sections",
    ("status", "kind"): "use data.section (e.g. \"notice\"); call status with no data to list valid sections",
    ("content_read", "section"): "content_read takes data.kind (and data.id); sections belong to context/status",
    ("feed_list", "kind"): "feed_list takes no kind; use data.query or data.topic to filter",
}
# Fields that only make sense inside a nested object of the same action: action -> (names, parent).
NESTED_HOME = {"source_check": ({"connection", "account", "resource"}, "source")}
ALIASES = {"revision": "expected_revision", "current_revision": "expected_revision", "_revision": "expected_revision",
           "requestId": "request_id", "idempotency_key": "request_id"}

TEXT = {"cursor", "id", "kind", "request_id", "signal_id", "resource", "body", "title", "description", "completion_criteria", "query",
        "topic", "receipt_id", "section", "revision", "snapshot_revision", "part"}
MORE_TEXT = {"message", "summary", "scope", "reason", "rationale", "content", "why", "prompt", "stop_condition", "event_key", "issue_key",
             "priority_reason", "schedule", "alias_reason", "token", "receipt", "job", "note", "progress", "goal", "context", "feedback"}
INT = {"offset", "length", "limit", "batch"}
BOOL = {"delete", "final_result", "read_only", "descriptor", "discovery_complete"}
LIST = {"ids", "sources", "blocked_topics", "items", "reviews", "tasks", "gaps", "source_check_ids", "expected_resources", "checked_resources"}
OBJECT = {"source", "query_window", "source_snapshot"}
TIME = {"verified_at", "expires", "deadline", "next_check_at", "research_review_at", "started_at", "observed_at", "up_to"}
TIME_BY_ACTION = {("feedback", "until"), ("operations_evidence", "since"), ("operations_evidence", "until")}
IDENTIFIER_ACTIONS = {"goal_update", "notification_refresh", "notification_review", "notification_resolve", "notification_prepare",
                      "notification_queue", "notification_handoff", "feed_update", "feedback"}

ENUMS = {
    ("source_check", "status"): ("checked", "partial", "error", "unavailable"),
    ("notification_review", "verdict"): ("relevant", "resolved", "superseded"),
    ("run_complete", "outcome"): ("complete", "partial"),
    ("feedback", "kind"): ("notification", "interest"),
    ("feedback", "action"): ("done", "dismiss", "stop", "accept", "snooze"),
    ("goal_update", "status"): ("active", "done", "cancelled"),
    ("notification_add", "priority"): ("ordinary", "time_sensitive", "urgent", "promised"),
    ("notification_refresh", "priority"): ("ordinary", "time_sensitive", "urgent", "promised"),
    ("memory_feedback_review", "decision"): ("inform", "silent", "defer"),
    ("installation_prepare", "operation"): ("disable", "remove"),
    ("calendar_window", "mode"): ("rolling", "month"),
    ("review_complete", "job"): ("memory-upkeep", "nightly-review", "feed-pulse", "proactive-watch",
                                 "weekly-governance-review", "monthly-system-audit", "watch-<id>"),
    ("forget", "kind"): ("interest", "notification", "feed", "goal", "operation_finding", "content"),
}

# Fields the action cannot run without on any path (checked against its service code); value is the field kind.
REQUIRED = {
    "goal_create": {"title": "text", "description": "text", "completion_criteria": "text"},
    "goal_update": {"id": "text"},
    "interest_record": {"signal_id": "text", "title": "text"},
    "idea_add": {"title": "text"},
    "feedback": {"kind": "text", "id": "text", "action": "text", "signal_id": "text"},
    "notification_add": {"event_key": "text", "message": "text", "rationale": "text", "sources": "list", "verified_at": "time", "expires": "time"},
    "notification_refresh": {"id": "text", "verified_at": "time"},
    "notification_review": {"id": "text"},
    "notification_resolve": {"id": "text", "verified_at": "time", "sources": "list", "reason": "text"},
    "notification_prepare": {"id": "text"},
    "notification_queue": {"id": "text"},
    "notification_handoff": {"id": "text"},
    "watch_create": {"goal_id": "text", "signal_id": "text", "prompt": "text", "stop_condition": "text"},
    "watch_stop": {"job_id": "text"},
    "feed_add": {"event_key": "text"},
    "feed_update": {"id": "text", "signal_id": "text"},
    "calendar_view": {"window": "object", "sources": "list"},
    "source_check": {"source": "object", "status": "text", "scope": "text", "summary": "text"},
    "review_complete": {"job": "text", "up_to": "time", "summary": "text"},
    "run_complete": {"outcome": "text", "summary": "text"},
    "research_start": {"items": "list"},
    "research_status": {"id": "text"},
    "operations_complete": {"token": "text", "summary": "text"},
    "forget": {"kind": "text", "id": "text"},
    "installation_prepare": {"operation": "text"},
    "conversation_read": {"session_id": "text"},
    "content_write": {"kind": "text", "id": "text", "body": "str", "expected_revision": "text", "request_id": "text"},
}
# Needed only once the call gets past an idempotent/early-return path; used to name the field in late failures.
LATE = {
    "idea_add": {"rationale": "text"},
    "feed_add": {"title": "text", "content": "text", "sources": "list", "why": "text"},
    "notification_review": {"verdict": "text", "reason": "text"},
}
REQUIRED_FIX = {("calendar_view", "window"): "call calendar_window first and pass its unchanged result as data.window",
                ("calendar_view", "sources"): "add data.sources: one entry per discovered calendar connection/account (see example)"}
KIND_WORDS = {"text": "non-empty text", "str": "text (may be empty)", "time": "a timestamp: epoch seconds or ISO-8601 with timezone (e.g. 2026-01-01T09:00:00+00:00)",
              "list": "an array", "object": "an object", "int": "an integer", "bool": "true or false"}


def describe(value, width=60):
    kind = {str: "text", bool: "boolean", int: "integer", float: "number", list: "array", dict: "object", type(None): "null"}.get(type(value), type(value).__name__)
    shown = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (list, dict)) else repr(value)
    return kind + " " + (shown if len(shown) <= width else shown[:width] + "…")


def fail(action, path, problem, *, fix, expected=None, got=NOTHING, code="invalid_argument", retry=False, **extra):
    """Build the DomainError every actionable argument failure uses."""
    parts = [f"{action}: {path} {problem}."]
    if expected:
        parts.append(f"Expected {expected}.")
    if got is not NOTHING:
        parts.append(f"Got {describe(got)}.")
    parts.append("Fix: " + fix.rstrip(".") + ".")
    if not retry:
        parts.append(SAME_AGAIN)
    details = {"field": path, "hint": fix, "retryable": retry, **extra}
    if expected:
        details["expected"] = expected
    if got is not NOTHING:
        details["received"] = describe(got)
    if action in EXAMPLES:
        details["example"] = EXAMPLES[action]
    return DomainError(code, " ".join(parts), **details)


def allowed_fields(action):
    from .contracts import FIELDS, CAS, CREATES, REPLAY_ACTIONS
    allowed = set(FIELDS[action].split())
    if action in CAS or action in CREATES:
        allowed |= {"expected_revision", "request_id"}
    if action in REPLAY_ACTIONS:
        allowed.add("request_id")
    return allowed


def field_kind(action, name):
    declared = REQUIRED.get(action, {}).get(name) or LATE.get(action, {}).get(name)
    if declared:
        return declared
    if (action, name) in ENUMS or name in MORE_TEXT:
        return "text"
    if name in OBJECT or name == "window" and action == "calendar_view":
        return "object"
    if name in INT:
        return "int"
    if name in BOOL:
        return "bool"
    if name in LIST:
        return "list"
    if name in TIME or (action, name) in TIME_BY_ACTION:
        return "time"
    if name == "window" or name.endswith("_id") or name in TEXT:
        return "text"
    return None


def unknown_action(action):
    from .contracts import FIELDS
    near = difflib.get_close_matches(str(action), FIELDS, n=3)
    return fail("muse_manage", "action", "is not a known action", expected="one of: " + ", ".join(FIELDS), got=action,
                fix=("did you mean " + " / ".join(near) + "? " if near else "") + "set top-level `action` to one listed value and put its arguments in `data`")


def unsupported(action, data, allowed):
    names = sorted(set(data) - allowed)
    fixes, home = [], NESTED_HOME.get(action)
    for name in names[:8]:
        if (action, name) in MISPLACED:
            fixes.append(f"data.{name}: {MISPLACED[(action, name)]}")
        elif home and name in home[0]:
            moved = [n for n in names if n in home[0]]
            if name == moved[0]:
                nested = data.get(home[1]) if isinstance(data.get(home[1]), dict) else {}
                if nested:
                    differs = [n for n in moved if n in nested and data.get(n) != nested[n]]
                    fixes.append("delete " + ", ".join("data." + n for n in moved) + f": data.{home[1]} already holds them"
                                 + (f"; they differ from data.{home[1]} for {', '.join(differs)}, so keep the value that is right inside data.{home[1]}" if differs else ""))
                    continue
                shape = {k: (data[k] if isinstance(data.get(k), str) else nested.get(k) or SOURCE[k]) for k in sorted(home[0])}
                fixes.append("move " + ", ".join("data." + n for n in moved) + f" into data.{home[1]} = " + json.dumps(shape, ensure_ascii=False)[:300]
                             + " (all three keys are required; resource is a stable collection such as \"mail:inbox\")")
        elif name in ALIASES:
            fixes.append(f"data.{name}: rename to data.{ALIASES[name]}" if ALIASES[name] in allowed else f"data.{name} is not used by {action}; delete it")
        elif name in {"request_id", "expected_revision"}:
            fixes.append(f"data.{name} is not accepted by {action} (no replay key or revision check is needed); delete it")
        elif name == "data":
            fixes.append("do not nest data inside data; put the fields directly in `data`")
        elif name == "action":
            fixes.append("`action` belongs beside `data`, not inside it")
        else:
            near = difflib.get_close_matches(name, allowed, n=2)
            fixes.append(f"data.{name}: " + (("did you mean " + " or ".join(near)) if near else "delete it or correct the field name"))
    tail = " (and more; all rejected)" if len(names) > 8 else ""
    return fail(action, ", ".join("data." + n for n in names[:8]) + tail, "is not a field of this action" if len(names) == 1 else "are not fields of this action",
                expected="only these data fields: " + " ".join(sorted(allowed)),
                fix="; ".join(fixes), fields=["data." + n for n in names[:8]])


def explain_part(exc):
    """Python exception from deep parsing -> a sentence without interpreter jargon."""
    from zoneinfo import ZoneInfoNotFoundError
    if isinstance(exc, ZoneInfoNotFoundError):
        return "unknown IANA timezone name (use e.g. America/New_York, or timezone_kind=fixed with utc_offset_seconds)"
    if isinstance(exc, KeyError):
        return f"missing required key {exc.args[0]!r}" if exc.args else "missing required key"
    if isinstance(exc, (TypeError, AttributeError, IndexError)):
        return "a value has the wrong type or shape (an object where text was needed, or text where an object was needed)"
    return str(exc)


def unexpected(exc, action, data):
    """Entry-boundary fallback for interpreter exceptions that escaped validation."""
    from zoneinfo import ZoneInfoNotFoundError
    from .contracts import FIELDS
    allowed = sorted(allowed_fields(action)) if action in FIELDS else []
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str) and not isinstance(exc, ZoneInfoNotFoundError):
        key = exc.args[0]
        if key in allowed:
            path, what = f"data.{key}", "is required but missing"
            fix = f"add data.{key} ({KIND_WORDS.get(field_kind(action, key), 'see example')})"
        else:
            path, what = f"required key {key!r} inside data", "is missing from a nested object"
            fix = f"add the key {key!r} to the nested object that lacks it; compare every nested object with the example/allowed fields"
        return fail(action, path, what, expected="the fields for " + action + ": " + " ".join(allowed), fix=fix)
    return fail(action, "data", "contains a value that cannot be used: " + explain_part(exc),
                expected="field types for " + action + ": " + " ".join(allowed),
                fix="check each field against its documented type (objects as objects, lists as arrays, timestamps as ISO text with timezone) and compare with `example`; do not JSON-encode objects into text")


def bad_envelope(args):
    """The tool takes exactly {action, data}; say where each stray argument belongs."""
    from .contracts import FIELDS
    if not isinstance(args, dict):
        return fail("muse_manage", "arguments", "must be a JSON object", expected='{"action": <name>, "data": {<fields>}}', got=args,
                    fix='call muse_manage with an object holding `action` (text) and `data` (object)')
    extra = sorted(set(args) - {"action", "data"})
    action = args.get("action") if args.get("action") in FIELDS else "muse_manage"
    moved = [k for k in extra if action != "muse_manage" and k in allowed_fields(action)]
    if moved:
        nested = args["data"] if isinstance(args.get("data"), dict) else {}
        call = json.dumps({"action": action, "data": {**nested, **{k: args[k] for k in moved}}}, ensure_ascii=False, default=str)
        fix = f"move {', '.join(moved)} inside `data`, e.g. {call[:300]}"
    else:
        fix = 'put every argument inside `data`: {"action": "<name>", "data": {<fields>}}'
    if action == "muse_manage" and isinstance(args.get("action"), str):
        near = difflib.get_close_matches(args["action"], FIELDS, n=3)
        fix += f"; `action` {args['action']!r} is not a muse_manage action (" + ("did you mean " + " / ".join(near) if near else "choose one of: " + ", ".join(FIELDS)) + ")"
    return fail(action, ", ".join(extra[:8]), "is not allowed beside action/data", expected="only `action` and `data` at the top level",
                got={k: args[k] for k in extra[:3]}, fix=fix)


def plain(exc, action, data):
    """Plain ValueError from deep code: keep its authored sentence, name the field when it can be told."""
    text, data = str(exc), data if isinstance(data, dict) else {}
    tiers = {**REQUIRED.get(action, {}), **LATE.get(action, {})}
    field = None
    if text.startswith("Expected nonempty text"):
        field = next((k for k, v in tiers.items() if v == "text" and not (isinstance(data.get(k), str) and data[k].strip())), None)
        if field:
            return fail(action, "data." + field, "must be non-empty text", expected=text.replace("Expected ", ""), got=data.get(field, NOTHING),
                        fix=f"set data.{field} to the required text")
    elif "Timestamps must include" in text or text.startswith("Invalid isoformat"):
        return fail(action, "a timestamp in data", "is malformed", expected=KIND_WORDS["time"],
                    fix="send every timestamp (verified_at, expires, deadline, started_at, ...) as epoch seconds or ISO-8601 with a UTC offset")
    elif text.startswith("ID must be"):
        field = next((k for k in ("id", *sorted(data)) if (k == "id" or k.endswith("_id")) and isinstance(data.get(k), str)
                      and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", data[k])), None)
        if field:
            return fail(action, "data." + field, "is not a valid record id", expected="1-80 letters, digits, hyphens or underscores", got=data[field],
                        fix="use the id exactly as returned by Muse (context/content_read), not a title or path")
    return DomainError("invalid_argument", f"{action}: {text.rstrip('.')}. Resending the identical call will fail again; change the field value or "
                       "satisfy the precondition this message names first.", retryable=False,
                       **({"example": EXAMPLES[action]} if action in EXAMPLES else {}))


SUFFIX_CODES = {"invalid_argument", "size_limit", "permission_denied", "scope_closed", "not_found", "idempotency_conflict"}


def normalize(exc, action=None, data=None):
    """One boundary: no interpreter exception text reaches the model; every non-retryable error says so."""
    from .contracts import FIELDS
    if isinstance(exc, DomainError):
        message = str(exc)
        if exc.code in SUFFIX_CODES and "fail again" not in message and not exc.details.get("retryable"):
            exc = _carry(exc, DomainError(exc.code, message + " " + SAME_AGAIN, **exc.details))
        return exc
    if action not in FIELDS:
        return exc
    if isinstance(exc, (KeyError, TypeError, AttributeError, IndexError)):
        return _carry(exc, unexpected(exc, action, data))
    if isinstance(exc, ValueError):
        return _carry(exc, plain(exc, action, data))
    return exc


def _carry(old, new):
    if hasattr(old, "native_result"):
        new.native_result = old.native_result
    return new
