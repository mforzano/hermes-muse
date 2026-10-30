"""Run: one woken job execution. Its outcome comes from the host execution status and the packet's
dispositions, never from a model self-report. A closed run of a drain job relays: its batch is closed
(packets.close) and the job is woken again while work remains. A wake after a run that made progress is free
(the queue shrinks every time); at most WAKES a day follow a run that did not, so the relay always ends."""
from . import packets
from .common import H, count_today, patch

LEASE = 2 * H
WAKES = 12


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
    """Close runs whose host execution ended; extend a still-running one; abandon one the host has no record of
    (a host read error is neither)."""
    closed = []
    for run in k.store.query("run", state="running"):
        p = run["payload"]
        host = k.adapter.cron.outcome(p["job_id"], p["execution_id"]) if p.get("job_id") else None
        if host and host["status"] == "host_error":
            continue  # the host could not be read: not "no record"; the next tick asks again
        if host and host["status"] not in ("claimed", "running"):
            closed.append(patch(k.store, run["id"], outcome(k, host, p.get("packet_id")), expect=("running",),
                                reason=f"host:{host['status']}", now=now))
        elif run["deadline"] <= now:
            if host:
                patch(k.store, run["id"], reason="host_still_running", deadline=now + LEASE, now=now)
            else:
                closed.append(patch(k.store, run["id"], "abandoned", expect=("running",), reason="host_execution_unknown", now=now))
    closed = [run for run in closed if run]
    for run in closed:
        relay(k, run, now)
    return len(closed)


def relay(k, run, now):
    p = run["payload"]
    if not packets.JOBS.get(p.get("job"), {}).get("drain"):
        return
    progress = bool(p.get("packet_id")) and packets.close(k, p["packet_id"], run["state"], now)
    if packets.pending(k, p["job"], now) and (progress or count_today(k.store, f"drain_wakes:{p['job']}", now, k.tz(), WAKES)):
        k.adapter.cron.wake(p["job_id"])
