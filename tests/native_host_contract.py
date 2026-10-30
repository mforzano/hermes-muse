"""Native contract test for hermes_muse/host (acceptance A13) and probe self-check (A4).

Synthetic temporary homes only; no model calls, no live channels, no user data.
Run with a Hermes interpreter: HERMES_SOURCE=/path/to/hermes-agent python tests/native_host_contract.py
The whole test runs from a deleted process cwd: the adapter must never need it.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
if os.environ.get("HERMES_SOURCE"):
    sys.path.insert(0, os.environ["HERMES_SOURCE"])
STATUSES = {"ok", "degraded", "missing"}


def seed_state(home, job_id):
    from hermes_state import SessionDB
    SessionDB(db_path=home / "state.db").close()  # native schema
    now = time.time()
    db = sqlite3.connect(home / "state.db")
    sessions = [("chat", "cli", "dm", "Plans", str(home)), ("bot", "cli", "dm", "Bot Chat", "/workspace"),
                ("local-run", "cron", "dm", "muse-patrol · Oct 29 20:36", None), (f"cron_{job_id}_20261029_120000", "cron", None, None, None),
                ("other-run", "cron", "dm", "someone-else · Oct 29 20:36", None), ("once", "oneshot", "dm", None, None),
                ("room", "telegram", "group", None, None), ("odd", "local", "dm", None, None)]
    for sid, source, chat_type, title, cwd in sessions:
        db.execute("INSERT INTO sessions (id, source, chat_type, title, cwd, started_at) VALUES (?,?,?,?,?,?)",
                   (sid, source, chat_type, title, cwd, now))
    long_text = "Attachment:\n" + "x" * 20000 + "\nkey sk-abcdefghijklmnopqrstuvwx end"
    messages = [("chat", "user", "Remind me Friday; password: hunter2xyz"), ("chat", "assistant", "Done, saved."),
                ("chat", "user", '[Cronjob "muse-deliver-k" output — scheduled job, not the user.]\n\nhi'),
                ("chat", "user", long_text), ("chat", "user", b"\xff"), ("once", "user", "Reply only OK"),
                ("room", "user", "group chatter"), ("local-run", "user", "cron prompt")]
    for i, (sid, role, content) in enumerate(messages):
        db.execute("INSERT INTO messages (session_id, role, content, timestamp, message_uid) VALUES (?,?,?,?,?)",
                   (sid, role, content, now + i * 0.001, f"uid-{i}"))
    db.commit()
    db.close()


def finish(job_id, outcome, success=True, error=None):
    from cron.executions import create_execution, finish_execution, mark_execution_running
    execution = create_execution(job_id, source="test")
    mark_execution_running(execution["id"])
    finish_execution(execution["id"], success=success, error=error, delivery_outcome=outcome)
    return execution["id"]


def main():
    with tempfile.TemporaryDirectory(prefix="muse-host-contract-") as temporary:
        base = Path(temporary).resolve()
        home = base / "profile"
        (home / "workspace").mkdir(parents=True)
        (home / "config.yaml").write_text(
            "timezone: UTC\nplugins:\n  enabled: [hermes-muse]\n  hermes-muse: {quiet: legacy, budget: 1}\n"
            "  entries:\n    hermes-muse:\n      settings: {budget: 2}\n")
        os.environ.update(HERMES_HOME=str(home), HERMES_IGNORE_USER_CONFIG="1")
        gone = base / "gone"
        gone.mkdir()
        os.chdir(gone)
        gone.rmdir()  # from here on os.getcwd() raises

        from cron.executions import create_execution
        from cron.jobs import list_jobs, pause_job, update_job
        from hermes_muse.host import HostAdapter, profile_scope, resolve_home
        from hermes_muse.host import sessions as host_sessions

        for bad, error in ((Path.home, TypeError), ("relative/home", ValueError)):
            try:
                resolve_home(bad)
                raise AssertionError(f"accepted home {bad!r}")
            except error:
                pass
        host = HostAdapter(home)
        assert host.cwd() == home / "workspace"
        assert host.config.enabled() is True
        assert host.config.get() == {"quiet": "legacy", "budget": 2}, host.config.get()

        # cron.ensure: owned launcher with absolute paths, no workdir, idempotent, user pause survives.
        entry = base / "entry.py"
        entry.write_text("import json\nprint(json.dumps({'home': MUSE_HOME, 'job': MUSE_JOB}))\n")
        job = host.cron.ensure("patrol", schedule="every 30m", entry=entry, prompt="Patrol.", deliver="bot-chat")
        assert job and job["name"] == "muse-patrol" and job.get("workdir") is None, job
        with profile_scope(home):
            pause_job(job["id"], reason="user pause")
        again = host.cron.ensure("patrol", schedule="every 1h", entry=entry, prompt="Patrol v2.")
        assert again["id"] == job["id"] and not again["enabled"] and again["prompt"] == "Patrol v2.", again
        script = home / "scripts" / "hermes-muse-patrol.py"
        ran = subprocess.run([sys.executable, str(script)], cwd=base, capture_output=True, text=True, check=True)
        assert json.loads(ran.stdout) == {"home": str(home), "job": "patrol"}, ran.stdout
        assert set(host.cron.jobs()) == {job["id"]}
        assert host.cron.wake(job["id"]) is False  # a user pause is never undone by a wake
        intake = host.cron.ensure("intake", schedule="every 1h", entry=entry, prompt="Intake.")
        assert host.cron.wake(intake["id"]) and host.cron.pause(intake["id"], "test")
        assert not host.cron.jobs()[intake["id"]]["enabled"] and host.cron.wake(intake["id"]) is False

        # sessions: user surfaces only, redacted, bounded; cron session attribution.
        seed_state(home, job["id"])
        with profile_scope(home):
            run_id = finish(job["id"], "delivered")
        turns = host.sessions.user_turns(0)
        assert [t["session_id"] for t in turns] == ["chat"] * 2, turns
        assert "hunter2xyz" not in turns[0]["text"] and "[REDACTED]" in turns[0]["text"]
        attachment = turns[1]
        assert attachment["attachment"] and len(attachment["text"]) < 8200 and "sk-abc" not in attachment["text"]
        assert any(f["call"] == "sessions.user_turns.row" for f in host.faults), "bad row must be skipped, not fatal"
        with_replies = host.sessions.user_turns(0, roles=("user", "assistant"))
        assert [t["role"] for t in with_replies] == ["user", "assistant", "user"]
        page = host.sessions.read(attachment["ref"], offset=attachment["chars"] - 100, length=50000)
        assert page["available"] and page["total"] == attachment["chars"] and "[REDACTED]" in page["text"] and not page["more"]
        assert len(host.sessions.read(attachment["ref"], length=50000)["text"]) == 12000
        once = next(iter(sqlite3.connect(home / "state.db").execute("SELECT id FROM messages WHERE session_id='once'")))[0]
        assert host.sessions.read(f"once:{once}") == {"ref": f"once:{once}", "available": False}
        assert host.sessions.job_of("local-run") == job["id"]
        assert host.sessions.job_of(f"cron_{job['id']}_20261029_120000") == job["id"]
        assert host.sessions.job_of("other-run") is None and host.sessions.job_of("chat") is None

        # deliver: one-shot no-agent job, at most once, receipts from native ledger and native Bot Chat key.
        sent = host.deliver.send("bot-chat", "Your synthetic reminder.", "k1")
        assert sent["status"] == "sending", sent
        assert host.deliver.send("bot-chat", "Your synthetic reminder.", "k1")["status"] == "sending"
        deliver_jobs = [j for j in host.cron.jobs().values() if j["name"] == "muse-deliver-k1"]
        assert len(deliver_jobs) == 1 and deliver_jobs[0]["no_agent"], deliver_jobs
        out = home / "scripts" / "hermes-muse-deliver-k1.py"
        ran = subprocess.run([sys.executable, str(out)], cwd=base, capture_output=True, text=True, check=True)
        assert ran.stdout.strip() == "Your synthetic reminder." and not out.exists()
        assert host.deliver.receipt("k1")["status"] == "sending"
        k1 = deliver_jobs[0]["id"]
        with profile_scope(home), mock.patch("tools.bot_live_delivery.find_canonical_live_owner", side_effect=ValueError("down")):
            from cron.scheduler_delivery import _deliver_to_bot_chat
            execution = create_execution(k1, source="test")
            native = _deliver_to_bot_chat({"id": k1, "name": "muse-deliver-k1", "execution_id": execution["id"]}, "body", "")
            assert "queued" in native, native
            from cron.executions import finish_execution, mark_execution_running
            mark_execution_running(execution["id"])
            finish_execution(execution["id"], success=True, delivery_outcome="queued")
        assert host.deliver.receipt("k1") == {"job_id": k1, "execution_id": execution["id"], "outcome": "queued",
                                              "status": "sending"}, host.deliver.receipt("k1")
        for key, outcome, error, expected in (("k2", "delivered", None, "sent"),
                                              ("k3", "failed", "bot_relay.deliver refused: invalid_params field=cwd", "host_config"),
                                              ("k4", "failed", "adapter timeout", "transport"), ("k5", "suppressed", None, "unknown")):
            job_id = host.deliver.send("telegram", "x", key)["job_id"]
            with profile_scope(home):
                finish(job_id, outcome)
                if error:
                    update_job(job_id, {"last_delivery_error": error})
            receipt = host.deliver.receipt(key)
            assert expected in (receipt["status"], receipt.get("reason")), (key, receipt)
        assert host.deliver.receipt("never")["status"] == "unknown"
        assert host.owners.status("no-such-delegation")["state"] == "unknown"

        # A4: a healthy profile probes all ok; the seeded faults are each caught; a fixed Bot Chat cwd clears.
        clean = base / "clean"
        clean.mkdir()
        (clean / "config.yaml").write_text("plugins:\n  enabled: [hermes-muse]\n")
        assert [r["status"] for r in HostAdapter(clean).probe(captured_sources=["cli"])] == ["ok"] * 7
        (home / "muse" / "<bound method Path.home of <class 'pathlib.PosixPath'>>").mkdir(parents=True)
        results = {r["name"]: r for r in host.probe(captured_sources=["cli", "oneshot"])}
        for r in results.values():
            assert set(r) == {"name", "status", "detail", "fix"} and r["status"] in STATUSES, r
            assert (r["status"] == "ok") == (r["fix"] == ""), r
        assert len(results) == 7
        expected = {"symbols": "ok", "cron_sessions": "ok", "delivery_semantics": "degraded",
                    "delivery_target": "missing", "signal_purity": "missing", "config_location": "degraded", "home": "degraded"}
        assert {n: r["status"] for n, r in results.items()} == expected, results
        assert "'local'" in results["signal_purity"]["detail"], results["signal_purity"]
        print(json.dumps(list(results.values()), ensure_ascii=False, indent=1))
        with sqlite3.connect(home / "state.db") as db:
            db.execute("UPDATE sessions SET cwd=? WHERE id='bot'", (str(home),))
        assert host.probe()[3]["status"] == "ok"

        # Per-call tolerance: a broken host degrades to empty answers and faults, never raises or resends.
        faults = host.fault_count
        with mock.patch("cron.jobs.list_jobs", side_effect=RuntimeError("host down")):
            assert host.cron.jobs() == {}
            assert host.deliver.send("bot-chat", "again", "k1") == {"status": "failed", "reason": "host_error"}
            assert host.cron.ensure("patrol", schedule="every 30m", entry=entry) is None
            assert host.probe()[1]["status"] != "ok"
        with mock.patch.object(host_sessions, "reader", side_effect=sqlite3.OperationalError("locked")):
            assert host.sessions.user_turns(0) == [] and host.sessions.job_of("local-run") is None
        assert host.fault_count > faults
        with profile_scope(home):
            assert len(list_jobs(include_disabled=True)) == 7  # patrol, intake, k1..k5: no duplicates
    print("PASS native_host_contract")


if __name__ == "__main__":
    main()
