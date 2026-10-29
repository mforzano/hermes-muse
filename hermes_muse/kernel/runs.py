"""Run: one woken job execution. Its outcome comes from the host execution status and the packet's
dispositions, never from a model self-report."""
from .common import H, patch

LEASE = 2 * H


def start(k, job, *, job_id, execution_id, packet_id=None, now):
    return k.store.create("run", str(execution_id), {"job": job, "job_id": job_id, "execution_id": execution_id,
                                                     "packet_id": packet_id}, deadline=now + LEASE, now=now)


def outcome(k, host, packet_id):
    if host["status"] in ("failed", "error"):
        return "failed"
    packet = k.store.get(packet_id) if packet_id else None
    if packet is None or not packet["payload"].get("items"):
        return "ok"
    refs = {i["ref"] for i in packet["payload"]["items"]}
    return "ok" if refs <= set(packet["payload"].get("handled", [])) else "degraded"


def check(k, now):
    """Close runs whose host execution ended; extend a still-running one; abandon one the host has no record of."""
    closed = 0
    for run in k.store.query("run", state="running"):
        p = run["payload"]
        host = k.adapter.cron.outcome(p["job_id"], p["execution_id"]) if p.get("job_id") else None
        if host and host["status"] not in ("claimed", "running"):
            closed += bool(patch(k.store, run["id"], outcome(k, host, p.get("packet_id")), expect=("running",),
                                 reason=f"host:{host['status']}", now=now))
        elif run["deadline"] <= now:
            if host:
                patch(k.store, run["id"], reason="host_still_running", deadline=now + LEASE, now=now)
            else:
                closed += bool(patch(k.store, run["id"], "abandoned", expect=("running",), reason="host_execution_unknown", now=now))
    return closed
