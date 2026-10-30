"""Health invariants over a read-only Store v2 and the HostAdapter (design 2.8, 7.3).

Each check returns one or more `finding(id, level, value, threshold, detail, evidence)`; level is
ok | low | medium | high. Checks only read; they never repair, so a kernel bug stays visible.
Payload fields read here are the kernel's contract: signal.source, notice.next_eligible/priority/strong_key,
goal.fade_at/fade_blocked (goals.fade), finding.evidence (review.record), source.health (sources.record).
Tests seed these through the kernel writers, never with hand-written payloads.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..kernel.store import MACHINES

H, D = 3600, 86400
LEVELS = ("ok", "low", "medium", "high")
PROBE_LEVEL = {"ok": "ok", "degraded": "medium", "missing": "high"}
MINOR_PROBES = {"config_location", "home"}  # degraded here never stops a feature
EVIDENCE = 10


def finding(id, level, value, threshold, detail="", evidence=(), fix=""):
    return {"id": id, "level": level, "value": value, "threshold": threshold, "detail": detail,
            "evidence": [str(e) for e in list(evidence)[:EVIDENCE]], "fix": fix if level != "ok" else ""}


def worst(*levels):
    return max(levels, key=LEVELS.index, default="ok")


def ids(rows):
    return [r["id"] for r in rows]


def probes(results):
    out = []
    for p in results:
        level = PROBE_LEVEL.get(p["status"], "high")
        if p["name"] in MINOR_PROBES and level == "medium":
            level = "low"
        out.append(finding(f"probe.{p['name']}", level, p["status"], "ok", p["detail"], fix=p["fix"]))
    return out


def host_faults(faults):
    calls = Counter(f["call"] for f in faults)
    return finding("host_faults", "medium" if faults else "ok", len(faults), 0,
                   "; ".join(f"{c} x{n}" for c, n in calls.most_common()) or "no host call failed",
                   [f["error"][:200] for f in faults],
                   "A Hermes API or database changed shape; run tests/native_host_contract.py")


# -- Store v2 ---------------------------------------------------------------------------------------
def deadlines(store, now):
    """A3 and the kernel watchdog: every non-terminal entity has a deadline, and tick() applies due exits."""
    missing, stalled = [], []
    for kind, m in MACHINES.items():
        for state, (_, exit_state) in m.ttl.items():
            for e in store.query(kind, state=state):
                if e["deadline"] is None:
                    missing.append(e)
                elif exit_state and e["deadline"] < now - H:
                    stalled.append(e)
    return [finding("deadlines", "high" if missing else "ok", len(missing), 0,
                    f"{len(missing)} non-terminal entities without a deadline", ids(missing),
                    "Kernel created a live entity without a deadline; fix the caller (A3)"),
            finding("tick_stalled", "high" if stalled else "ok", len(stalled), 0,
                    f"{len(stalled)} entities over 1h past a deadline with a code exit", ids(stalled),
                    "Store.tick() is not running; check the kernel pre-run and its errors")]


def signals(store, now):
    """A7 watermark age (oldest undisposed signal), parked count in 7 days."""
    live = sorted(store.query("signal", state="new"), key=lambda e: e["state_since"])
    age = (now - live[0]["state_since"]) / H if live else 0
    parked = store.query("signal", state="parked", since=now - 7 * D)
    return [finding("signal_watermark", "high" if age > 6 else "medium" if age > 2 else "ok", round(age, 2), "<2h",
                    f"{len(live)} signals waiting; oldest {age:.1f}h", ids(live[:1]),
                    "Intake packets are not disposing signals; check packets_idle and intake runs"),
            finding("signal_parked", "medium" if len(parked) >= 5 else "ok", len(parked), "<5",
                    f"{len(parked)} signals parked in 7 days (excerpt not disposed in 3 tries/72h)", ids(parked),
                    "Read the parked signals; long pastes may need a smaller intake packet")]


def packets(store, now):
    """A8 expiry rate, and the watchdog: work is queued but the kernel issued no packet for 2 hours."""
    recent = store.query("packet", since=now - 7 * D)
    done = [p for p in recent if p["state"] in ("submitted", "expired")]
    expired = [p for p in done if p["state"] == "expired"]
    rate = len(expired) / len(done) if done else 0.0
    queued = store.query("signal", state="new", limit=1) + store.query("notice", state=("drafted", "scheduled"), limit=1)
    latest = max((p["state_since"] for p in store.query("packet", limit=1)), default=None)
    idle = bool(queued) and (latest is None or latest < now - 2 * H)
    return [finding("packet_expiry", "medium" if rate >= 0.1 else "ok", round(rate, 3), "<0.1",
                    f"{len(expired)}/{len(done)} packets expired in 7 days", ids(expired),
                    "Packets are too large for the job's time budget; check the per-item timing"),
            finding("packets_idle", "high" if idle else "ok", int(idle), 0,
                    "work is queued but no packet was issued for 2h" if idle else "packets flowing",
                    ids(queued), "Kernel packet builder is stuck or the Muse cron jobs are not running")]


def notices(store, now):
    """7-day duplicate sends per strong notice key; hanging drafted/scheduled notices; closed unknowns."""
    groups = defaultdict(list)
    for n in store.query("notice", state="sent", since=now - 7 * D):
        if n["payload"].get("strong_key", n["natural_key"].startswith("src:")):
            groups[n["natural_key"]].append(n)
    dups = {k: g for k, g in groups.items() if len(g) > 1}
    urgent = any(n["payload"].get("priority") == "urgent" for g in dups.values() for n in g)
    hanging = [n for n in store.query("notice", state=("drafted", "scheduled"))
               if n["payload"].get("next_eligible", n["state_since"]) < now - 6 * H]
    closed = store.query("notice", state="closed_unknown", since=now - 7 * D)
    return [finding("notice_duplicates", ("high" if urgent else "medium") if dups else "ok", len(dups), 0,
                    f"{len(dups)} source keys sent more than once in 7 days",
                    [n["id"] for g in dups.values() for n in g], "Notifier dedupe against sent history failed (A5)"),
            finding("notice_hanging", "medium" if hanging else "ok", len(hanging), 0,
                    f"{len(hanging)} notices eligible for over 6h but not decided", ids(hanging),
                    "Patrol packets must carry due notices; check packet building"),
            finding("notice_unknown", "medium" if closed else "ok", len(closed), 0,
                    f"{len(closed)} notices closed after 48h unknown in 7 days", ids(closed),
                    "Delivery receipts are unreadable; see probe.delivery_semantics")]


def deliveries(store, executions, jobs, now):
    """Delivery failure within one cycle: host executions of Muse jobs, and v2 notices that failed or went unknown."""
    failed = [e for e in executions if e["delivery_outcome"] in ("failed", "not_configured")]
    broken = [e for e in executions if e["status"] in ("failed", "unknown")]
    lost = store.query("notice", state=("failed", "unknown"), since=now - D) if store else []
    errors = sorted({str(e["error"] or jobs.get(e["job_id"], {}).get("last_delivery_error") or e["delivery_outcome"])[:160]
                     for e in failed})
    return [finding("delivery", "high" if failed or lost else "ok", len(failed) + len(lost), 0,
                    f"{len(failed)} failed deliveries, {len(lost)} failed/unknown notices in 24h; {errors}",
                    [e["execution_id"] for e in failed] + ids(lost),
                    "Check probe.delivery_target and the job's last_delivery_error; configure health_deliver"),
            finding("executions", "medium" if broken else "ok", len(broken), 0,
                    f"{len(broken)} Muse cron executions failed or unknown in 24h",
                    [f"{e['execution_id']}: {str(e['error'])[:120]}" for e in broken],
                    "Read the execution error; a gateway restart explains one isolated failure")]


def runs(store, now):
    """Run results in 24h (ok >= 90%) and runs left running past their deadline."""
    finished = [r for r in store.query("run", since=now - D) if r["state"] != "running"]
    ok = sum(r["state"] == "ok" for r in finished)
    rate = ok / len(finished) if finished else 1.0
    stuck = [r for r in store.query("run", state="running") if r["deadline"] is not None and r["deadline"] < now]
    return [finding("runs_ok", "medium" if rate < 0.9 else "ok", round(rate, 3), ">=0.9",
                    f"{ok}/{len(finished)} runs ok in 24h", ids(r for r in finished if r["state"] != "ok"),
                    "Read the degraded/failed runs' reasons; limited sources must not degrade a run"),
            finding("runs_unclosed", "low" if stuck else "ok", len(stuck), 0,
                    f"{len(stuck)} runs running past 2h", ids(stuck),
                    "Run closing reads the host execution; check host_faults")]


def goals(store, now):
    """No active goal overdue more than 3 days; fade executed within 2 days of fade_at."""
    live = store.query("goal", state=("active", "waiting"))
    overdue = [g for g in live if g["state"] == "active" and g["deadline"] is not None and g["deadline"] < now - 3 * D]
    active = [g for g in live if g["state"] == "active"]
    faded = [g for g in active if (g["payload"].get("fade_at") or now) < now - 2 * D]
    stuck = [g for g in active if g["payload"].get("fade_blocked")]
    return [finding("goals_overdue", "medium" if overdue else "ok", len(overdue), 0,
                    f"{len(overdue)} active goals with next_check over 3 days old", ids(overdue),
                    "Nightly goals packets are not touching due goals"),
            finding("goal_fade", "medium" if faded or stuck else "ok", len(faded) + len(stuck), 0,
                    f"{len(faded)} goals over 2 days past fade_at and still open; "
                    f"{len(stuck)} whose dormancy question was withdrawn every time", ids(faded + stuck),
                    "Fade ask was not delivered, the code cancel did not run, or the question keeps being withdrawn")]


def sources(store, now):
    failing = [s for s in store.query("source", state="failing") if s["payload"].get("health")]
    return finding("sources_failing", "medium" if failing else "ok", len(failing), 0,
                   f"{len(failing)} sources failing 3+ times in a row", ids(failing),
                   "Check the source's tool or credentials")


def findings(store, now):
    recent = store.query("finding", since=now - D)
    bare = [f for f in store.query("finding", state="open") if not f["payload"].get("evidence")]
    return finding("review_findings", "low" if bare or len(recent) > 10 else "ok", len(recent), "<=10/day, all with evidence",
                   f"{len(recent)} findings in 24h; {len(bare)} open without evidence", ids(bare),
                   "Review candidates must stay within 10/day and cite evidence")


STORE_CHECKS = (deadlines, signals, packets, notices, runs, goals, sources, findings)


# -- Host --------------------------------------------------------------------------------------------
def tools(results):
    """A9 tool error rate per job (<3%) and A10 oversized results / credential patterns sent to the model."""
    by_job = defaultdict(Counter)
    for s in results:
        by_job[s["job_id"]].update({k: s[k] for k in ("tools", "failed", "oversized", "secrets")})
    noisy = {j: c["failed"] / c["tools"] for j, c in by_job.items() if c["tools"] and c["failed"] / c["tools"] >= 0.03}
    exposed = [s["session_id"] for s in results if s["oversized"] or s["secrets"]]
    return [finding("tool_errors", "medium" if noisy else "ok", round(max(noisy.values(), default=0), 3), "<0.03 per job",
                    "; ".join(f"{j} {r:.0%}" for j, r in noisy.items()) or f"{len(results)} Muse cron sessions in 24h",
                    [s["session_id"] for s in results if s["job_id"] in noisy and s["failed"]],
                    "Repeated tool errors are a schema/prompt contract gap; read the failing calls"),
            finding("model_input", "medium" if exposed else "ok", len(exposed), 0,
                    f"oversized (>20k) {sum(s['oversized'] for s in results)}, credential patterns "
                    f"{sum(s['secrets'] for s in results)} in Muse cron tool results", exposed,
                    "A read path skipped redaction or the excerpt limit")]


def jobs(owned, now):
    """Scheduled Muse jobs fire on time; a pause carries a reason."""
    scheduled = {j: job for j, job in owned.items() if "-deliver-" not in str(job.get("script"))}
    late = [j for j, job in scheduled.items() if job.get("enabled") and job.get("state") == "scheduled"
            and job.get("next_run_at") and _epoch(job["next_run_at"]) < now - H]
    silent = [j for j, job in scheduled.items() if (job.get("paused_at") or job.get("state") == "paused")
              and not job.get("paused_reason")]
    return [finding("jobs_late", "high" if late else "ok", len(late), 0,
                    f"{len(late)} Muse jobs over 1h past next_run_at", late,
                    "The Hermes scheduler is not ticking; check the gateway"),
            finding("jobs_paused", "low" if silent else "ok", len(silent), 0,
                    f"{len(silent)} Muse jobs paused without a reason", silent,
                    "Confirm the pause was intended; resume it otherwise")]


def _epoch(stamp):
    from datetime import datetime
    return datetime.fromisoformat(stamp).timestamp()
