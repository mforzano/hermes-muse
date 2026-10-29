"""Pre-run script of every Muse cron job (cron_entry.py): decides whether the model wakes and with what.

Prints one JSON line. `{"wakeAgent": false, ...}` keeps the run silent: plugin disabled, migration
marker present, empty packet, unknown key or a watch that is no longer active. Otherwise the line carries
the job's packet from build_packet (ids, refs, schema and budget filled in by code), which Hermes hands to
the model as Script Output, and the run is registered with start_run so its outcome is computed later.
"""
import time

from . import health
from .host import HostAdapter
from .install import JOBS, migrating, owned
from .kernel import Kernel, goals
from .kernel.common import D

WEEK = 7 * D


def silent(reason):
    return {"wakeAgent": False, "reason": reason}


def weekly(k, now):
    """Inputs of the weekly digest: the last health.json and 7-day counts from Store v2."""
    def count(kind, state):
        return len(k.store.query(kind, state=state, since=now - WEEK))
    return {"health": health.load(k.home) or {"available": False},
            "week": {"notices_sent": count("notice", "sent"), "notices_failed": count("notice", ("failed", "closed_unknown")),
                     "signals_disposed": count("signal", "disposed"), "signals_parked": count("signal", "parked"),
                     "runs": {s: count("run", s) for s in ("ok", "degraded", "failed", "abandoned")},
                     "findings_open": len(k.store.query("finding", state="open"))}}


def watch(k, key):
    job = owned(k.adapter, key)
    w = next((w for w in k.store.query("watch", state="active") if job and w["payload"].get("job_id") == job["id"]), None)
    if not w:
        return silent("no active watch for this job")
    p = w["payload"]
    found = goals.read(k.home, p.get("goal_id"))
    return {"wakeAgent": True, "job": "watch", "watch": {
        "id": w["id"], "goal_id": p.get("goal_id"), "goal_title": found[0].get("title") if found else None,
        "stop_condition": p.get("stop_condition"), "expires": w["deadline"]}}


def run(home, key, now=None):
    adapter = HostAdapter(home)
    if not adapter.config.enabled():
        return silent("plugin absent or disabled")
    if migrating(adapter.home):
        return silent("migration in progress")
    now = time.time() if now is None else now
    k = Kernel(adapter.home, adapter=adapter)
    k.tick(now)
    if key not in JOBS:
        return watch(k, key) if key.startswith("watch-") else silent(f"no Muse job {key!r}")
    job = JOBS[key][0]
    if job == "weekly":
        return {"wakeAgent": True, "job": job, **weekly(k, now)}
    packet = k.build_packet(job, now)
    if packet["id"] is None:
        return silent("nothing to do")
    cron_job = owned(adapter, key)
    execution = adapter.cron.outcome(cron_job["id"]) if cron_job else None
    if execution and execution["status"] in ("claimed", "running"):
        k.start_run(job, job_id=cron_job["id"], execution_id=execution["execution_id"], packet_id=packet["id"], now=now)
    return {"wakeAgent": True, "job": job, "packet": packet}
