"""WP4 acceptance: kernel business on synthetic homes (design 2.1, 2.4, 2.5, 2.7, 2.9; 7.3 A3, A5, A10).

Part 1 drives the kernel through a recording fake host (no Hermes needed). Part 2, when Hermes is
importable, runs Intake and Notifier through the real HostAdapter on a temporary Hermes home; it creates a
delivery job in that home only and never runs it. No real user data, no live channel.
Usage: PYTHONPATH=<hermes-agent> <hermes venv python> tests/kernel_acceptance.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_muse.host.sessions import SECRETS, excerpt, redact  # noqa: E402
from hermes_muse.kernel import Kernel, goals, intake, notifier, review, sources  # noqa: E402
from hermes_muse.kernel.common import render  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 29, 10, 0, tzinfo=UTC).timestamp()
H, D = 3600, 86400
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
FAILED = []
SEEN_TEXT = []  # every packet / read handed to the model, for A10


def check(name, ok, detail=""):
    print(f"{name} {'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)}")
    if not ok:
        FAILED.append(name)


class FakeHost:
    """The HostAdapter surface the kernel uses, over in-memory rows; filters like hermes_muse/host does."""

    def __init__(self, clock):
        self.clock, self.rows, self.delivered, self.receipts = clock, [], {}, {}
        self.paused, self.outcomes, self.owner, self.send_errors = [], {}, {}, 0
        self.sessions = SimpleNamespace(user_turns=self.user_turns, read=self.read)
        self.deliver = SimpleNamespace(send=self.send, receipt=self.receipt)
        self.cron = SimpleNamespace(pause=lambda job_id, reason: self.paused.append(job_id) or True,
                                    outcome=lambda job_id, execution_id=None: self.outcomes.get(execution_id))
        self.owners = SimpleNamespace(status=lambda handle: {"state": self.owner.get(handle, "unknown")})
        self.config = SimpleNamespace(get=lambda: {})

    def now(self):
        return datetime.fromtimestamp(self.clock(), UTC)

    def add(self, session, role, text, at, source="cli", tool=None):
        self.rows.append({"id": len(self.rows) + 1, "session_id": session, "role": role, "text": text, "at": at,
                          "source": source, "tool": tool})
        return f"{session}:{len(self.rows)}"

    def user_turns(self, since, roles=("user",), limit=500):
        out = []
        for r in self.rows:
            if r["at"] > since and r["role"] in roles and sources.classify(r["source"]) == "user":
                clean = redact(r["text"])
                out.append({"ref": f"{r['session_id']}:{r['id']}", "session_id": r["session_id"], "role": r["role"],
                            "at": r["at"], "source": r["source"], "tool_name": r["tool"], "chars": len(clean),
                            "attachment": len(clean) > 8000, "text": excerpt(clean)})
        return out[:limit]

    def read(self, ref, offset=0, length=12000):
        row = next(r for r in self.rows if f"{r['session_id']}:{r['id']}" == ref)
        text = redact(row["text"])
        part = text[offset:offset + min(length, 12000)]
        return {"ref": ref, "available": True, "offset": offset, "total": len(text), "text": part, "more": offset + len(part) < len(text)}

    def send(self, target, text, key):
        assert KEY.fullmatch(key), key
        if self.send_errors:
            self.send_errors -= 1
            return {"status": "failed", "reason": "host_error"}
        if key in self.delivered:
            return self.receipt(key)
        self.delivered[key] = text
        return {"status": "sending", "job_id": "job-" + key[:8]}

    def receipt(self, key):
        if key not in self.delivered:
            return {"status": "unknown", "reason": "no_delivery_job"}
        return self.receipts.get(key, {"status": "sending", "job_id": "job-" + key[:8]})


def make_home(base, prefs=None):
    home = base / "profile"
    (home / "muse").mkdir(parents=True)
    defaults = {"start_hour": 9, "end_hour": 22, "ordinary_per_day": 1, "time_sensitive_per_day": 3, "blocked_topics": []}
    (home / "muse" / "PROACTIVE_PREFERENCES.md").write_text(
        "<!-- hermes-muse " + json.dumps({**defaults, **(prefs or {})}) + " -->\n# Preferences\n", encoding="utf-8")
    return home


def seen(value):
    SEEN_TEXT.append(json.dumps(value, ensure_ascii=False))
    return value


def deliver_all(k, host):
    for key in list(host.delivered):
        host.receipts.setdefault(key, {"status": "sent", "job_id": "job-" + key[:8]})
    k.tick()


def part_intake(base):
    clock = [T0]
    host = FakeHost(lambda: clock[0])
    home = make_home(base)
    k = Kernel(home, adapter=host, clock=lambda: clock[0])
    host.add("chat", "user", "Remind me Friday; password: hunter2xyz", T0 - 50)
    host.add("chat", "assistant", "Noted.", T0 - 49)
    long_ref = host.add("chat", "user", "Pasted data:\n" + "row,1,2\n" * 4000 + "key sk-" + "a" * 30 + " end", T0 - 40)
    host.add("cronrun", "user", "cron prompt", T0 - 30, source="cron")
    host.add("odd", "user", "legacy source", T0 - 30, source="local")
    host.add("tg", "user", "telegram note", T0 - 20, source="telegram")
    forgotten = host.add("chat", "user", "forget me", T0 - 10)
    k.store.create("tombstone", forgotten, {}, now=T0 - 5)
    first = k.tick()
    again = k.tick()
    signals = k.store.query("signal")
    check("K1 intake: user surfaces only, machine/unclassified/forgotten skipped, idempotent",
          first.get("signals_captured") == 3 and again.get("signals_captured") == 0
          and sorted(s["payload"]["source"] for s in signals) == ["cli", "cli", "telegram"],
          {"first": first.get("signals_captured"), "again": again.get("signals_captured"),
           "sources": intake.captured_sources(k.store, T0 - D), "classify": {s: sources.classify(s) for s in ("cli", "cron", "local")}})

    packet = seen(k.build_packet("intake"))
    same = k.build_packet("intake")
    sizes = [len(i["excerpt"]) for i in packet["items"]]
    long_item = next(i for i in packet["items"] if i["ref"] == long_ref)
    check("K2 intake packet: one per period, head/tail excerpts, <=15 items and <=24k chars, schema+budget",
          same["id"] == packet["id"] and len(packet["items"]) <= 15 and sum(sizes) <= 24000 and max(sizes) <= 8200
          and long_item["attachment"] and set(packet) >= {"id", "type", "items", "schema", "budget"},
          {"items": len(packet["items"]), "excerpt_chars": sizes, "budget": packet["budget"]})

    reads = [seen(k.read(packet["id"], long_ref, offset)) for offset in (0, 12000, 24000)]
    check("K3 read: <=12k per call, <=24k per item per packet",
          [len(r.get("text", "")) for r in reads] == [12000, 12000, 0] and reads[2]["available"] is False,
          [{"len": len(r.get("text", "")), "available": r["available"]} for r in reads])

    results = [{"ref": i["ref"], "disposition": "saved", "facts": ["prefers Friday reminders"],
                **({"goal_hint": "new"} if "Remind" in i["excerpt"] else {})} for i in packet["items"]]
    out = k.submit(packet["id"], results + [{"ref": "chat:999", "disposition": "saved"}])
    after = k.build_packet("intake")
    check("K4 submit: idempotent per item, foreign refs refused, empty packet stays silent",
          [o.get("state") for o in out[:-1]] == ["disposed"] * 3 and "not in this packet" in out[-1]["error"]
          and k.submit(packet["id"], results)[0]["state"] == "disposed" and after["id"] is None
          and k.store.get(packet["id"])["state"] == "submitted",
          {"out": out, "after": after["id"]})

    # A signal handed out 3 times without a disposition parks and stops blocking the queue.
    host.add("chat", "user", "something ambiguous", T0 + 10)
    for hour in range(3):
        clock[0] = T0 + 60 + hour * H
        k.tick()
        k.build_packet("intake")
    clock[0] += H
    k.tick()
    parked = [s for s in k.store.query("signal") if s["payload"]["excerpt"] == "something ambiguous"][0]
    check("K5 signal: 3 hand-outs -> parked", parked["state"] == "parked" and parked["attempts"] == 3,
          {"state": parked["state"], "attempts": parked["attempts"]})

    # Silence-triggered intake wake: 5 min of silence, waiting signals, at most 3 per day.
    host.add("chat", "user", "new thought", clock[0] - 1)
    k.tick()
    wakes = [intake.quiet_wake(k, clock[0] - 400, clock[0]) for _ in range(4)]
    check("K6 quiet wake: after 5 min silence, <=3 per day", wakes == [True, True, True, False]
          and not intake.quiet_wake(k, clock[0] - 60, clock[0]), wakes)
    return k, host, clock


def patrol(k, items, verdict="send", priority="urgent", source="mail:work", text="Security alert", topic=None):
    packet = seen(k.build_packet("patrol"))
    observed = k.observe(packet["id"], source, items)
    results = [{"candidate_ref": o["ref"], "verdict": verdict, "text": text, "priority": priority,
                **({"topic": topic} if topic else {})} for o in observed["items"]]
    return packet, observed, k.submit(packet["id"], results)


def part_notifier(base):
    clock = [T0]
    host = FakeHost(lambda: clock[0])
    k = Kernel(make_home(base, {"blocked_topics": ["crypto"]}), adapter=host, clock=lambda: clock[0])
    railway = {"upstream_id": "gmail:message:1a169d66fac6d167", "title": "Railway: environment variable disclosure"}
    outcomes = []
    for day, hour in ((0, 0), (2, 1), (2, 3), (2, 5)):  # the audited pattern: once, then three times two days later
        clock[0] = T0 + day * D + hour * H
        _, observed, out = patrol(k, [railway])
        outcomes.append((observed["items"][0]["notified"], out[0]["result"]))
        k.tick()
        deliver_all(k, host)
    digitalplat = {"upstream_id": "1a180a721fc10d92", "title": "DigitalPlat domain renewal"}
    for i in range(7):
        clock[0] = T0 + 3 * D + i * 600
        patrol(k, [{**digitalplat, "title": digitalplat["title"] + ("" if i % 2 else " (reminder)")}], priority="time_sensitive")
        k.tick()
        deliver_all(k, host)
    by_key = {}
    for e in k.store.query("notice"):
        by_key.setdefault(e["natural_key"], []).append(e["state"])
    rail, plat = by_key.get("src:1a169d66fac6d167", []), by_key.get("src:1a180a721fc10d92", [])
    check("A5 dedupe replay: Railway x4 and DigitalPlat x7 -> one notice, one delivery each",
          rail == ["sent"] and plat == ["sent"] and len(host.delivered) == 2,
          {"railway": rail, "digitalplat": plat, "delivered": len(host.delivered), "railway_rounds": outcomes})

    clock[0] += H
    _, _, out = patrol(k, [railway], verdict="supersede", text="Railway: rotation completed, new details")
    k.tick()
    rail = [e for e in k.store.query("notice") if e["natural_key"] == "src:1a169d66fac6d167"]
    check("N1 supersede: explicit, new evidence, new generation and delivery key",
          out[0]["result"] == "superseded" and len(rail) == 2 and len(host.delivered) == 3
          and len({e["payload"]["delivery_key"] for e in rail}) == 2, [(e["state"], e["payload"].get("generation")) for e in rail])

    clock[0] = datetime(2026, 11, 5, 9, 30, tzinfo=UTC).timestamp()
    weak = {"title": "Weekly digest ready"}
    patrol(k, [weak], priority="ordinary", source="web:news")
    k.tick()
    deliver_all(k, host)
    clock[0] += 600
    patrol(k, [weak], priority="ordinary", source="web:news")
    k.tick()
    weak_rows = [e for e in k.store.query("notice") if e["natural_key"].startswith("weak:")]
    check("N2 weak key: dedupes unsent candidates only, never blocks on sent history; budget 1/day defers the second",
          sorted(e["state"] for e in weak_rows) == ["scheduled", "sent"]
          and datetime.fromtimestamp(next(e for e in weak_rows if e["state"] == "scheduled")["payload"]["next_eligible"], UTC)
          == datetime(2026, 11, 6, 9, 0, tzinfo=UTC), [(e["state"], e["payload"].get("next_eligible")) for e in weak_rows])

    clock[0] = datetime(2026, 11, 5, 23, 0, tzinfo=UTC).timestamp()
    patrol(k, [{"upstream_id": "cal:evt-1", "title": "Meeting moved"}], priority="time_sensitive", source="calendar:home")
    patrol(k, [{"upstream_id": "mail:<outage@example.org>", "title": "Outage"}], priority="urgent")
    k.tick()
    quiet = {e["natural_key"]: e for e in k.store.query("notice")}
    held, urgent = quiet["src:cal:evt-1"], quiet["src:outage@example.org"]
    check("N3 quiet hours: time_sensitive waits for 09:00, urgent goes at once",
          held["state"] == "scheduled" and datetime.fromtimestamp(held["payload"]["next_eligible"], UTC).hour == 9
          and urgent["state"] == "sending", {"held": held["state"], "urgent": urgent["state"]})

    for n in (2, 3):  # urgent_per_day (default 2): the third urgent of the night is gated as time_sensitive
        patrol(k, [{"upstream_id": f"mail:<urgent{n}@example.org>", "title": f"Urgent {n}"}], priority="urgent")
        k.tick()
    crypto = patrol(k, [{"upstream_id": "mail:<coin@example.org>", "title": "Token sale"}], priority="urgent", topic="crypto")
    k.tick()
    fade = patrol(k, [{"upstream_id": "mail:<f@example.org>", "title": "x"}], priority="urgent", topic="goal-fade")
    quiet = {e["natural_key"]: e for e in k.store.query("notice")}
    second, third = quiet["src:urgent2@example.org"], quiet["src:urgent3@example.org"]
    check("N3b urgent_per_day caps quiet-hour urgent sends (the rest wait as time_sensitive); a patrol topic in "
          "blocked_topics is withdrawn; the dormancy topic is reserved",
          notifier.spent_today(k.store, "urgent", clock[0], UTC) == 2  # the superseded Railway and Outage today
          and all(n["state"] == "scheduled" and n["payload"]["priority"] == "time_sensitive"
                  and n["payload"]["requested_priority"] == "urgent"
                  and datetime.fromtimestamp(n["payload"]["next_eligible"], UTC).hour == 9 for n in (second, third))
          and quiet["src:coin@example.org"]["state"] == "withdrawn" and "reserved" in fade[2][0].get("error", ""),
          {"urgent_spent": notifier.spent_today(k.store, "urgent", clock[0], UTC),
           "held": [[n["state"], n["payload"]["priority"]] for n in (second, third)],
           "crypto": quiet["src:coin@example.org"]["state"], "fade": fade[2][0]})

    # Crash/host error before the host saw the delivery: reconcile resends under the same key, once.
    clock[0] = datetime(2026, 11, 6, 10, 0, tzinfo=UTC).timestamp()
    host.send_errors = 1
    patrol(k, [{"upstream_id": "https://status.example.org/incidents/42?utm_source=x", "title": "Incident 42"}])
    k.tick()
    pending = [e for e in k.store.query("notice", state="sending") if e["natural_key"].endswith("incidents/42")][0]
    k.tick()
    key = pending["payload"]["delivery_key"]
    host.receipts[key] = {"status": "failed", "reason": "host_config", "detail": "invalid_params field=cwd", "job_id": "job-x"}
    k.tick()
    failed = k.store.get(pending["id"])
    check("N4 delivery: host error retried under the same key once; failed(host_config) recorded, not unknown",
          list(host.delivered).count(key) == 1 and failed["state"] == "failed" and failed["payload"]["receipt"]["reason"] == "host_config",
          {"state": failed["state"], "receipt": failed["payload"].get("receipt")})

    # Stale scheduled notice goes back to patrol; only send/skip/withdraw apply to it.
    clock[0] += 2 * D
    packet = seen(k.build_packet("patrol"))
    due = [i for i in packet["items"] if i["kind"] == "notice"]
    out = k.submit(packet["id"], [{"candidate_ref": due[0]["ref"], "verdict": "withdraw"}]) if due else []
    check("N5 patrol packet: stale notices re-verified (<=5), sources due listed, withdraw by verdict",
          due and due[0]["why"] == "stale" and out[0]["result"] == "withdrawn" and len(due) <= 5
          and any(s["source_id"] == "mail:work" for s in packet["context"]["sources_due"]),
          {"due": [(i["ref"], i["why"]) for i in due], "sources_due": [s["source_id"] for s in packet["context"]["sources_due"]]})

    keys = [e["payload"]["delivery_key"] for e in k.store.query("notice") if e["payload"].get("delivery_key")]
    check("N6 delivery keys: digest of the notice key, all match [a-z0-9][a-z0-9_-]{0,63}",
          keys and all(KEY.fullmatch(x) for x in keys), {"count": len(keys), "sample": keys[:2]})

    # Concurrency: two kernels (processes) dispatch the same notice and build the same packet.
    clock[0] += H
    patrol(k, [{"upstream_id": "gmail:message:race", "title": "Race"}])
    other = Kernel(k.home, adapter=host, clock=lambda: clock[0])
    before = len(host.delivered)
    ids = []
    threads = [threading.Thread(target=lambda kk=kk: (notifier.dispatch(kk, clock[0]), ids.append(kk.build_packet("patrol", clock[0] + 1800)["id"])))
               for kk in (k, other)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("N7 concurrency: one delivery, one issued packet per job and period",
          len(host.delivered) - before == 1 and len(set(ids)) == 1, {"delivered": len(host.delivered) - before, "packets": ids})

    # Source registry.
    for _ in range(4):
        four = sources.record(k.store, "calendar:apple", limitation_code="no_attendees", now=clock[0])
    five = sources.record(k.store, "calendar:apple", limitation_code="no_attendees", now=clock[0])
    for _ in range(3):
        failing = sources.record(k.store, "mail:old", error="auth expired", now=clock[0])
    check("S1 sources: same limitation 5x -> limited (no gap); 3 errors -> failing + health; intervals per kind",
          four["state"] == "ok" and four["payload"]["gap"] and five["state"] == "limited" and not five["payload"]["gap"]
          and failing["state"] == "failing" and failing["payload"]["health"]
          and sources.interval("mail:work") == 1800 and sources.interval("calendar:x", {"calendar": 30}) == 1800,
          {"four": four["state"], "five": five["state"], "failing": failing["payload"].get("fail_streak")})
    return k


def write_goal(home, slug, **fields):
    record = {"id": slug, "title": fields.pop("title", slug), "status": "active", "revision": 1, "parent_id": None,
              "created": T0 - 10 * D, "updated": T0 - 10 * D, "deadline": None, "session_id": "chat", "progress": [],
              "owner": None, "next_check_at": T0 - 9 * D, "waiting_user": False, **fields}
    path = home / "muse" / "workspace" / "goals" / slug / "GOAL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render(record, f"# {record['title']}\n\nBody.\n"))


def part_goals(base):
    clock = [T0]
    host = FakeHost(lambda: clock[0])
    home = make_home(base, {"ordinary_per_day": None})
    k = Kernel(home, adapter=host, clock=lambda: clock[0])
    write_goal(home, "old-goal", title="Old goal")
    write_goal(home, "revive-goal", title="Revive goal")
    write_goal(home, "kept-goal", title="Kept goal", no_fade=True)
    write_goal(home, "fresh", title="Fresh goal", created=T0 - D, updated=T0 - D, next_check_at=T0 + H)
    k.store.create("watch", "goal:old-goal:watch:1", {"job_id": "job-watch-old", "goal_id": "old-goal", "expires": None},
                   deadline=T0 + 30 * D, now=T0)
    k.store.create("watch", "goal:fresh:watch:1", {"job_id": "job-watch-fresh", "goal_id": "fresh", "expires": None},
                   deadline=T0 - 1, now=T0 - 30 * D)
    k.store.create("watch", "goal:fresh:watch:2", {"job_id": "job-watch-user", "goal_id": "fresh", "expires": T0 - 1},
                   deadline=T0 - 1, now=T0 - 30 * D)
    report = k.tick()
    asks = {e["natural_key"]: e for e in k.store.query("notice") if e["payload"].get("topic") == notifier.FADE_TOPIC}
    renewed = k.store.query("watch", natural_key="goal:fresh:watch:1")[0]
    check("G1 sync + watches: mirror from GOAL.md; open goal's watch renewed, user-expired watch expired and paused",
          report["goals_synced"] == 4 and renewed["state"] == "active" and renewed["deadline"] == T0 + goals.WATCH_RENEW
          and k.store.query("watch", natural_key="goal:fresh:watch:2")[0]["state"] == "expired" and "job-watch-user" in host.paused,
          {"report": report, "paused": host.paused})
    check("G2 dormancy: one question per dormant goal (watch, no_fade exempt), code-keyed",
          sorted(asks) == ["goal:revive-goal:fade:20261026:1"], sorted(asks))

    ask = asks["goal:revive-goal:fade:20261026:1"]
    try:
        notifier.withdraw(k, ask["id"], reason="patrol:withdraw", now=clock[0])
        refused = False
    except PermissionError:
        refused = True
    host.receipts[k.store.get(ask["id"])["payload"]["delivery_key"]] = {"status": "failed", "reason": "transport"}
    clock[0] = T0 + 13 * H  # 23:00: the re-ask waits for waking hours
    k.tick()  # the first question never arrived
    second = k.store.query("notice", natural_key="goal:revive-goal:fade:20261026:2")
    check("G3 fade question: model cannot withdraw it; a failed delivery is asked again (attempt 2)",
          refused and second and second[0]["state"] in ("scheduled", "sending"), {"refused": refused, "second": [s["state"] for s in second]})

    # The user answers (main session goal_update): the goal revives and the open question is withdrawn by code.
    goals.update(k, "revive-goal", {"progress_note": "user: still on it"}, by="user", now=clock[0])
    k.tick()
    second = k.store.get(second[0]["id"])
    check("G4 revival withdraws the open question", second["state"] == "withdrawn", second["state"])

    clock[0] = T0 + D

    # A dormant goal whose question was delivered a day earlier is cancelled by code at fade_at.
    write_goal(home, "fade-me", title="Fade me", created=T0 - 15 * D, updated=T0 - 15 * D)
    k.store.create("watch", "goal:fade-me:watch:1", {"job_id": "job-watch-fade", "goal_id": "fade-me", "expires": T0 + 9 * D},
                   deadline=T0 + 9 * D, now=T0)
    k.tick()
    w = k.store.query("watch", natural_key="goal:fade-me:watch:1")[0]
    k.store.transition(w["id"], "stopped", state="active", version=w["version"], reason="test:user_stop")
    k.tick()
    ask = [e for e in k.store.query("notice") if e["natural_key"].startswith("goal:fade-me:fade:")][0]
    deliver_all(k, host)
    clock[0] += D + 60
    k.tick()
    record = goals.read(home, "fade-me")[0]
    mirror = k.store.get("goal:fade-me")
    check("G5 fade: question delivered >= 1 day and fade_at reached -> code cancels GOAL.md and the mirror",
          k.store.get(ask["id"])["state"] == "sent" and record["status"] == "cancelled" and mirror["state"] == "cancelled"
          and "Closed by Muse" in record["progress"][-1]["note"], {"status": record["status"], "mirror": mirror["state"]})

    # Old goal: watch keeps it exempt; once the goal closes, its watch stops and its host job is paused.
    goals.update(k, "old-goal", {"status": "done"}, by="user", now=clock[0])
    k.tick()
    check("G6 closed goal stops its watch and pauses the host job",
          k.store.query("watch", natural_key="goal:old-goal:watch:1")[0]["state"] == "stopped" and "job-watch-old" in host.paused,
          host.paused)

    # next_check: any touch moves it; cron may only append; external edits are picked up by sync.
    clock[0] += H
    before = goals.read(home, "fresh")[0]["next_check_at"]
    goals.assess(k, "fresh", "on_track", "made progress", now=clock[0])
    rec = goals.read(home, "fresh")[0]
    try:
        goals.update(k, "fresh", {"status": "done"}, by="cron", now=clock[0])
        cron_status = True
    except PermissionError:
        cron_status = False
    write_goal(home, "fresh", title="Fresh goal (renamed by user)", created=T0 - D, updated=clock[0], next_check_at=clock[0] + 5 * H)
    k.tick()
    check("G7 next_check: touch -> now+1d, cron cannot change status, mirror follows GOAL.md edits",
          rec["next_check_at"] == clock[0] + D and before != rec["next_check_at"] and not cron_status
          and rec["progress"][-1]["by"] == "cron" and k.store.get("goal:fresh")["payload"]["title"] == "Fresh goal (renamed by user)"
          and k.store.get("goal:fresh")["deadline"] == clock[0] + 5 * H, {"next_check_at": rec["next_check_at"]})

    # Nightly: due goals, review candidates and goal proposals in one packet; cron goal creation needs a user signal.
    host.add("chat", "user", "fix the deploy script", clock[0] - 5 * H)
    host.add("chat", "assistant", "Done, fixed.", clock[0] - 5 * H + 5)
    host.add("chat", "user", "run the tests", clock[0] - 4 * H)
    host.add("chat", "tool", '{"success": false, "error": "exit 1"}', clock[0] - 4 * H + 5, tool="terminal")
    host.add("chat", "assistant", "The tests fail.", clock[0] - 4 * H + 6)
    host.add("chat", "user", "不是这个，我说的是另一个仓库", clock[0] - 3 * H)
    need = host.add("chat", "user", "remind me to renew the passport next month", clock[0] - 2 * H)
    host.add("chat", "user", "thanks", clock[0] - H)
    for i in range(12):
        host.add("burst", "user", f"ship build {i}", clock[0] - 50 * 60 + i)
        host.add("burst", "assistant", f"Deployed build {i}.", clock[0] - 50 * 60 + i + 0.5)
    k.tick()
    every_rule = {r for i in review.candidates(k, clock[0], 30) for r in i["rules"]}
    signal = k.store.query("signal", natural_key=need)[0]
    intake.dispose(k, signal["natural_key"], "saved", ["passport renewal next month"], "new", clock[0])
    packet = seen(k.build_packet("nightly"))
    kinds = [i["kind"] for i in packet["items"]]
    reviews = [i for i in packet["items"] if i["kind"] == "review"]
    check("R1 review pick: <=10, newest first, rules claim/tool failure/correction/need",
          len(reviews) == 10 and reviews[0]["at"] >= reviews[-1]["at"]
          and every_rule == {"claim_without_write", "tool_failure_unretried", "user_correction", "unrecorded_need"},
          {"kinds": kinds, "rules_all_candidates": sorted(every_rule)})
    results = [{"candidate_ref": i["ref"], "verdict": "agent_gap" if n == 0 else "fine", "reason": "claimed without evidence"}
               for n, i in enumerate(reviews)]
    results += [{"goal_id": "revive-goal", "assessment": "stalled"}] if "goal" in kinds else []
    proposal = next(i for i in packet["items"] if i["kind"] == "signal")
    results.append({"create": {"signal_ref": proposal["ref"], "title": "Renew passport", "criteria": "Passport renewed"}})
    out = k.submit(packet["id"], results)
    created = out[-1]
    again = k.submit(packet["id"], results[-1:])
    older_reviews = review.candidates(k, clock[0], 30)
    check("R2 review/goals submit: one finding per turn, goal from a user signal once, picked turns never re-picked",
          created.get("result") == "created" and again[0]["result"] == "existing"
          and len(k.store.query("finding", state="open")) == 1
          and not {i["ref"] for i in older_reviews} & {i["ref"] for i in reviews},
          {"created": created, "open_findings": len(k.store.query("finding", state="open")), "next_reviews": len(older_reviews)})

    # Runs: closed from host status and packet dispositions; abandoned when the host never heard of it.
    k.start_run("nightly", job_id="job-n", execution_id="e-ok", packet_id=packet["id"])
    k.start_run("nightly", job_id="job-n", execution_id="e-fail")
    k.start_run("nightly", job_id="job-n", execution_id="e-lost")
    k.start_run("nightly", job_id="job-n", execution_id="e-flaky")
    host.outcomes.update({"e-ok": {"status": "completed"}, "e-fail": {"status": "failed"},
                          "e-flaky": {"execution_id": None, "status": "host_error"}})  # what Cron.outcome gives on a fault
    k.tick()
    clock[0] += 3 * H
    k.tick()
    states = {r["natural_key"]: r["state"] for r in k.store.query("run")}
    check("R3 runs: degraded/failed from host + packet, abandoned when the host has no record, kept when the host read failed",
          states == {"e-ok": "degraded", "e-fail": "failed", "e-lost": "abandoned", "e-flaky": "running"}, states)
    return k


def a3_a10(kernels):
    missing = []
    for k in kernels:
        db = sqlite3.connect(k.store.path)
        from hermes_muse.kernel.store import MACHINES
        for kind, m in MACHINES.items():
            for state in m.ttl:
                missing += db.execute("SELECT id FROM entity WHERE kind=? AND state=? AND deadline IS NULL", (kind, state)).fetchall()
        db.close()
    check("A3 every non-terminal entity has a deadline", not missing, {"missing": missing})
    hits = sum(len(p.findall(text)) for text in SEEN_TEXT for p, _ in SECRETS[1:])
    hits += sum(len(re.findall(r"(?:hunter2xyz)", text)) for text in SEEN_TEXT)
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, (list, dict)):
            for v in value.values() if isinstance(value, dict) else value:
                yield from strings(v)
    biggest = max(len(x) for text in SEEN_TEXT for x in strings(json.loads(text)))
    check("A10 nothing handed to the model holds a credential pattern; no single text field > 12k chars",
          hits == 0 and biggest <= 12000, {"credential_hits": hits, "largest_item_chars": biggest, "payloads": len(SEEN_TEXT)})


def part_native(base):
    try:
        from hermes_state import SessionDB
    except ImportError:
        print("NATIVE SKIP hermes-agent not importable (set PYTHONPATH to hermes-agent)")
        return
    home = base / "native"
    (home / "workspace").mkdir(parents=True)
    (home / "config.yaml").write_text("timezone: UTC\nplugins:\n  enabled: [hermes-muse]\n")
    os.environ.update(HERMES_HOME=str(home), HERMES_IGNORE_USER_CONFIG="1")
    SessionDB(db_path=home / "state.db").close()
    import time
    now = time.time()
    db = sqlite3.connect(home / "state.db")
    for sid, source in (("chat", "cli"), ("once", "oneshot"), ("odd", "local")):
        db.execute("INSERT INTO sessions (id, source, chat_type, title, started_at) VALUES (?,?,?,?,?)", (sid, source, "dm", None, now - 60))
    for i, (sid, text) in enumerate((("chat", "Plan the trip; token: zq81kd0"), ("once", "Reply only OK"), ("odd", "legacy"))):
        db.execute("INSERT INTO messages (session_id, role, content, timestamp, message_uid) VALUES (?,?,?,?,?)",
                   (sid, "user", text, now - 30 + i, f"u{i}"))
    db.commit()
    db.close()
    k = Kernel(home)
    k.tick()
    signals = k.store.query("signal")
    packet = k.build_packet("intake")
    page = k.read(packet["id"], packet["items"][0]["ref"]) if packet["items"] else {}
    probe = {p["name"]: p for p in k.adapter.probe(captured_sources=intake.captured_sources(k.store, now - D))}
    check("NATIVE intake via HostAdapter: only the cli turn, redacted; unclassified 'local' reported by probe",
          [s["payload"]["source"] for s in signals] == ["cli"] and "zq81kd0" not in json.dumps(packet) + json.dumps(page)
          and page.get("available") and probe["signal_purity"]["status"] == "degraded" and "local" in probe["signal_purity"]["detail"],
          {"signals": len(signals), "purity": probe["signal_purity"]["detail"]})
    notice = notifier.draft(k, text="Native delivery check", priority="urgent", upstream_id="gmail:message:native1",
                            title="Native", now=now)
    notifier.dispatch(k, now)
    sent = k.store.get(notice["id"])
    key = sent["payload"].get("delivery_key", "")
    jobs = k.adapter.cron.jobs()
    check("NATIVE delivery: host accepted the code-derived key as a one-shot job in the temporary home (not run)",
          sent["state"] == "sending" and sent["payload"].get("job_id") in jobs
          and jobs[sent["payload"]["job_id"]]["name"] == f"muse-deliver-{key}" and k.adapter.deliver.receipt(key)["status"] == "sending",
          {"state": sent["state"], "key": key, "faults": list(k.adapter.faults)[-2:]})


def main():
    with tempfile.TemporaryDirectory(prefix="muse-kernel-") as temporary:
        base = Path(temporary).resolve()
        kernels = []
        for name, part in (("intake", part_intake), ("notifier", part_notifier), ("goals", part_goals)):
            (base / name).mkdir()
            result = part(base / name)
            kernels.append(result[0] if isinstance(result, tuple) else result)
        a3_a10(kernels)
        part_native(base)
    print(f"RESULT {'FAIL ' + ','.join(FAILED) if FAILED else 'PASS'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
