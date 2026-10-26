"""Single-tool field contracts, independent from prompts and UI."""

import re

from .content_io import DomainError
from .guidance import (EXAMPLES, ENUMS, IDENTIFIER_ACTIONS, KIND_WORDS, NOTHING, REQUIRED, KEY_RULE, REQUIRED_FIX, SAME_AGAIN, allowed_fields, fail, field_kind,
                       unknown_action, unsupported)
from .store import epoch

FIELDS = {
    "context": "section job window cursor limit id offset length revision part status",
    "status": "section job window cursor limit id offset length revision part status",
    "conversation_read": "session_id revision offset read_only since until descriptor",
    "content_read": "kind id resource offset length revision cursor limit topic query part",
    "content_write": "kind id resource body expected_revision request_id signal_id",
    "memory_feedback_review": "receipt_id decision reason expected_revision request_id",
    "installation_prepare": "operation signal_id expected_revision request_id",
    "goal_create": "signal_id title description completion_criteria id parent_id deadline external_ref owner no_fade",
    "goal_update": "id progress note status owner waiting_user blocked next_check_at expected_revision research_review_at description completion_criteria no_fade",
    "interest_record": "signal_id title id expires rationale",
    "idea_add": "title rationale goal_id sources expires",
    "feedback": "kind id action signal_id until",
    "preferences": "start_hour end_hour ordinary_per_day time_sensitive_per_day blocked_topics feed_brief deliver weather_context goal_dormant_days goal_fade_days signal_id",
    "notification_add": "event_key message rationale sources verified_at expires goal_id interest_id priority priority_reason issue_key source_snapshot supersedes signal_id watch_id final_result topic",
    "notification_refresh": "id verified_at sources message priority priority_reason source_snapshot",
    "notification_review": "id conversation_revisions verdict reason",
    "notification_resolve": "id verified_at sources reason",
    "notification_prepare": "id",
    "notification_queue": "id",
    "notification_handoff": "id",
    "watch_create": "goal_id signal_id schedule prompt stop_condition expires",
    "watch_stop": "job_id",
    "feed_add": "event_key title content sources why goal_id interest_id",
    "feed_list": "query topic cursor limit",
    "feed_update": "id signal_id feedback position delete title content sources",
    "calendar_window": "mode",
    "calendar_view": "window sources discovery_complete",
    "source_check": "source started_at status scope summary expected_resources checked_resources cursor query_window observed_at source_id alias_reason",
    "operations_read": "through",
    "operations_batch": "",
    "operations_evidence": "receipt ref offset length limit session_id job_id since until",
    "operations_complete": "token summary",
    "operations_commit": "batch reviews",
    "finding_record": "id evidence summary impact status verification disclosure no_notice_reason",
    "review_complete": "job up_to through summary goal_id stop_condition source_check_ids window",
    "run_complete": "outcome summary gaps",
    "delegate": "goal_id goal tasks context output_schema images action subagent_id message",
    "research_start": "items goal_id context",
    "research_status": "id",
    "forget": "kind object_kind id resource signal_id",
}
CAS = {
    "preferences": "preferences",
    "feed_update": "feed",
    "feedback": None,
    "notification_refresh": "notice",
    "notification_review": "notice",
    "notification_resolve": "notice",
    "source_check": "source_check",
    "finding_record": "finding",
    "forget": None,
}
CREATES = {
    "interest_record",
    "idea_add",
    "notification_add",
    "feed_add",
    "watch_create",
}


# Optional replay keys for actions without an existing object/content journal.
REPLAY_ACTIONS = {'delegate', 'research_start', 'research_status', 'watch_stop',
                  'operations_read', 'operations_batch', 'operations_complete', 'operations_commit',
                  'review_complete', 'run_complete', 'notification_prepare', 'notification_queue',
                  'notification_handoff'}


CALENDAR_RESOURCES = ("calendars:rolling-30d", "calendars:natural-month")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")


def _type_error(action, name, kind, value):
    path = "data." + name
    expected = KIND_WORDS[kind]
    fix = {"text": f"pass {path} as plain text", "int": f"pass {path} as a bare number, not text", "bool": f"pass {path} as true or false (no quotes)",
           "list": f"pass {path} as a JSON array, e.g. [\"a\", \"b\"]", "time": f"pass {path} as epoch seconds or ISO-8601 text with a UTC offset",
           "object": f"pass {path} as a JSON object, not JSON-encoded text"}[kind]
    if isinstance(value, str) and kind in {"list", "object"}:
        fix += "; this value is text, so decode it first and send the structure itself"
    if kind == "list" and not isinstance(value, (str, dict, type(None))):
        fix = f"wrap it: {path} = [value]"
    return fail(action, path, "has the wrong type", expected=expected, got=value, fix=fix)


def _query_window_error(action, value, source):
    resource = source.get("resource") if isinstance(source, dict) else None
    if resource in CALENDAR_RESOURCES:
        fix = ("pass the calendar_window result as a JSON object (not JSON text), copying data.query_window and data.observed_at unchanged "
               "from the matching calendar_view coverage[].source_check")
        expected = "the unchanged object returned by calendar_window"
    else:
        fix = ("this is not a calendar source: delete data.query_window and data.observed_at and write the time range into data.scope "
               "(text, e.g. \"unread mail from the last 24 hours\"); only calendar_view coverage uses query_window")
        expected = "no query_window for non-calendar sources (calendar sources: the calendar_window object)"
    return fail(action, "data.query_window", "is not valid here", expected=expected, got=value, fix=fix)


def _check_source(action, data):
    source = data.get("source")
    if not isinstance(source, dict):
        raise _type_error(action, "source", "object", source)
    missing = [k for k in ("connection", "account", "resource") if k not in source]
    extra = sorted(set(source) - {"connection", "account", "resource"})
    if missing or extra:
        raise fail(action, "data.source", "needs exactly connection, account and resource",
                   expected="{\"connection\": <tool/CLI integration id>, \"account\": <account or device id, \"default\" if none>, \"resource\": <stable collection such as mail:inbox>}",
                   got=source, fix=(("add " + ", ".join("data.source." + k for k in missing)) if missing else "") + ("; " if missing and extra else "")
                   + (("remove " + ", ".join("data.source." + k for k in extra) + " (scan dates and window text go in data.scope)") if extra else ""))
    for key in ("connection", "account", "resource"):
        if not isinstance(source[key], str) or not source[key].strip() or len(source[key]) > 500:
            raise fail(action, "data.source." + key, "must be stable non-empty text of at most 500 characters", got=source[key],
                       fix=f"set data.source.{key} to the actual tool/account/collection id; never a date, tick id or changing prose")
    if "query_window" in data and (source["resource"] not in CALENDAR_RESOURCES or not isinstance(data["query_window"], dict)):
        raise _query_window_error(action, data["query_window"], source)
    if "query_window" in data and "observed_at" not in data:
        raise fail(action, "data.observed_at", "is required together with data.query_window", expected=KIND_WORDS["time"],
                   fix="copy data.observed_at from the matching calendar_view coverage[].source_check")


def tidy(action, data):
    """Drop what a source_check repeats redundantly and fill what its calendar window already implies, so only the essential fields must be right."""
    source = data.get("source") if action == "source_check" and isinstance(data, dict) else None
    if not isinstance(source, dict):
        return data
    out = {k: v for k, v in data.items() if not (k in ("connection", "account", "resource") and v == source.get(k))}
    window = out.get("query_window")
    if source.get("resource") in CALENDAR_RESOURCES and isinstance(window, dict) and isinstance(window.get("observed_at"), str):
        for name in ("started_at", "observed_at"):
            if out.get(name) is None:
                out[name] = window["observed_at"]
    return out


def validate(action, data):
    if action not in FIELDS:
        raise unknown_action(action)
    if not isinstance(data, dict):
        raise fail(action, "data", "must be an object", expected="a JSON object holding this action's fields", got=data,
                   fix=f"send {{\"action\": \"{action}\", \"data\": {{...fields...}}}}; use {{}} when no fields are needed, and never JSON-encode data into text")
    allowed = allowed_fields(action)
    if set(data) - allowed:
        raise unsupported(action, data, allowed)
    for name, value in data.items():
        kind = field_kind(action, name)
        kind = "text" if kind == "str" else kind
        if name == "query_window":
            continue  # Its type depends on the source resource; _check_source reports it.
        if kind == "int" and type(value) is not int or kind == "bool" and type(value) is not bool or \
                kind == "list" and not isinstance(value, list) or kind == "object" and not isinstance(value, dict) or \
                kind == "text" and value is not None and not isinstance(value, str):
            raise _type_error(action, name, kind, value)
        if kind == "time" and value is not None:
            try:
                epoch(value)
            except (ValueError, OverflowError, OSError, TypeError):
                raise fail(action, "data." + name, "is not a usable timestamp", expected=KIND_WORDS["time"], got=value,
                           fix=f"send data.{name} as epoch seconds or ISO-8601 text that includes a UTC offset or Z (a bare date or local time without offset is rejected)")
        if (action, name) in ENUMS and value not in ENUMS[(action, name)] and not (
                name == "job" and isinstance(value, str) and value.startswith("watch-")):  # Owned watch Crons check in as watch-<id>.
            raise fail(action, "data." + name, "is not an accepted value", expected="one of " + ", ".join(ENUMS[(action, name)]), got=value,
                       fix=f"set data.{name} to exactly one of the listed values (case-sensitive)")
        if name == "id" and action in IDENTIFIER_ACTIONS and isinstance(value, str) and not IDENTIFIER.fullmatch(value):
            raise fail(action, "data.id", "is not a valid record id", expected="1-80 letters, digits, hyphens or underscores, starting with a letter or digit",
                       got=value, fix="use the id exactly as returned by context/content_read (not a title, path or URL)")
    required = dict(REQUIRED.get(action, {}))
    if action == "forget" and data.get("kind") != "content":
        required["signal_id"] = "text"
    if action == "review_complete" and str(data.get("job", "")).startswith("watch-"):
        required.pop("up_to")  # A watch checkpoint names its goal/source checks, not a signal cursor.
    absent = [n for n, k in required.items() if data.get(n) is None or k == "text" and not data[n].strip()]
    if action == "source_check" and "source" in absent:
        present = [k for k in ("connection", "account", "resource") if k in data]
        raise fail(action, "data.source", "is required", expected="an object {connection, account, resource}",
                   fix="add data.source = {\"connection\": ..., \"account\": ..., \"resource\": ...}" + (" (those keys are only valid inside it)" if present else ""))
    if absent:
        raise fail(action, ", ".join("data." + n for n in absent), "is required but missing or empty" if len(absent) == 1 else "are required but missing or empty",
                   expected="; ".join(f"data.{n}: {KIND_WORDS[required[n]]}" for n in absent),
                   fix="; ".join(REQUIRED_FIX.get((action, n), f"add data.{n}") for n in absent)
                   + (" (empty or whitespace-only text counts as missing)" if any(required[n] == "text" for n in absent) else ""),
                   fields=["data." + n for n in absent])
    if action == "source_check":
        _check_source(action, data)
    if action == 'content_write' and isinstance(data.get('body'), str):
        if len(data['body']) > 60000 or len(data['body'].encode()) > 256 * 1024:
            raise fail(action, 'data.body', 'is too large', code='size_limit', expected='at most 60000 characters and 256 KiB',
                       got=f"{len(data['body'])} characters", fix='split the content into several pages/sections and write each with its own request_id')
    bounds = {'watch_create': {'prompt': 4000, 'stop_condition': 4000},
              'research_start': {'context': 8000}}
    common = {'title': 300, 'description': 8000, 'completion_criteria': 8000,
              'summary': 4000 if action == 'finding_record' else 2000,
              'reason': 2000, 'rationale': 2000, 'why': 2000, 'feedback': 2000,
              'progress': 2000, 'note': 2000, 'next_action': 2000, 'source_quote': 2000,
              'event_key': 500, 'issue_key': 500, 'priority_reason': 2000,
              'content': 60000, 'feed_brief': 12000, 'alias_reason': 1000}
    if action == 'goal_create':
        common['title'] = 200
    if action in {'notification_add', 'notification_refresh'}:
        common['message'] = 6000
    if action == 'source_check':
        common.update(scope=1000, cursor=4000)
    for name, maximum in {**common, **bounds.get(action, {})}.items():
        if isinstance(data.get(name), str) and len(data[name]) > maximum:
            raise fail(action, 'data.' + name, 'is too long', code='size_limit', expected=f'at most {maximum} characters',
                       got=f"{len(data[name])} characters", fix=f'shorten data.{name} to {maximum} characters or fewer (keep the essential facts; put long material in content/files and reference it)')
    if action == 'research_start' and isinstance(data.get('items'), list):
        if not 1 <= len(data['items']) <= 24:
            raise fail(action, 'data.items', 'has the wrong number of questions', code='size_limit', expected='1..24 text questions',
                       got=f"{len(data['items'])} items", fix='send 1-24 questions; split the rest into a later research_start')
        for index, item in enumerate(data['items']):
            if not isinstance(item, str) or not item.strip() or len(item) > 2000:
                raise fail(action, f'data.items[{index}]', 'must be one question as text', code='size_limit', expected='non-empty text of at most 2000 characters',
                           got=item, fix='make every item a single question string (not an object), at most 2000 characters')
    if action == 'review_complete' and 'window' in data and data.get('job') not in {'memory-upkeep', 'nightly-review'}:
        raise fail(action, 'data.window', 'is only valid for memory-upkeep and nightly-review', got=data['window'],
                   fix='delete data.window for this job; use it only with the window token returned by context(section=signals, job=...)')
    for name in ('window', 'revision', 'expected_revision', 'snapshot_revision', 'request_id'):
        if isinstance(data.get(name), str) and len(data[name]) > (128 if name == 'request_id' else 1024):
            raise fail(action, 'data.' + name, 'is too long', code='size_limit', expected=f"at most {128 if name == 'request_id' else 1024} characters",
                       got=f"{len(data[name])} characters", fix=f'send data.{name} exactly as issued by the previous result; do not edit or concatenate it')
    if action in {'context', 'status', 'content_read', 'feed_list'} and isinstance(data.get('cursor'), str) and len(data['cursor']) > 2048:
        raise fail(action, 'data.cursor', 'is too long', code='size_limit', expected='at most 2048 characters', got=f"{len(data['cursor'])} characters",
                   fix='send the next_cursor exactly as returned; do not edit it')
    for name, value in data.items():
        if (name.endswith('_id') or name in {'id', 'kind', 'section', 'job', 'resource'}) and isinstance(value, str) and len(value) > 512:
            raise fail(action, 'data.' + name, 'is too long', code='size_limit', expected='an identifier of at most 512 characters', got=f"{len(value)} characters",
                       fix=f'use the short id returned by Muse for data.{name}, not a title, path or body text')
    if action in CAS or action in CREATES or "request_id" in data:
        if (
            not isinstance(data.get("request_id"), str)
            or not 1 <= len(data["request_id"]) <= 128
        ):
            raise DomainError(
                "invalid_argument",
                f"{action}: request_id required for {action} (data.request_id): a new 1..128-character key per logical write. "
                + KEY_RULE.format(action=action) + " Reuse a key only to replay the identical call."
                + (" This action also needs data.expected_revision: the revision string from the object's context list row "
                   "or content_read, or \"absent\" for a new object." if action in CAS else "")
                + " " + SAME_AGAIN,
                field="data.request_id", expected="text of 1..128 characters", hint="add a fresh data.request_id", retryable=False,
                **({"example": EXAMPLES[action]} if action in EXAMPLES else {}),
            )
    # Other CAS actions get revision_required with current_revision from mutations.invoke.
    if action == "forget" and data.get("kind") == "content" and "expected_revision" not in data:
        raise DomainError(
            "revision_required", "forget(kind=content): Read the object through content_read first, then resend with data.expected_revision set to its revision "
            "(plus object_kind, id, signal_id and a new request_id)", field="data.expected_revision", retryable=False
        )
