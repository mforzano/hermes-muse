"""The optional wake gate is off by default, read-only, logs metadata only and fails open."""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from hermes_muse import wake_gate
from hermes_muse.store import Store

SECRET = "my private plan for Friday"


def make_result(job="proactive-watch"):
    return {"wakeAgent": True, "job": job, "started_at": "now", "instruction": "run"}


def table_dump(store):
    with store.transaction() as db:
        return db.execute("SELECT kind,id,data FROM records ORDER BY kind,id").fetchall()


class Asker:
    def __init__(self, probability=0.9, error=None):
        self.probability, self.error, self.states = probability, error, []

    def __call__(self, cfg, state, job):
        self.states.append(state)
        if self.error:
            raise self.error
        return self.probability


class WakeGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="muse-wake-gate-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.cfg = wake_gate.settings({"mode": "typesafe"})
        self.now = time.time()

    def log(self):
        path = wake_gate.log_path(self.home)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def signal(self, turn, text, age=60):
        with self.store.transaction() as db:
            self.store.put(db, "signal", turn, {"id": turn, "session_id": "s", "text": text,
                                                "created": self.now - age, "truncated": False})

    def prime(self, job="proactive-watch", minutes_ago=30):
        """A previous run that woke the agent, so the gate has history."""
        wake_gate._log(self.home, {"at": self.now - minutes_ago * 60, "job": job, "probability": None,
                                   "skipped": False, "reason": "first_run", "ms": 0})

    def apply(self, ask, job="proactive-watch", cfg=None):
        return wake_gate.apply(self.store, job, make_result(job), cfg or self.cfg, clock=lambda: self.now, ask=ask,
                               local_time="Wednesday 2026-10-21 10:00")

    def test_settings_default_off_and_bad_values_fall_back(self):
        self.assertEqual(wake_gate.settings(None)["mode"], "off")
        self.assertEqual(wake_gate.settings({"mode": "on"})["mode"], "off")
        cfg = wake_gate.settings({"mode": "TypeSafe", "jobs": ["feed-pulse", "nightly-review", 3],
                                  "min_probability": 7, "timeout_seconds": "fast", "max_skip_minutes": True,
                                  "base_url": "ftp://x", "include_message_text": "no"})
        self.assertEqual(cfg["mode"], "typesafe")
        self.assertEqual(cfg["jobs"], ["feed-pulse"])
        for key in ("min_probability", "timeout_seconds", "max_skip_minutes", "base_url", "include_message_text"):
            self.assertEqual(cfg[key], wake_gate.DEFAULTS[key], key)
        self.assertEqual(wake_gate.DEFAULTS["mode"], "off")
        self.assertEqual(wake_gate.settings({"jobs": ["proactive-watch"]})["jobs"], ["proactive-watch"])

    def test_off_and_ungated_jobs_are_untouched_and_silent(self):
        ask = Asker(0.0)
        for cfg, job in ((wake_gate.settings(None), "proactive-watch"),
                         (wake_gate.settings({"mode": "typesafe", "jobs": ["feed-pulse"]}), "proactive-watch"),
                         (self.cfg, "nightly-review")):
            result = make_result(job)
            self.assertIs(wake_gate.apply(self.store, job, result, cfg, ask=ask), result)
        self.assertFalse(ask.states)
        self.assertFalse(wake_gate.log_path(self.home).exists())
        # A result that already says do not wake is never turned into a wake or logged.
        stopped = {"wakeAgent": False}
        self.assertIs(wake_gate.apply(self.store, "proactive-watch", stopped, self.cfg, ask=ask), stopped)

    def test_first_run_wakes_and_later_low_probability_skips(self):
        ask = Asker(0.01)
        self.assertEqual(self.apply(ask), make_result())
        self.assertFalse(ask.states, "no history yet: wake without asking")
        self.assertEqual(self.log()[0]["reason"], "first_run")
        # The first entry above was written with the real clock; line it up with this test's clock.
        wake_gate.log_path(self.home).unlink()
        self.prime()
        result = self.apply(ask)
        self.assertEqual(result["wakeAgent"], False)
        self.assertEqual(result["reason"], "wake_gate")
        last = self.log()[-1]
        self.assertEqual((last["job"], last["probability"], last["skipped"], last["reason"]),
                         ("proactive-watch", 0.01, True, "below_threshold"))
        self.assertIsInstance(last["ms"], int)
        self.assertEqual(self.apply(Asker(0.5))["wakeAgent"], True)
        self.assertEqual(self.log()[-1]["skipped"], False)

    def test_threshold_boundary_and_custom_threshold(self):
        self.prime()
        self.assertTrue(self.apply(Asker(0.15))["wakeAgent"])
        self.assertFalse(self.apply(Asker(0.149))["wakeAgent"])
        strict = wake_gate.settings({"mode": "typesafe", "min_probability": 0.6})
        self.assertFalse(self.apply(Asker(0.5), cfg=strict)["wakeAgent"])

    def test_every_failure_wakes(self):
        self.prime()
        for error in (TimeoutError(), OSError("down"), ValueError("bad json"), KeyError("answers"),
                      LookupError("api key missing")):
            result = self.apply(Asker(error=error))
            self.assertEqual(result, make_result(), error)
            self.assertEqual(self.log()[-1]["reason"], "error:" + type(error).__name__)
            self.assertIs(self.log()[-1]["skipped"], False)

    def test_unreadable_state_wakes(self):
        self.prime()
        (self.home / "muse" / "state.db").unlink()
        ask = Asker(0.0)
        self.assertEqual(self.apply(ask), make_result())
        self.assertTrue(self.log()[-1]["reason"].startswith("error:"))

    def test_missing_key_and_dead_endpoint_fail_open_through_the_real_request(self):
        self.prime()
        cfg = wake_gate.settings({"mode": "typesafe", "api_key_env": "MUSE_TEST_NO_SUCH_KEY",
                                  "base_url": "http://127.0.0.1:9", "timeout_seconds": 0.5})
        os.environ.pop("MUSE_TEST_NO_SUCH_KEY", None)
        result = wake_gate.apply(self.store, "proactive-watch", make_result(), cfg, clock=lambda: self.now)
        self.assertTrue(result["wakeAgent"])
        self.assertEqual(self.log()[-1]["reason"], "error:LookupError")
        with patch.dict(os.environ, {"MUSE_TEST_NO_SUCH_KEY": "k"}):
            result = wake_gate.apply(self.store, "proactive-watch", make_result(), cfg, clock=lambda: self.now)
        self.assertTrue(result["wakeAgent"])
        self.assertTrue(self.log()[-1]["reason"].startswith("error:"))

    def test_request_shape_and_timeout_against_a_local_endpoint(self):
        seen, delay = {}, [0]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"], seen["auth"] = self.path, self.headers["Authorization"]
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                time.sleep(delay[0])
                payload = json.dumps({"answers": {"run": {"type": "noul", "noul": 0.04}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = wake_gate.settings({"mode": "typesafe", "api_key_env": "MUSE_TEST_KEY", "timeout_seconds": 0.5,
                                  "base_url": f"http://127.0.0.1:{server.server_port}/v1"})
        self.prime()
        self.signal("t1", SECRET)
        with patch.dict(os.environ, {"MUSE_TEST_KEY": "secret-key"}):
            result = wake_gate.apply(self.store, "proactive-watch", make_result(), cfg, clock=lambda: self.now)
            self.assertFalse(result["wakeAgent"])
            self.assertEqual(seen["path"], "/v1/systemone")
            self.assertEqual(seen["auth"], "Bearer secret-key")
            body = seen["body"]
            self.assertEqual(body["model"], "jev-latest")
            self.assertEqual(body["questions"]["run"]["type"], "noul")
            self.assertEqual(body["state"]["new_user_message_count"], 1)
            self.assertNotIn("secret-key", json.dumps(body))
            delay[0] = 2
            started = time.monotonic()
            result = wake_gate.apply(self.store, "proactive-watch", make_result(), cfg, clock=lambda: self.now)
            self.assertLess(time.monotonic() - started, 1.8)
        self.assertTrue(result["wakeAgent"])
        self.assertTrue(self.log()[-1]["reason"].startswith("error:"))
        for line in wake_gate.log_path(self.home).read_text().splitlines():
            self.assertNotIn("secret-key", line)
            self.assertNotIn(SECRET, line)

    def test_max_skip_forces_a_run_without_asking(self):
        self.prime(minutes_ago=121)
        ask = Asker(0.0)
        self.assertEqual(self.apply(ask), make_result())
        self.assertFalse(ask.states)
        self.assertEqual(self.log()[-1]["reason"], "max_skip")
        unlimited = wake_gate.settings({"mode": "typesafe", "max_skip_minutes": 0})
        self.assertFalse(self.apply(ask, cfg=unlimited)["wakeAgent"])

    def test_due_goal_live_notice_feed_request_and_backlog_skip_the_model(self):
        ask = Asker(0.0)
        self.prime()
        goal = {"id": "g1", "title": "Reply to the landlord", "status": "active", "revision": 1,
                "research_review_at": self.now + 86400, "next_check_at": self.now + 3600, "deadline": None}
        self.store.save_goal(goal, "# Reply\n")
        self.assertFalse(self.apply(ask)["wakeAgent"], "a goal that is not due does not force a run")
        self.assertEqual(len(ask.states), 1)
        goals = ask.states[0]["active_goals"]
        self.assertEqual((goals["count"], goals["next_check_within_2h"][0]["minutes_until_check"]), (1, 60))
        # Overdue since before the last wake: Cron cannot clear it, so it must not wake every tick.
        self.store.save_goal({**goal, "revision": 2, "next_check_at": self.now - 40 * 60}, "# Reply\n",
                             expected_hash=self.store.goal("g1", with_fingerprint=True)[2])
        self.assertFalse(self.apply(ask)["wakeAgent"], "a stale overdue goal does not force a run")
        self.assertEqual(len(ask.states), 2)
        self.assertEqual(ask.states[1]["active_goals"]["next_check_within_2h"], [])
        # Became due after the last wake: look at it once.
        self.store.save_goal({**goal, "revision": 3, "next_check_at": self.now - 5}, "# Reply\n",
                             expected_hash=self.store.goal("g1", with_fingerprint=True)[2])
        before = len(ask.states)
        self.assertTrue(self.apply(ask)["wakeAgent"])
        self.assertEqual(len(ask.states), before)
        self.assertEqual(self.log()[-1]["reason"], "due_goal")
        self.prime()  # the wake above was logged at the test clock; the next goal state is newer
        self.store.save_goal({**goal, "revision": 4}, "# Reply\n",
                             expected_hash=self.store.goal("g1", with_fingerprint=True)[2])
        with self.store.transaction() as db:
            self.store.put(db, "notification", "n1", {"id": "n1", "status": "pending", "expires": self.now + 600,
                                                      "created": self.now})
        self.assertTrue(self.apply(ask)["wakeAgent"])
        self.assertEqual(self.log()[-1]["reason"], "live_notice")
        with self.store.transaction() as db:
            self.store.put(db, "notification", "n1", {"id": "n1", "status": "pending", "expires": self.now - 1,
                                                      "created": self.now - 900})
            self.store.put(db, "meta", "feed_requested", self.now - 10)
        self.prime("feed-pulse")
        self.assertTrue(self.apply(ask, "feed-pulse")["wakeAgent"])
        self.assertEqual(self.log()[-1]["reason"], "feed_requested")
        self.prime("memory-upkeep")
        for i in range(wake_gate.BACKLOG_WAKE):
            self.signal(f"b{i}", "hello", age=100 + i)
        self.assertTrue(self.apply(ask, "memory-upkeep")["wakeAgent"])
        self.assertEqual(self.log()[-1]["reason"], "backlog")
        self.assertEqual(len(ask.states), before)

    def test_gate_changes_no_state_and_a_skipped_round_still_sees_its_signals(self):
        self.prime()
        self.prime("memory-upkeep")
        self.signal("t1", SECRET, age=300)
        before = table_dump(self.store)
        first, second = Asker(0.02), Asker(0.02)
        self.assertFalse(self.apply(first)["wakeAgent"])
        self.assertFalse(self.apply(second)["wakeAgent"])
        self.assertFalse(self.apply(Asker(0.02), "memory-upkeep")["wakeAgent"])
        self.assertEqual(table_dump(self.store), before, "the gate must not touch cursors or signals")
        for ask in (first, second):
            self.assertEqual(ask.states[0]["new_user_message_count"], 1)
            self.assertEqual(ask.states[0]["newest_new_user_messages"][0]["text"], SECRET)
        # Skips are not wakes: the next run still counts the message as new.
        self.assertEqual([e["skipped"] for e in self.log() if e["job"] == "proactive-watch"], [False, True, True])

    def test_digest_is_bounded_and_text_can_be_withheld(self):
        self.prime()
        for i in range(200):
            self.signal(f"m{i}", SECRET + " " + "x" * 5000, age=10 + i)
        ask = Asker(0.01)  # a skip, so the next call sees the same messages
        self.apply(ask)
        state = ask.states[0]
        self.assertEqual(state["new_user_message_count"], 200)
        self.assertEqual(len(state["newest_new_user_messages"]), wake_gate.LIST_LIMIT)
        self.assertLess(len(json.dumps(state)), 4000)
        private = wake_gate.settings({"mode": "typesafe", "include_message_text": False})
        ask = Asker(0.9)
        self.apply(ask, cfg=private)
        self.assertNotIn(SECRET, json.dumps(ask.states[0]))
        self.assertEqual(ask.states[0]["new_user_message_count"], 200)

    def test_each_gated_job_has_its_own_question_and_only_the_data_it_needs(self):
        self.assertEqual(set(wake_gate.QUESTIONS), set(wake_gate.GATED_JOBS))
        self.assertEqual(set(wake_gate.PURPOSE), set(wake_gate.GATED_JOBS))
        states = {}
        for job in wake_gate.GATED_JOBS:
            self.prime(job)
            ask = Asker(0.9)
            self.apply(ask, job)
            states[job] = ask.states[0]
            self.assertEqual(states[job]["job"], job)
            self.assertNotEqual(wake_gate.question(job)["instructions"],
                                wake_gate.question(next(j for j in wake_gate.GATED_JOBS if j != job))["instructions"])
        self.assertIn("active_goals", states["proactive-watch"])
        self.assertIn("feed_articles_last_24h", states["feed-pulse"])
        for job in ("memory-upkeep", "feed-pulse"):
            self.assertNotIn("active_goals", states[job])
        self.assertNotIn("feed_articles_last_24h", states["memory-upkeep"])

    def test_background_messages_are_not_user_signals(self):
        self.prime()
        self.signal("t1", '[Cronjob "x"] report')
        ask = Asker(0.9)
        self.apply(ask)
        self.assertEqual(ask.states[0]["new_user_message_count"], 0)

    def test_memory_upkeep_uses_its_cursor_not_the_last_wake(self):
        self.prime("memory-upkeep", minutes_ago=10)
        self.signal("old", "before the last wake", age=3600)
        ask = Asker(0.9)
        self.apply(ask, "memory-upkeep")
        self.assertEqual(ask.states[0]["new_user_message_count"], 1)
        with self.store.transaction() as db:
            self.store.put(db, "cursor", "memory-upkeep", self.now - 1800)
        ask = Asker(0.9)
        self.apply(ask, "memory-upkeep")
        self.assertEqual(ask.states[0]["new_user_message_count"], 0)

    def test_log_is_private_bounded_and_rotates(self):
        wake_gate._log(self.home, {"at": 1, "job": "x", "skipped": False})
        if os.name == "nt":
            import win32security
            sd = win32security.GetFileSecurity(str(wake_gate.log_path(self.home)),
                                              win32security.DACL_SECURITY_INFORMATION)
            self.assertTrue(sd.GetSecurityDescriptorControl()[0] & 0x1000)
            acl = sd.GetSecurityDescriptorDacl()
            self.assertLessEqual(acl.GetAceCount(), 3)
        else:
            self.assertEqual(oct(wake_gate.log_path(self.home).stat().st_mode & 0o777), "0o600")
        pad = "p" * 600
        for i in range(wake_gate.LOG_MAX_BYTES // 600 + 20):
            wake_gate._log(self.home, {"at": i, "job": "x", "skipped": False, "pad": pad})
        lines = wake_gate.log_path(self.home).read_text().splitlines()
        self.assertLess(wake_gate.log_path(self.home).stat().st_size, wake_gate.LOG_MAX_BYTES)
        self.assertTrue(all(json.loads(line) for line in lines), "rotation keeps whole lines")
        self.assertEqual(json.loads(lines[-1])["at"], wake_gate.LOG_MAX_BYTES // 600 + 19)


if __name__ == "__main__":
    unittest.main()
