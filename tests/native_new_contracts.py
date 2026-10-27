"""New actions via actual host registry/hooks and native Cron fixtures."""

import os
import sys
import tempfile
import json
import shutil
import time
import uuid
from pathlib import Path

os.environ["HERMES_DATA_DIR_SUFFIX"] = "-muse-isolated-test"
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), os.environ["HERMES_SOURCE"]]
with tempfile.TemporaryDirectory(prefix="muse-native-contracts-") as directory:
    home = Path(directory)
    os.environ["HERMES_HOME"] = str(home)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n"
    )
    shutil.copytree(
        root,
        home / "plugins/hermes-muse",
        ignore=shutil.ignore_patterns(".git", ".serena", "__pycache__"),
    )
    from hermes_cli.plugins import discover_plugins, invoke_hook
    from tools.registry import registry
    from model_tools import handle_function_call
    from cron.jobs import list_jobs, get_job, pause_job, create_job
    from hermes_muse.store import digest
    from hermes_state import SessionDB

    discover_plugins(force=True)
    rt = registry.get_entry("muse_manage").handler.__self__
    native = SessionDB(db_path=home / "state.db")
    native.create_session("origin", source="cli", chat_type="private")
    invoke_hook(
        "pre_llm_call",
        session_id="origin",
        turn_id="u",
        platform="cli",
        user_message="Research goal g and maintain current tools_note. Later disable Muse.",
    )

    def call(action, data=None, ok=True, sid="origin", task=None):
        from tool_client import transport

        result = json.loads(
            handle_function_call(
                "muse_manage",
                transport({"action": action, "data": data or {}}),
                session_id=sid,
                turn_id=rt.store.read("session", sid, {}).get("turn_id", "u"),
                tool_call_id="fixture-" + uuid.uuid4().hex,
                task_id=task,
                enabled_toolsets=["muse"],
            )
        )
        from tool_client import record

        record(action, result)
        assert result["ok"] is ok, result
        return result.get("result") if ok else result

    goal = call(
        "goal_create",
        {
            "id": "g",
            "title": "Fixture",
            "description": "Research fixture",
            "completion_criteria": "Verified artifact",
            "signal_id": "u",
        },
    )
    from tool_client import arguments

    native.append_message("origin", "user", "Research goal g using synthetic evidence")
    feed = call(
        "feed_add",
        arguments(
            rt,
            "feed_add",
            {
                "event_key": "native-article",
                "title": "Native article",
                "content": "Synthetic body",
                "sources": ["fixture:source"],
                "why": "Relevant to fixture",
                "goal_id": "g",
            },
        ),
    )
    assert call("feed_list")["items"][0]["id"] == feed["id"]
    notice = call(
        "notification_add",
        arguments(
            rt,
            "notification_add",
            {
                "event_key": "native-notice",
                "message": "Synthetic evidence",
                "rationale": "Fixture relevance",
                "sources": ["fixture:source"],
                "verified_at": time.time(),
                "expires": time.time() + 3600,
                "goal_id": "g",
            },
        ),
    )
    call(
        "notification_refresh",
        arguments(
            rt,
            "notification_refresh",
            {
                "id": notice["id"],
                "verified_at": time.time(),
                "sources": ["fixture:source"],
                "message": "Refreshed synthetic evidence",
            },
        ),
    )
    from tool_client import conversations
    recent = {sid: value["revision"] for sid,value in conversations(call).items() if value.get("total_text_chars")}
    call(
        "notification_review",
        arguments(
            rt,
            "notification_review",
            {
                "id": notice["id"],
                "conversation_revisions": recent,
                "verdict": "relevant",
                "reason": "Read current original fixture conversation",
            },
        ),
    )
    for alias in ("notification_prepare", "notification_queue", "notification_handoff"):
        assert call(alias, {"id": notice["id"]})["final_response"] == "[SILENT]"
    resolve = {"id": notice["id"], "verified_at": time.time() + 30, "sources": ["fixture:source"],
               "reason": "Fixture resolved without sending", "request_id": "resolve-fixture"}
    # Missing/integer revisions are answered with the exact kind=notice read and the revision string to send.
    missing = call("notification_resolve", resolve, ok=False)
    assert missing["code"] == "revision_required" and missing["read"] == {"action": "content_read", "data": {"kind": "notice", "id": notice["id"]}}, missing
    current = call("content_read", missing["read"]["data"])["revision"]
    assert missing["current_revision"] == current, missing
    integer = call("notification_resolve", {**resolve, "expected_revision": 3}, ok=False)
    assert integer["code"] == "invalid_argument" and integer["current_revision"] == current, integer
    assert "Notifications are kind=notice" in call("content_read", {"kind": "notification", "id": notice["id"]}, ok=False)["error"]
    # verified_at a few seconds ahead of the plugin clock is accepted (same skew as add/refresh).
    assert call("notification_resolve", {**resolve, "expected_revision": missing["current_revision"]})["status"] == "cancelled"
    scoped = call("content_read", {"kind": "goal", "id": "g"})
    data = {
        "kind": "goal_file",
        "id": "g",
        "resource": "files/result.md",
        "expected_revision": "absent",
        "request_id": "result",
        "body": "Verified synthetic artifact",
        "signal_id": "u",
    }
    first = call("content_write", data)
    assert call("content_write", data) == first
    assert (
        call("content_write", {**data, "body": "Changed"}, ok=False)["code"]
        == "idempotency_conflict"
    )
    assert (
        call("content_write", {**data, "request_id": "foreign"}, ok=False, sid="other")[
            "code"
        ]
        == "permission_denied"
    )
    assert (
        call("content_read", {"kind": "goal", "id": "g", "home": str(home)}, ok=False)[
            "code"
        ]
        == "invalid_argument"
    )
    receipts = [
        ("memory", {"action": "add"}, {"success": True}, "saved"),
        (
            "memory",
            {"action": "replace"},
            {"success": True, "staged": True, "pending_id": "fixture"},
            "staged",
        ),
        (
            "skill_manage",
            {"action": "edit"},
            {"success": False, "message": "all touched skills rolled back"},
            "failed",
        ),
        (
            "hindsight_retain",
            {},
            "Memory stored successfully.",
            "submitted_unconfirmed",
        ),
    ]
    receipts.extend([
        ("memory", {"action": "add"}, {"success": True, "message": "Entry already exists (no duplicate added)"}, "duplicate"),
        ("memory", {"action": "add"}, "unparseable", "unknown"),
        ("memory", {"action": "remove"}, {"success": True, "proposal_staged": True}, "staged"),
        ("skill_manage", {"action": "edit"}, {"success": True, "staged": True, "pending_id": "skill-pending"}, "staged"),
        ("skill_manage", {"action": "create"}, {"success": False, "message": "already exists"}, "failed"),
        ("hindsight_retain", {}, {"success": True, "items_count": 0}, "submitted_unconfirmed"),
    ])
    for n, (tool, args, result, state) in enumerate(receipts):
        invoke_hook(
            "post_tool_call",
            tool_name=tool,
            args=args,
            result=result,
            session_id="origin",
            turn_id="u",
            tool_call_id="receipt-" + str(n),
            status="success",
        )
        row = rt.store.read(
            "memory_feedback", digest("memory-feedback:origin:receipt-" + str(n))
        )
        assert row["classified_state"] == state, row
        reviewed = call(
            "memory_feedback_review",
            {
                "receipt_id": row["id"],
                "decision": "inform" if state == "saved" else "defer",
                "reason": "Actual synthetic receipt",
                "expected_revision": row["revision"],
                "request_id": "review-" + str(n),
            },
        )
        assert reviewed["classified_state"] == state
        call(
            "memory_feedback_review",
            {
                "receipt_id": row["id"],
                "decision": "inform",
                "reason": "Wrong origin",
                "expected_revision": row["revision"],
                "request_id": "other-" + str(n),
            },
            ok=False,
            sid="other",
        )
    invoke_hook(
        "on_skill_lifecycle",
        action="updated",
        skill_name="fixture",
        session_id="origin",
    )
    assert len(rt.store.all("memory_feedback")) == len(receipts)
    reply = "I saved the requested preference; staged changes still await approval."
    mid = native.append_message("origin", "assistant", reply)
    invoke_hook(
        "post_llm_call",
        session_id="origin",
        turn_id="u",
        assistant_response=reply,
        conversation_history=[{"role": "assistant", "content": reply, "_row_id": mid}],
    )
    saved = rt.store.read("memory_feedback", digest("memory-feedback:origin:receipt-0"))
    assert saved["reply_state"] == "recorded_unconfirmed"
    watch = call(
        "watch_create",
        {
            "goal_id": "g",
            "signal_id": "u",
            "schedule": "every 30m",
            "prompt": "Inspect synthetic source",
            "stop_condition": "Fixture complete",
            "request_id": "watch",
        },
    )
    from cron.executions import create_execution, mark_execution_running

    attempt = create_execution(watch["id"], source="test")
    mark_execution_running(attempt["id"])
    task = "cron:" + watch["id"] + ":" + attempt["id"]
    source_args = {
        "source": {
            "connection": "fixture",
            "account": "fixture",
            "resource": "watch",
        },
        "started_at": time.time(),
        "status": "checked",
        "scope": "fixture",
        "summary": "Synthetic source read",
        "expected_resources": ["fixture"],
        "checked_resources": ["fixture"],
        "expected_revision": "absent",
        "request_id": "source",
    }
    # A missing revision is answered with the value to resend, not a content_read detour.
    missing = call("source_check", {k: v for k, v in source_args.items() if k != "expected_revision"},
                   ok=False, sid="watch-session", task=task)
    assert missing["code"] == "revision_required" and missing["current_revision"] == "absent", missing
    assert missing["source_id"].startswith("source-"), missing
    no_key = call("source_check", {k: v for k, v in source_args.items() if k != "request_id"},
                  ok=False, sid="watch-session", task=task)
    assert "request_id required for source_check" in no_key["error"] and "expected_revision" in no_key["error"], no_key
    source = call("source_check", source_args, sid="watch-session", task=task)
    listed = next(r for r in call("context", {"section": "source_check", "limit": 100})["items"] if r["id"] == source["id"])
    assert listed["source"] == source_args["source"] and listed["revision"], listed
    unknown = call("context", {"section": "preferences"}, ok=False)
    assert "Valid sections" in unknown["error"] and "content_read" in unknown["error"], unknown
    # Through the real registry: misplaced/mistyped arguments say where the field belongs and never leak interpreter text.
    misplaced = call("source_check", {"account": "a", "connection": "c"}, ok=False)
    assert "move data.account, data.connection into data.source" in misplaced["error"] and misplaced["field"] == "data.account, data.connection", misplaced
    typed = call("source_check", {**{k: v for k, v in source_args.items() if k != "query_window"}, "query_window": "last 24h"}, ok=False)
    assert "data.query_window" in typed["error"] and "data.scope" in typed["error"] and typed["retryable"] is False, typed
    assert "data.window" in call("calendar_view", {}, ok=False)["error"]
    key = rt.host.manifest()["jobs"][watch["id"]]["key"]
    legacy = rt.host.manifest()
    saved_condition = legacy["jobs"][watch["id"]].pop("stop_condition")
    rt.host.save_manifest(legacy)
    legacy_error = call("review_complete", {
        "job": key, "goal_id": "g", "summary": "Cannot invent a legacy condition",
        "stop_condition": saved_condition, "source_check_ids": [source["id"]]},
        ok=False, sid="watch-session", task=task)
    assert "no saved watch condition" in legacy_error["error"], legacy_error
    legacy["jobs"][watch["id"]]["stop_condition"] = saved_condition
    rt.host.save_manifest(legacy)
    missing_condition = call("review_complete", {
        "job": key, "goal_id": "g", "summary": "Missing condition", "source_check_ids": [source["id"]]},
        ok=False, sid="watch-session", task=task)
    assert "data.stop_condition" in missing_condition["error"], missing_condition
    reviewed = call(
        "review_complete",
        {
            # The host already owns this Cron task; its job key is derivable.
            "goal_id": "g",
            "summary": "Actual synthetic goal and source checked",
            "stop_condition": "Fixture complete",
            "source_check_ids": [source["id"]],
        },
        sid="watch-session",
        task=task,
    )
    assert reviewed["complete"], reviewed
    completed = call(
        "run_complete",
        {"outcome": "complete", "summary": "Watch source check finished"},
        sid="watch-session",
        task=task,
    )
    assert completed["status"] == "ready"
    from agent.turn_finalizer import apply_llm_output_transform
    from types import SimpleNamespace

    apply_llm_output_transform(
        SimpleNamespace(session_id="watch-session", model="fixture", platform="cron"),
        "[SILENT]",
        turn_id="watch-end",
    )
    assert rt.store.read("run", attempt["id"])["status"] == "complete"
    call("watch_stop", {"job_id": watch["id"]})
    assert not get_job(watch["id"])["enabled"]
    native.append_message(
        "origin",
        "assistant",
        tool_calls=[
            {"id": "fixture-read", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    )
    native.append_message(
        "origin",
        "tool",
        json.dumps({"success": True, "content": "Synthetic evidence"}),
        tool_name="read_file",
        tool_call_id="fixture-read",
    )
    page = call("operations_read")
    assert page["available"] and not page["gaps"], page
    refs = [row["ref"] for row in page["events"] + page["executions"]]
    assert refs
    finding = call(
        "finding_record",
        arguments(
            rt,
            "finding_record",
            {
                "evidence": refs[:1],
                "summary": "Synthetic finding for action acceptance",
                "impact": "Fixture-only demonstration",
                "expected_revision": "absent",
            },
        ),
    )
    assert finding["status"] == "open"
    call(
        "operations_complete",
        {
            "token": page["token"],
            "summary": "Inspected the synthetic user intent, call and persisted result",
        },
    )
    ids = [j["id"] for j in list_jobs(include_disabled=True) if j["id"] != watch["id"]]
    pause_job(ids[0], reason="Preexisting user pause")
    assert len(rt.host.manifest()["fixed"]) == 6

    unrelated = create_job(
        prompt="User subscription",
        schedule="every 1h",
        name="user-subscription",
        deliver="local",
    )
    install = call("content_read", {"kind": "installation", "id": "current"})
    invoke_hook(
        "pre_llm_call",
        session_id="origin",
        turn_id="disable-now",
        platform="cli",
        user_message="Please disable Muse now.",
    )
    prep = {
        "operation": "disable",
        "signal_id": "disable-now",
        "expected_revision": install["revision"],
        "request_id": "prepare",
    }
    from unittest.mock import patch
    failed_once = [False]
    def interrupted_pause(key, **kwargs):
        if not failed_once[0]:
            failed_once[0] = True
            raise OSError("synthetic native pause interruption")
        return pause_job(key, **kwargs)
    with patch("cron.jobs.pause_job", side_effect=interrupted_pause):
        partial = call("installation_prepare", prep)
    assert partial["partial"] and len(partial["remaining_ids"]) == 1
    assert len(partial["paused_owned_ids"]) == 4
    result = call("installation_prepare", prep)
    assert (
        result["requires_host_action"]
        and not result["partial"]
        and ids[0] in result["unchanged_ids"]
    )
    assert get_job(unrelated["id"])["enabled"]
    assert len(result["paused_owned_ids"]) == 5
    assert (
        call("installation_prepare", prep)["paused_owned_ids"]
        == result["paused_owned_ids"]
    )
    assert all(not get_job(key)["enabled"] for key in ids)
    rt.host.initialize()
    assert all(not get_job(key)["enabled"] for key in ids)
    from hermes_muse.runtime import ACTIONS

    for action in ACTIONS:
        assert (
            call(action, {"profile": "forged"}, ok=False)["code"] == "invalid_argument"
        )
    native.close()
    rt.close()
    from tool_client import close_containers
    close_containers()
    print(
        json.dumps(
            {
                "native_new_contracts": "PASS",
                "registry": True,
                "content_cas_replay": True,
                "profile_path_injection_rejected": True,
                "real_hook_memory_classes": len({r[3] for r in receipts}),
                "real_hook_receipt_cases": len(receipts),
                "reply_record_not_delivery": True,
                "cross_session_rejected": True,
                "watch_run_completed": True,
                "unknown_fields_rejected_actions": len(ACTIONS),
                "prepare_paused": 5,
                "previous_user_pause_preserved": True,
                "user_subscription_untouched": True,
                "no_model_calls": True,
            }
        )
    )
