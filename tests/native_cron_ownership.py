"""Cron cwd migration/lifecycle against native jobs; only synthetic temporary homes."""
import json
import time


def run(base):
    from cron.jobs import create_job, get_job, list_jobs, pause_job, update_job
    from cron.executions import create_execution, mark_execution_running, finish_execution
    from hermes_muse.host_v1 import HermesHost, profile_scope
    from hermes_muse.installation import prepare_owned
    from hermes_muse.service import Companion
    from hermes_muse.store import Store
    from hermes_muse.views import Views

    for operation in ("disable", "remove"):
        home = base / operation
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("timezone: UTC\nplugins:\n  enabled: [hermes-muse]\n")
        with profile_scope(home):
            store = Store(home)
            host = HermesHost(store)
            original = host.initialize()
            assert len(list_jobs(True)) == 6
            assert all(j.get("workdir") is None for j in list_jobs(True))
            service = Companion(store, host)
            service.signal("fixture", "fixture-session", "Watch the synthetic goal")
            service.goal_create({"id": "fixture", "signal_id": "fixture", "title": "Fixture",
                                 "description": "Synthetic", "completion_criteria": "Verified"})
            watches = [host.watch({"goal_id": "fixture", "signal_id": "fixture", "schedule": schedule,
                                   "prompt": "Read synthetic evidence", "stop_condition": "Verified",
                                   "expires": time.time() + 7200})
                       for schedule in ("every 30m", "every 1h", "in 1h", "in 2h")]
            assert all(j.get("workdir") is None for j in watches)
            # Reconstruct 0.3.1 state, including paused and completed one-shot jobs.
            owned_ids = set(host.manifest()["jobs"])
            for job_id in owned_ids:
                update_job(job_id, {"workdir": str(store.root)})
            pause_job(watches[1]["id"], reason="User fixture pause")
            update_job(watches[3]["id"], {"state": "completed", "enabled": False,
                                         "next_run_at": None, "repeat": {"times": 1, "completed": 1}})
            feed_id = original["fixed"]["feed-pulse"]
            update_job(feed_id, {"schedule": "every 2h", "deliver": "bot-chat", "model": "fixture-model"})
            pause_job(feed_id, reason="Keep user pause")
            # A crash between native create and manifest save must not duplicate a fixed job.
            record = host.manifest()
            patrol_id = record["fixed"]["proactive-watch"]
            record["jobs"].pop(patrol_id)
            host.save_manifest(record)
            metadata = host.manifest()["jobs"].copy()
            fields = ("schedule", "repeat", "enabled", "state", "paused_reason", "next_run_at",
                      "deliver", "origin", "model", "provider", "attach_to_session")
            before = {i: {k: get_job(i).get(k) for k in fields} for i in owned_ids}
            unrelated = [create_job(prompt=prompt, schedule="every 2h", name="muse-feed-pulse",
                                    workdir=str(store.root), deliver="local")
                         for prompt in ("Unrelated user job", "[hermes-muse:foreign:feed-pulse]\nForeign job")]
            unrelated_before = {j["id"]: get_job(j["id"]) for j in unrelated}

            host.initialize()
            host.initialize()
            assert set(host.manifest()["jobs"]) == owned_ids
            assert host.manifest()["fixed"] == original["fixed"]
            assert host.manifest()["installation_id"] == original["installation_id"]
            assert len(list_jobs(True)) == len(owned_ids) + len(unrelated)
            assert {i: {k: get_job(i).get(k) for k in fields} for i in owned_ids} == before
            assert all(get_job(i).get("workdir") is None and host.owned_job(i) for i in owned_ids)
            assert {i: host.manifest()["jobs"][i] for i in metadata} == metadata
            assert {j["id"]: get_job(j["id"]) for j in unrelated} == unrelated_before
            assert all(host.owned_job(j["id"]) is None for j in unrelated)
            # Verify the on-disk native store, rather than only API return values.
            disk = json.loads((home / "cron/jobs.json").read_text())["jobs"]
            assert all(j.get("workdir") is None for j in disk if j["id"] in owned_ids)

            for job_id in (patrol_id, watches[0]["id"]):
                attempt = create_execution(job_id, source="test")
                mark_execution_running(attempt["id"])
                binding = host.current_delivery(f"cron:{job_id}:{attempt['id']}")
                assert binding and binding["route"] == {"deliver": get_job(job_id)["deliver"]}
                finish_execution(attempt["id"], success=True)
            assert host.is_active_watch(watches[0]["id"], "fixture")
            assert host.is_active_watch(watches[2]["id"], "fixture")
            assert not host.is_active_watch(watches[1]["id"], "fixture")
            host.stop_watch(watches[0]["id"])
            assert not get_job(watches[0]["id"])["enabled"]

            # The lesson body is read through the same tool-facing projection without cwd.
            lessons = store.path("AGENTS.md")
            lessons.write_text(lessons.read_text().replace("<!-- muse-lessons:start -->",
                               "<!-- muse-lessons:start -->\nVerify fixture sources.\n"))
            body = Views(home).object("working_lessons", "current")[0]
            assert "Verify fixture sources." in body

            result = prepare_owned(home, store, host, {"operation": operation,
                "request_id": "prepare-fixture", "expected_revision": Views(home).object("installation", "current")[2]})
            assert not result["partial"] and not result["remaining_ids"]
            assert set(result["paused_owned_ids"] + result["unchanged_ids"]) == owned_ids
            assert all(not get_job(i)["enabled"] for i in owned_ids)
            assert {j["id"]: get_job(j["id"]) for j in unrelated} == unrelated_before
            # Reload and owner cancellation must retain these pauses and recognize watches.
            host.initialize()
            assert all(not get_job(i)["enabled"] for i in owned_ids)
            host.pause_owner("fixture")  # skips the completed one-shot instead of raising
            assert get_job(watches[3]["id"])["state"] == "completed"
            assert all(not host.is_active_watch(j["id"], "fixture") for j in watches)
    print("PASS: cron ownership/migration, fresh cwd, double initialize, orphan recovery, watches, delivery, lessons, disable/remove isolation")
