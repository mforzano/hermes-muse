"""Real Hermes integration, isolated profiles; no models or live channels used.

Run with a Hermes interpreter: python tests/native_smoke.py
Set HERMES_SOURCE to a public checkout to test that source with the same interpreter.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
if os.environ.get("HERMES_SOURCE"):
    sys.path.insert(0, os.environ["HERMES_SOURCE"])
    os.environ["PYTHONPATH"] = os.environ["HERMES_SOURCE"] + os.pathsep + os.environ.get("PYTHONPATH", "")


def _close_host_logging():
    # Windows cannot remove scratch profiles while native log lock handles
    # remain open. Drain asynchronous logging before closing its handlers.
    import logging
    module = sys.modules.get("hermes_logging")
    drain = getattr(module, "drain_log_queue", None)
    if callable(drain):
        drain()
    logging.shutdown()


def main():
    from contextlib import ExitStack
    with tempfile.TemporaryDirectory(prefix="hermes-muse-integration-") as temporary, ExitStack() as cleanup:
        if os.name == "nt":
            cleanup.callback(_close_host_logging)
        base = Path(temporary).resolve()
        homes = [base / "a", base / "b"]
        for home in homes:
            home.mkdir()
            (home / "config.yaml").write_text("plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n")
            (home / "SOUL.md").write_text("Keep this identity exactly.\n")
            (home / "memories").mkdir()
            for name in ("USER.md", "MEMORY.md"):
                (home / "memories" / name).write_text("Existing private information.\n")
            shutil.copytree(PROJECT, home / "plugins/hermes-muse", ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", ".hermes-core"))
        # Bind a scratch home before importing any Hermes module.
        os.environ["HERMES_HOME"] = str(homes[0])
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.plugins import discover_plugins, get_plugin_manager, render_system_prompt_sections
        from tools.skills_tool import skill_view
        from tools.registry import registry
        from cron.jobs import list_jobs, pause_job, get_job
        from cron.scheduler_script import _run_job_script
        from cron.scheduler_prompt import _parse_wake_gate
        from hermes_muse.host import HermesHost, profile_scope
        from hermes_muse.store import Store
        first_ids = None
        for visit, index in enumerate((0, 1, 0)):
            home = homes[index]
            token = set_hermes_home_override(home)
            try:
                discover_plugins(force=True)
                manager = get_plugin_manager()
                loaded = manager._plugins.get("hermes-muse")
                assert loaded and loaded.enabled, repr(loaded)
                jobs = list_jobs(include_disabled=True)
                assert all(j.get("workdir") is None for j in jobs), jobs
                assert len(jobs) == 6, [(j["name"], j["id"]) for j in jobs]
                for key, schedule in (("weekly-governance-review", "15 21 * * 0"),
                                      ("monthly-system-audit", "40 10 1 * *")):
                    job = next(j for j in jobs if j["name"] == "muse-" + key)
                    assert job["deliver"] == "bot-chat", job
                    assert schedule in str(job["schedule"]), job
                    assert "hermes-muse:companion" in str(job), job
                    ok, gate = _run_job_script("hermes-muse-" + key + ".py", workdir=get_job(job["id"])["workdir"])
                    assert ok and _parse_wake_gate(gate), (ok, gate)
                # Empty local state is not evidence that connections or native memory have no changes.
                for key in ("proactive-watch", "memory-upkeep", "nightly-review", "feed-pulse"):
                    job = next(j for j in jobs if j["name"] == "muse-" + key)
                    ok, gate = _run_job_script("hermes-muse-" + key + ".py", workdir=get_job(job["id"])["workdir"])
                    assert ok and _parse_wake_gate(gate), (key, ok, gate)
                assert not any("skill-audit" in j["name"] for j in jobs)
                ids = {j["id"] for j in jobs}
                if index == 0 and first_ids is not None:
                    assert first_ids == ids, "Reload duplicated jobs"
                    assert not get_job(next(iter(first_ids)))["enabled"], "Reload silently resumed a paused job"
                if index == 0 and first_ids is None:
                    first_ids = ids
                    pause_job(next(iter(ids)), reason="user paused")
                prompt = render_system_prompt_sections({"session_id": "integration"})
                rendered = str(prompt)
                expected_prompt = (PROJECT / "prompts/system.md").read_text().replace("{{MUSE_HOME}}", str(home / "muse")).replace("{{HERMES_HOME}}", str(home))
                assert any(section.content == expected_prompt.strip() for section in prompt), "The host clipped part of the shipped system instructions"
                assert "muse_manage" in str(rendered), rendered
                assert str(home / "muse") not in str(rendered), "Prompts must use object IDs"
                assert str(homes[1 - index] / "muse") not in str(rendered)
                skill = skill_view("hermes-muse:companion")
                assert "Goal" in str(skill) or "goal" in str(skill), str(skill)
                briefing = skill_view("hermes-muse:companion", file_path="references/briefings.md")
                assert (PROJECT / "skills/companion/references/briefings.md").read_text().strip() in json.loads(briefing)["content"]
                entry = registry.get_entry("muse_manage")
                assert entry, "Plugin tool absent"
                def call_registered(args, **kwargs):
                    from tool_client import transport, record
                    raw = entry.handler(transport(args), **kwargs)
                    record(args["action"], json.loads(raw))
                    return raw
                status = json.loads(call_registered({"action": "context"}))
                assert status["ok"], status
                assert status["result"]["sections"] and "workspace" not in status["result"]
                # Exercise the actual native finalizer with the registered plugin hook.
                from types import SimpleNamespace
                import time, uuid
                from cron.executions import create_execution, mark_execution_running, finish_execution
                from agent.turn_finalizer import apply_llm_output_transform
                runtime = entry.handler.__self__
                if visit < 2:
                    from native_calendar_smoke import run as calendar_checks
                    calendar_checks(entry, runtime)
                from agent.subagent_lifecycle import bind_subagent_parent
                from unittest.mock import patch
                from hermes_muse.research import start as start_research
                parent = SimpleNamespace(valid_tool_names={"delegate_task"})
                with bind_subagent_parent(parent), patch("tools.delegate_tool.delegate_task", return_value=json.dumps({"status": "dispatched", "delegation_id": "isolated-native"})) as dispatch:
                    batch = start_research(runtime.store, runtime.ctx, {"items": ["Check fixture A", "Check fixture B"]})
                    assert batch["total"] == 2
                    assert dispatch.call_args.kwargs["parent_agent"] is parent
                    assert dispatch.call_args.kwargs["background"] is True
                notice = runtime.service.notification_add({"event_key": uuid.uuid4().hex, "message": "Verified test body.",
                    "rationale": "Isolated native test", "sources": ["test fixture"], "verified_at": time.time(),
                    "expires": time.time() + 3600, "priority": "urgent"})
                patrol = runtime.host.manifest()["fixed"]["proactive-watch"]
                attempt = create_execution(patrol, source="test")
                mark_execution_running(attempt["id"])
                # Real runtime ownership decides routing, never model-provided data.
                from tools.delegate_tool_config import _resolve_child_runtime, _resolve_delegation_credentials
                parent.model, parent.provider = "fixture-luna", "custom"
                parent.requested_provider, parent.base_url = "fixture-provider", "https://fixture.invalid/v1"
                parent.api_mode, parent.api_key = "chat_completions", "fixture-not-a-secret"
                with bind_subagent_parent(parent), patch("hermes_cli.config.load_config_readonly", return_value={"delegation": {"model": "fixture-sol"}}), patch("tools.delegate_tool.delegate_task", autospec=True,
                        return_value=json.dumps({"results": [{"status": "completed"}]})) as dispatch:
                    owned = json.loads(call_registered({"action": "delegate", "data": {"goal": "Inspect fixture evidence"}},
                        task_id="cron:" + patrol + ":" + attempt["id"]))
                    assert owned["ok"], owned
                    assert "credentials_cfg" not in dispatch.call_args.kwargs
                    # Public registry receives the parent; native routing honors delegation config.
                    assert dispatch.call_args.kwargs["parent_agent"] is parent
                    ordinary = json.loads(call_registered({"action": "delegate", "data": {"goal": "Read fixture"}},
                        task_id="cron:unrelated:unknown"))
                    assert ordinary["ok"], ordinary
                    assert "credentials_cfg" not in dispatch.call_args.kwargs
                    assert dispatch.call_args.kwargs["background"] is True
                    forged = json.loads(call_registered({"action": "delegate", "data": {"goal": "x", "owned_cron": True}}))
                    assert not forged["ok"], forged
                prepared = json.loads(call_registered({"action": "notification_prepare", "data": {"id": notice["id"]}},
                    task_id="cron:" + patrol + ":" + attempt["id"], session_id="native-patrol"))["result"]
                agent = SimpleNamespace(session_id="native-patrol", model="test", platform="cron")
                final, changed, _ = apply_llm_output_transform(agent, "Status: dispatching", turn_id="native-turn")
                assert changed and final == prepared["final_response"] and "Verified test body." in final
                # Compare our read-only receipt lookup with the actual host producer key.
                from cron.scheduler_delivery import _deliver_to_bot_chat
                from hermes_muse.host import bot_receipt_key
                with patch("tools.bot_live_delivery.read_delivery_result", return_value=None), \
                     patch("cron.bot_chat_delivery.read_pending", return_value=None), \
                     patch("tools.bot_live_delivery.find_canonical_live_owner", return_value={"test": True}), \
                     patch("tools.bot_live_delivery.deliver_to_live_owner", side_effect=lambda home, owner, message, **kw: {"status": "queued", "message": message}) as deliver:
                    _deliver_to_bot_chat({"id": patrol, "name": "muse-proactive-watch", "execution_id": attempt["id"]}, final, "")
                    assert deliver.call_args.kwargs["delivery_id"] == bot_receipt_key(home, patrol, attempt["id"])
                context = json.loads(call_registered({"action": "context"}, task_id="cron:" + patrol + ":" + attempt["id"]))["result"]
                assert context["job"] == "proactive-watch" and "signal_reviews" in context
                finish_execution(attempt["id"], success=True, delivery_outcome="delivered")
                runtime.host.reconcile()
                assert runtime.store.read("notification", notice["id"])["status"] == "sent"
                ok, gate = _run_job_script("hermes-muse-memory-upkeep.py", workdir=next(j for j in list_jobs(True) if j["name"] == "muse-memory-upkeep")["workdir"])
                assert ok and _parse_wake_gate(gate), (ok, gate)
                assert (home / "SOUL.md").read_text() == "Keep this identity exactly.\n"
                assert (home / "memories/USER.md").read_text() == "Existing private information.\n"
                assert (home / "memories/MEMORY.md").read_text() == "Existing private information.\n"
                # A genuine host-owned governance run receives the same Bot contract,
                # while its recommendations remain evidence rather than user intent.
                weekly = runtime.host.manifest()["fixed"]["weekly-governance-review"]
                review_attempt = create_execution(weekly, source="test")
                mark_execution_running(review_attempt["id"])
                review_context = json.loads(call_registered({"action": "context"},
                    task_id="cron:" + weekly + ":" + review_attempt["id"], session_id="native-review"))
                assert review_context["ok"]
                staged = runtime.service.notification_add({"event_key": uuid.uuid4().hex, "message": "A future patrol candidate.",
                    "rationale": "Isolated test", "sources": ["fixture"], "verified_at": time.time(),
                    "expires": time.time() + 3600, "priority": "urgent"})
                report_candidate = json.loads(call_registered({"action": "notification_prepare", "data": {"id": staged["id"]}},
                    task_id="cron:" + weekly + ":" + review_attempt["id"], session_id="native-review"))["result"]
                assert report_candidate["status"] == "pending" and report_candidate["final_response"] == "[SILENT]"
                assert not runtime.store.read("notification", staged["id"]).get("execution_id")
                review_agent = SimpleNamespace(session_id="native-review", model="test", platform="cron")
                report, changed, _ = apply_llm_output_transform(review_agent, "A verified review; proposed changes are not applied.", turn_id="review-turn")
                assert changed and report.startswith("[Hermes Muse internal delivery]")
                assert report.endswith("A verified review; proposed changes are not applied.")
                assert "The user is the audience" in report
                finish_execution(review_attempt["id"], success=True, delivery_outcome="delivered")
                if index == 1:
                    # Native read-only history must include completed answers, not just requests.
                    from hermes_state import SessionDB
                    from hermes_muse.conversation import read_conversation
                    native_db = SessionDB(db_path=home / "state.db")
                    native_db.create_session("native-history", source="cli", chat_type="private")
                    native_db.append_message("native-history", "user", "Please diagnose this screenshot.")
                    native_db.append_message("native-history", "assistant", "The database diagnosis is finished; no new screenshot is needed.")
                    native_db.create_session("group-history", source="telegram", chat_type="group")
                    native_db.append_message("group-history", "user", "Group-only private context")
                    native_db.append_message("native-history", "assistant", tool_calls=[{"id": "native-write", "function": {"name": "write_file", "arguments": '{"path":"goal.md"}'}}])
                    native_db.append_message("native-history", "tool", "Saved goal.md", tool_name="write_file", tool_call_id="native-write")
                    native_db.create_session("audit-child", source="delegate", parent_session_id="native-history", chat_type="private")
                    native_db.append_message("audit-child", "tool", '{"ok":false,"error":"Child failed to verify"}', tool_name="inspect", tool_call_id="child-call")
                    native_db.close()
                    runtime.store.write("session", "native-history", {"id": "native-history", "last_signal": time.time()})
                    audit_response = json.loads(call_registered({"action": "operations_read"}))
                    assert audit_response["ok"], audit_response
                    audit = audit_response["result"]
                    assert audit["available"] and not audit["gaps"], audit
                    assert {"native-history", "audit-child"} <= {e["session_id"] for e in audit["events"]}, audit
                    assert any(len(e["evidence_refs"]) >= 3 for e in audit["events"])
                    assert all(e["session_id"] != "group-history" for e in audit["events"])
                    wave = json.loads(call_registered({"action": "operations_batch"}))["result"]
                    receipt = runtime.store.read("meta", "operation_wave")["pages"][0]["receipt"]
                    from agent.delegation_context import delegated_child_context
                    with delegated_child_context("audit-worker"):
                        evidence = json.loads(call_registered({"action": "operations_evidence", "data": {"receipt": receipt}}))
                        assert evidence["ok"], evidence
                        forbidden = json.loads(call_registered({"action": "operations_commit", "data": {"batch": wave["batch"], "reviews": [{"slot": 1, "summary": "Must return to parent"}]}}))
                        assert not forbidden["ok"], "Audit workers cannot advance parent state"
                    committed = json.loads(call_registered({"action": "operations_commit", "data": {"batch": wave["batch"], "reviews": [{"slot": 1, "summary": "Parent verified native evidence and worker findings"}]}}))
                    assert committed["ok"] and not committed["result"]["more"], committed
                    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=SimpleNamespace(iteration_budget=SimpleNamespace(remaining=3))):
                        deferred = json.loads(call_registered({"action": "operations_batch"}))["result"]
                        assert deferred["deferred"] and not deferred["assignments"]
                    runtime.service.operations.complete({"token": audit["events"][0]["receipt"], "summary": "Native read-only operation evidence verified"})
                    history = read_conversation(home, "native-history", time.time() - 86400)
                    assert history["available"], history
                    assert any("diagnosis is finished" in m["text"] for m in history["messages"]), history
                    assert not read_conversation(home, "group-history", 0)["available"]
                    runtime.service.signal("history-user", "native-history", "Please diagnose this screenshot.")
                    stale = runtime.service.notification_add({"event_key": "screenshot-stale", "message": "Please send another screenshot.",
                        "rationale": "Regression fixture", "sources": ["fixture"], "verified_at": time.time(), "expires": time.time()+3600})
                    from tool_client import conversations
                    inspected = conversations(lambda action,data: json.loads(call_registered({'action':action,'data':data}))['result'])
                    assert inspected['native-history']['complete']
                    review = runtime.service.notification_review({"id": stale["id"], "conversation_revisions": {"native-history": history["revision"]},
                        "verdict": "resolved", "reason": "Native history confirms the completed diagnosis was already delivered"})
                    assert review["notification"]["status"] == "cancelled"
                    # Migrate only pristine old defaults; preserve explicit edits.
                    preference_path = home / "muse/PROACTIVE_PREFERENCES.md"
                    template = (PROJECT / "templates/PROACTIVE_PREFERENCES.md").read_text()
                    old_template = template.replace('"ordinary_per_day": null', '"ordinary_per_day": 1')
                    preference_path.write_text(old_template)
                    runtime.host.initialize()
                    assert runtime.service.preferences()["ordinary_per_day"] is None
                    preference_path.write_text(old_template + "\nMy explicit limit must remain.\n")
                    runtime.host.initialize()
                    assert runtime.service.preferences()["ordinary_per_day"] == 1
                # User edits to workspace templates survive a second initialization.
                prefs = home / "muse/TOOLS.md"
                prefs.write_text("User maintained source list.\n")
                HermesHost(Store(home), plugin_root=home / "plugins/hermes-muse").initialize()
                assert prefs.read_text() == "User maintained source list.\n"
                assert len(list_jobs(include_disabled=True)) == 6
            finally:
                reset_hermes_home_override(token)
        for home in homes:
            with profile_scope(home):
                assert get_plugin_manager().unload("hermes-muse")
                shutil.rmtree(home / "plugins/hermes-muse")
                ok, output = _run_job_script("hermes-muse-memory-upkeep.py", workdir=next(j for j in list_jobs(True) if j["name"] == "muse-memory-upkeep")["workdir"])
                assert ok and not _parse_wake_gate(output), (ok, output)
                assert (home / "muse/install.json").exists()
                # Host remove intentionally leaves native tasks for the documented cleanup flow.
                assert len(list_jobs(include_disabled=True)) == 6
        from native_cron_ownership import run as cron_ownership_checks
        cron_ownership_checks(base / "cron-ownership")
        from native_companion_smoke import run as companion_checks
        companion_checks(base / "companion")
        from native_followup_smoke import run as followup_checks
        followup_checks(base / "followup")
        from native_goal_intake import run as goal_intake_checks
        goal_intake_checks(base / "goal-intake")
        from native_goal_fade import run as goal_fade_checks
        goal_fade_checks(base / "goal-fade")
        from native_compatibility_smoke import run as compatibility_checks
        compatibility_checks(base / "compatibility")
        subprocess.run([sys.executable,str(PROJECT/'tests/native_simplification.py')],check=True,
                       env={**os.environ,'HERMES_DISABLE_LAZY_INSTALLS':'1'})
        from tool_client import close_containers
        close_containers()
        print("PASS: native registration, Skill, prompt, tools, six Cron jobs, real script gates, A->B->A isolation, idempotent reload, preserved native files and harmless removed-plugin launchers")


if __name__ == "__main__":
    main()
