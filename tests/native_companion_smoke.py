"""0.1.2 behavior plus Bot Chat delivery, exercised against real Hermes APIs."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def run(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n")
    from cron.jobs import get_job, list_jobs, remove_job
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_muse.host import profile_scope, enabled, cron_run
    from hermes_muse.runtime import Runtime
    from hermes_muse.store import stamp

    runtime = Runtime(object(), home)
    now = time.time()

    def call(operation, **data):
        from tool_client import arguments, transport
        if operation == "preferences": data.setdefault("signal_id", "new-user-choice")
        origin=runtime.store.read("signal", data.get("signal_id"), {}).get("session_id", "source-session")
        result = json.loads(runtime.handle(transport({"action": operation, "data": arguments(runtime, operation, data)}), session_id=origin))
        from tool_client import record
        record(operation,result)
        assert result["ok"], (operation, result)
        return result["result"]

    with profile_scope(home):
        runtime.host.initialize()
        from cron.jobs import get_due_jobs, pause_job, resume_job
        feed_id = runtime.host.manifest()["fixed"]["feed-pulse"]
        feed_schedule = get_job(feed_id)["schedule"]
        assert runtime.host.wake("feed-pulse")
        assert feed_id in {j["id"] for j in get_due_jobs()}, "An off-tick wake must survive native cron-expression repair"
        assert get_job(feed_id)["schedule"] == feed_schedule
        pause_job(feed_id, reason="User paused the fixture")
        assert not runtime.host.wake("feed-pulse")
        assert not get_job(feed_id)["enabled"]
        resume_job(feed_id)
        # Reviews persist summaries without manufacturing user activity or notices.
        for review in ("weekly-governance-review", "monthly-system-audit"):
            call("review_complete", job=review, up_to=now, summary="Evidence limited; simplify only after discussion.")
            assert call("status")["reviews"][review]["summary"].startswith("Evidence limited")
        assert not runtime.store.all("signal") and not runtime.store.all("notification")
        runtime.service.signal("user-choice", "source-session", "Track bicycles until I choose one.")
        parent_tokens = set_session_vars(session_id='source-session', chat_type='private', cron_session='')
        call('goal_create', id='bicycle', title='Choose a bicycle', description='Compare options', completion_criteria='User confirms a purchase', signal_id='user-choice')
        clear_session_vars(parent_tokens)
        tokens = set_session_vars(session_id="cron-session", chat_type="private", cron_session="1")
        try:
            interest = call("interest_record", id="cycling", title="Cycling", signal_id="user-choice")
            runtime.service.signal("new-user-choice", "source-session", "I am still interested in cycling.")
            renewed = call("interest_record", id="cycling", title="Cycling", signal_id="new-user-choice")
            assert renewed["expires"] > interest["expires"]
            invalid = json.loads(runtime.handle({"action": "interest_record", "data": {
                "id": "cycling", "title": "Cycling", "signal_id": "fabricated"}}))
            assert not invalid["ok"], "Restoring background work must not manufacture user evidence"
            repeated = json.loads(runtime.handle({"action": "interest_record", "data": {
                "id": "cycling", "title": "Cycling", "signal_id": "new-user-choice"}}))
            assert not repeated["ok"], "Rereading a signal must not renew an interest"
            call("preferences", ordinary_per_day=2)
            assert runtime.service.preferences()["ordinary_per_day"] == 2
            idea = call("idea_add", goal_id="bicycle", title="Compare nearby shops", rationale="Relevant")
            call("feedback", kind="interest", id=idea["id"], action="dismiss", signal_id="new-user-choice")
            article = call("feed_add", event_key="article", title="Bicycles", content="Evidence",
                           sources=["https://example.org/source"], why="Matches the goal")
            call("feed_update", id=article["id"], feedback="Useful", signal_id="new-user-choice")
            call("feed_update", id=article["id"], delete=True, signal_id="new-user-choice")
            assert not runtime.store.path(article["path"]).exists()
            runtime.service.signal("discard-choice", "source-session", "This separate topic can be forgotten.")
            call("interest_record", id="discard", title="Discard this", signal_id="discard-choice")
            call("forget", kind="interest", id="discard", signal_id="discard-choice")
            assert runtime.store.read("interest", "discard") is None
            owned = None
            from tool_client import arguments, transport
            refused = json.loads(runtime.handle({"action":"watch_create", "data": arguments(runtime, "watch_create", {
                "goal_id":"bicycle", "signal_id":"new-user-choice", "schedule":"every 5m",
                "prompt":"Read fixture", "stop_condition":"User chooses"})}, session_id="source-session"))
            assert not refused["ok"] and "allow_agent_scheduling" in refused["error"]
            from hermes_cli.config import load_config_readonly
            config = {**load_config_readonly(), "cron": {"allow_agent_scheduling": True}}
            with patch("hermes_cli.config.load_config_readonly", return_value=config):
                for schedule in ("every 5m", "*/10 * * * *", "0/5 * * * *", "5/10 * * * *"):  # Interval, step and N/step cron floors; once is exempt below.
                    short = json.loads(runtime.handle({"action":"watch_create", "data": arguments(runtime, "watch_create", {
                        "goal_id":"bicycle", "signal_id":"new-user-choice", "schedule":schedule,
                        "prompt":"Read fixture", "stop_condition":"User chooses"})}, session_id="source-session"))
                    assert not short["ok"] and "30 minutes" in short["error"], (schedule, short)
                for schedule in ("every 30m", "0,30 * * * *", "0 * * * *", "0 0/1 * * *", "in 1m"):
                    watch = call("watch_create", goal_id="bicycle", signal_id="new-user-choice", schedule=schedule,
                                 prompt="x" * 4000, stop_condition="User chooses a bicycle")
                    assert get_job(watch["id"]), schedule
                    if owned is None:
                        owned = watch
                    else:
                        remove_job(watch["id"])
        finally:
            clear_session_vars(tokens)

        source = runtime.store.read("session", "source-session")
        source["route"] = {"platform": "telegram", "chat_id": "123", "session_key": "source"}
        runtime.store.write("session", "source-session", source)
        for targets in ([], [{"id": "bot-chat:other-profile", "home_target_set": True}],
                        [{"id": "telegram", "home_target_set": True}]):
            with patch("cron.scheduler_delivery.cron_delivery_targets", return_value=targets):
                assert runtime.host.route("source-session") == {"deliver": "bot-chat"}
        notice = runtime.service.notification_add({
            "event_key": "delivery", "message": "A bicycle is available.", "rationale": "Matches the goal",
            "sources": ["https://example.org/source"], "verified_at": now,
            "expires": now + 3600, "priority": "urgent", "goal_id": "bicycle"})
        from cron.executions import create_execution, mark_execution_running, finish_execution
        from cron.jobs import update_job
        patrol_id = runtime.host.manifest()["fixed"]["proactive-watch"]
        assert get_job(patrol_id)["deliver"] == "bot-chat"
        # Upgrade a pre-direct installation once, without resuming paused jobs.
        record = runtime.host.manifest()
        record.pop("direct_patrol_delivery")
        runtime.host.save_manifest(record)
        update_job(patrol_id, {"deliver": "local"})
        watch_before = get_job(owned["id"])
        prefix = "Check its status and your stop condition first. "
        new_header = prefix + watch_before['prompt'].split(prefix, 1)[1].split('\n', 1)[0] + '\n'
        assert 'review_complete' in new_header and 'run_complete' in new_header
        assert runtime.host.manifest()['jobs'][owned['id']]['key'] in new_header
        old_header = ("Check its status and your stop condition first. Never send directly; prepare evidence with notification_add and notification_queue. "
                      "Stop and call watch_stop when the promised condition is satisfied. Return [SILENT].\n")
        assert new_header in watch_before["prompt"]
        update_job(owned["id"], {"prompt": watch_before["prompt"].replace(new_header, old_header),
                                "deliver": "local", "enabled": False})
        runtime.host.initialize()
        migrated = get_job(owned["id"])
        assert new_header in migrated["prompt"] and migrated["deliver"] == "bot-chat"
        assert not migrated["enabled"] and migrated["schedule"] == watch_before["schedule"]
        assert get_job(patrol_id)["deliver"] == "bot-chat"
        before = len(list_jobs(True))
        staged = runtime.handoff(notice["id"])
        assert staged["status"] == "pending" and staged["job_id"] is None
        assert cron_run(home, "proactive-watch")["wakeAgent"]
        # The wake gate is off by default and, when on, fails open (no API key here).
        gate_log = home / "logs/hermes-muse-wake-gate.jsonl"
        assert not gate_log.exists()
        plain_config = (home / "config.yaml").read_text()
        (home / "config.yaml").write_text("plugins:\n  enabled: [hermes-muse]\n  hermes-muse:\n    wake_gate:\n"
                                          "      mode: typesafe\n      api_key_env: MUSE_SMOKE_NO_SUCH_KEY\ntimezone: UTC\n")
        try:
            # The fixture holds a pending notice, so the second run is a state-driven wake, not a model call.
            for expected in ("first_run", "live_notice"):
                assert cron_run(home, "proactive-watch")["wakeAgent"]
                assert json.loads(gate_log.read_text().splitlines()[-1])["reason"] == expected, gate_log.read_text()
            assert enabled(home)
        finally:
            (home / "config.yaml").write_text(plain_config)
            gate_log.unlink()
        execution = create_execution(patrol_id, source="test")
        mark_execution_running(execution["id"])
        task_id = "cron:" + patrol_id + ":" + execution["id"]
        runtime.pre_turn(session_id="patrol-session", turn_id="patrol-turn", platform="cron", task_id=task_id)
        for action, data in [
            ("review_complete", {"job": "proactive-watch", "up_to": now, "summary": "Verified the source and candidate"}),
            ("run_complete", {"outcome": "complete", "summary": "Ready for final notice preparation"}),
        ]:
            outcome = json.loads(runtime.handle({"action": action, "data": data}, task_id=task_id, session_id="patrol-session"))
            assert outcome["ok"], outcome
        prepared = json.loads(runtime.handle({"action": "notification_prepare", "data": {"id": notice["id"]}},
                                             task_id=task_id, session_id="patrol-session"))["result"]
        assert len(list_jobs(True)) == before, "Preparing a notice must not create a delivery Cron"
        assert prepared["execution_id"] == execution["id"] and prepared["job_id"] == patrol_id
        payload = prepared["final_response"]
        assert payload.startswith("[Hermes Muse internal delivery]") and "A bicycle is available." in payload
        assert "[Hermes Muse notification " in payload and "The user is the audience" in payload
        assert runtime.transform_output("Status: dispatching", session_id="patrol-session") == payload
        assert runtime.store.read("run", execution["id"])["status"] == "complete"
        finish_execution(execution["id"], success=True, delivery_outcome="delivered")
        runtime.host.reconcile()
        assert runtime.store.read("notification", notice["id"])["status"] == "sent"
        assert runtime.host.current_delivery(task_id) is None, "A finished run cannot prepare another notice"
        hourly_id = runtime.host.manifest()["fixed"]["memory-upkeep"]
        interrupted = create_execution(hourly_id, source="test")
        mark_execution_running(interrupted["id"])
        runtime.pre_turn(session_id="interrupted-session", turn_id="interrupted-turn", platform="cron",
                         task_id="cron:" + hourly_id + ":" + interrupted["id"])
        finish_execution(interrupted["id"], success=True, delivery_outcome="suppressed")
        actual = {r["id"]: r for r in runtime.service.runs.recent()}
        assert actual[interrupted["id"]]["status"] == "partial", "Native success cannot prove task completion"
        tokens = set_session_vars(session_id="bot-session", chat_type="private", cron_session="")
        try:
            count = len(runtime.store.all("signal"))
            context = runtime.pre_turn(session_id="bot-session", turn_id="delivery-turn",
                                       user_message='[Cronjob "muse-proactive-watch" output]\n\n' + payload)
            from hermes_muse.handoff import receiving_instructions
            assert context and context["context"] == receiving_instructions()
            other = runtime.pre_turn(session_id="bot-session", turn_id="other-cron",
                user_message='[Cronjob "existing-system-monitor" output]\nA verified system finding.')
            assert other["context"] == receiving_instructions()
            assert len(runtime.store.all("signal")) == count
            for review in ("weekly-governance-review", "monthly-system-audit"):
                review_context = runtime.pre_turn(session_id="bot-session", turn_id=review,
                    user_message='[Cronjob "muse-' + review + '" output]\n\nConsider simplifying a job.')
                assert "proposals" in review_context["context"]
                assert len(runtime.store.all("signal")) == count
            call("goal_update", id="bicycle", progress="Bot reviewed the result.")
            call("preferences", ordinary_per_day=3)
            before = len(list_jobs(True))
            call("notification_handoff", id=notice["id"])
            assert len(list_jobs(True)) == before, "The same reminder must not queue twice"
            runtime.pre_turn(session_id="bot-session", turn_id="human-turn", user_message="I am interested in hiking.")
            call("interest_record", title="Hiking", signal_id="human-turn")
        finally:
            clear_session_vars(tokens)

        tokens = set_session_vars(session_id="source-session", chat_type="private", cron_session="")
        try:
            runtime.service.signal("completed-choice", "source-session", "I confirm bicycle is completed")
            call("goal_update", id="bicycle", status="done", note="Result checked and told to user")
            assert not get_job(owned["id"])["enabled"]
        finally:
            clear_session_vars(tokens)
            runtime.close()
        config = (home / "config.yaml").read_bytes()
        assert enabled(home)
        assert (home / "config.yaml").read_bytes() == config
    print("PASS: 0.1.2 background actions and evidence rules, native schedules/long prompts, writable Bot Chat handling, profile-local delivery and no duplicate queue")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="muse-baseline-") as temporary:
        os.environ["HERMES_HOME"] = temporary
        run(Path(temporary))
