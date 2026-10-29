"""Notifier: code-derived keys, 7-day sent-history dedupe, quiet hours, daily budget, delivery through
host.deliver and reconcile from host receipts. The model only says send / skip / withdraw / supersede.

Keys come from kernel.store.notice_key (upstream ID; weak title+day key when there is none). A strong
key with a drafted/scheduled/sending/sent notice in the last 7 days returns that notice; a weak key only
meets unsent ones. Only supersede with a new observation makes a second notice for the same key.
"""
import hashlib
from datetime import datetime, timedelta

from .common import D, H, local_midnight, one, patch, preferences
from .store import notice_key

PRIORITIES = ("urgent", "time_sensitive", "ordinary")
VERDICTS = ("send", "skip", "withdraw", "supersede")
UNSENT = frozenset({"drafted", "scheduled"})
LIVE = frozenset({"drafted", "scheduled", "sending", "sent"})
SPENT = ("sending", "sent", "unknown")
VERIFY_EVERY = D      # evidence older than this goes back to patrol before it is sent
STUCK_AFTER = 6 * H   # eligible this long and still unsent: forced into the next patrol packet
FADE_TOPIC = "goal-fade"
TEXT_MAX = 2000


def delivery_key(source_key, generation=0):
    """Host delivery key ([a-z0-9][a-z0-9_-]{0,63}): a digest of the source key, stable across retries."""
    return "n" + hashlib.sha256(f"{source_key}#{generation}".encode()).hexdigest()[:40]


def check_text(text):
    text = str(text or "").strip()
    if not text or len(text) > TEXT_MAX:
        raise ValueError(f"notice text must be 1–{TEXT_MAX} characters")
    return text


def gate(priority, prefs, at, tz, spent=0):
    """Earliest send time for `priority` from `at` (see `effective` for urgent): urgent at once; others wait for waking hours and the
    daily allowance (tomorrow when spent). None when the allowance is 0 (this priority is off)."""
    if priority == "urgent":
        return at
    limit = prefs.get(f"{priority}_per_day")
    local = datetime.fromtimestamp(at, tz)
    if limit is not None and spent >= limit:
        if limit == 0:
            return None
        local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = prefs["start_hour"], prefs["end_hour"]
    awake = start <= local.hour < end if start < end else local.hour >= start or local.hour < end
    if not awake:
        opening = local.replace(hour=start, minute=0, second=0, microsecond=0)
        local = opening if opening > local else opening + timedelta(days=1)
    return local.timestamp()


def effective(store, priority, prefs, now, tz):
    """urgent passes quiet hours and budgets only within urgent_per_day; past it, it is gated as time_sensitive.
    A model-given priority or verdict never opens a gate by itself."""
    if priority == "urgent" and spent_today(store, "urgent", now, tz) >= prefs["urgent_per_day"]:
        return "time_sensitive"
    return priority


def spent_today(store, priority, now, tz):
    return sum(1 for e in store.query("notice", state=SPENT, since=local_midnight(now, tz))
               if e["payload"].get("priority", "ordinary") == priority)


def text_of(p):
    return p.get("text") or p.get("message") or ""  # v1 notices carry `message`


def render(p):
    sources = [s for s in p.get("sources") or [] if s][:5]
    return text_of(p) + ("\n\nSources: " + ", ".join(sources) if sources else "")


def stale(p, now):
    return p.get("topic") != FADE_TOPIC and (p.get("verified_at") or 0) < now - VERIFY_EVERY


def draft(k, *, text, priority="ordinary", title="", upstream_id=None, sources=(), source_id=None, topic="",
          goal_id=None, evidence=(), expires=None, observed_at=None, source_key=None, now):
    """Create (or return the live) notice for this source key and schedule it."""
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    if source_key:
        key, strong = source_key, True
    else:
        key, strong = notice_key(upstream_id, sources, title, now if observed_at is None else observed_at)
    payload = {"text": check_text(text), "priority": priority, "title": str(title or "")[:200], "upstream_id": upstream_id,
               "sources": [str(s)[:500] for s in sources or ()][:20], "source_id": source_id, "topic": topic or "",
               "goal_id": goal_id, "evidence": list(evidence)[:20], "strong_key": strong, "generation": 0, "verified_at": now}
    e = k.store.create("notice", key, payload, deadline=expires or now + 3 * D, now=now,
                       dedupe=(LIVE, 7 * D) if strong else (UNSENT, None))
    return e if e["existing"] else {**schedule(k, e, now), "existing": False}


def schedule(k, e, now, prefs=None):
    """drafted/scheduled -> scheduled with next_eligible; withdrawn when its topic or priority is turned off."""
    prefs = prefs or preferences(k.home)
    p, tz = e["payload"], k.tz()
    requested = p.get("priority", "ordinary")
    priority = effective(k.store, requested, prefs, now, tz)
    eligible = gate(priority, prefs, now, tz, spent_today(k.store, priority, now, tz))
    if p.get("topic") and p["topic"] in prefs["blocked_topics"] or eligible is None:
        why = "topic_blocked" if eligible is not None else "priority_off"
        return patch(k.store, e["id"], "withdrawn", expect=UNSENT, reason=why, now=now) or k.store.get(e["id"])
    over = {"priority": priority, "requested_priority": requested} if priority != requested else {}
    return patch(k.store, e["id"], "scheduled", expect=UNSENT, reason="schedule" + (":urgent_cap" if over else ""),
                 payload={"next_eligible": eligible, **over}, now=now) or k.store.get(e["id"])


def withdraw(k, notice_id, *, reason, now, by_code=False):
    e = k.store.get(notice_id)
    if e is None:
        raise KeyError(f"unknown notice {notice_id!r}")
    if e["payload"].get("topic") == FADE_TOPIC and not by_code:
        raise PermissionError("a dormancy question is withdrawn only by Muse, when its goal revives or closes")
    return patch(k.store, notice_id, "withdrawn", expect=UNSENT, reason=reason, now=now)


def supersede(k, old_id, *, text, evidence, now):
    """A new notice for the same source key with new evidence; the old one is superseded if still unsent."""
    if not evidence:
        raise ValueError("supersede needs new evidence (an observed item)")
    with k.store.atomic():
        old = k.store.get(old_id)
        if old["payload"].get("topic") == FADE_TOPIC:
            raise PermissionError("a dormancy question cannot be superseded")
        if old["state"] in ("sending", "unknown"):
            raise ValueError("the previous notice is still being delivered; decide after its receipt")
        if old["state"] in UNSENT:
            patch(k.store, old_id, "superseded", reason="superseded", now=now)
        drop = {"delivery_key", "job_id", "dispatched_at", "sent_at", "receipt", "target", "next_eligible", "message"}
        payload = {**{f: v for f, v in old["payload"].items() if f not in drop}, "text": check_text(text),
                   "evidence": list(evidence)[:20], "generation": old["payload"].get("generation", 0) + 1,
                   "supersedes": old_id, "verified_at": now}
        new = k.store.create("notice", old["natural_key"], payload, deadline=now + 3 * D, now=now, dedupe=False,
                             reason="supersede")
    return schedule(k, new, now)


def decide(k, item, verdict, *, text=None, priority=None, topic=None, now):
    """One patrol verdict on an observed item (send/skip/supersede) or a due notice (send/skip/withdraw)."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if item["kind"] == "observed":
        if verdict == "skip":
            return {"result": "skipped"}
        if verdict == "withdraw":
            raise ValueError("withdraw applies to a due notice; skip an observed item instead")
        sources = [item["url"]] if item.get("url") else []
        if verdict == "supersede":
            key, _ = notice_key(item.get("upstream_id"), sources, item["title"], item.get("ts") or now)
            live = one(k.store, "notice", key)
            if not live or live["state"] not in LIVE:
                raise ValueError("nothing was notified for this item; use send")
            e = supersede(k, live["id"], text=text, evidence=[item["ref"]], now=now)
            return {"result": "superseded", "notice": e["id"], "state": e["state"]}
        if topic == FADE_TOPIC:
            raise ValueError(f"topic {FADE_TOPIC!r} is reserved for Muse's dormancy questions")
        e = draft(k, text=text, priority=priority or "ordinary", title=item["title"], upstream_id=item.get("upstream_id"),
                  topic=str(topic or "").strip()[:100],
                  sources=sources, source_id=item.get("source_id"), observed_at=item.get("ts"), evidence=[item["ref"]], now=now)
        return {"result": "existing" if e["existing"] else "drafted", "notice": e["id"], "state": e["state"]}
    if verdict == "supersede":
        raise ValueError("supersede needs a new observation: observe the item and supersede its ref")
    if verdict == "send":
        e = patch(k.store, item["ref"], expect=UNSENT, reason="reverified", payload={"verified_at": now}, now=now)
        if e is None:
            return {"result": "not_pending"}
        e = schedule(k, e, now)
        return {"result": "reverified", "notice": e["id"], "state": e["state"]}
    e = withdraw(k, item["ref"], reason=f"patrol:{verdict}", now=now)
    return {"result": "withdrawn" if e else "not_pending", "notice": item["ref"]}


def due_items(k, now, limit=5):
    """Scheduled notices that need the model again: stale evidence, or eligible for 6h and still unsent."""
    rows = [e for e in reversed(k.store.query("notice", state="scheduled"))
            if e["payload"].get("topic") != FADE_TOPIC and (e["payload"].get("next_eligible") or 0) <= now
            and (stale(e["payload"], now) or (e["payload"].get("next_eligible") or 0) <= now - STUCK_AFTER)]
    return [{"kind": "notice", "ref": e["id"], "text": text_of(e["payload"])[:TEXT_MAX], "title": e["payload"].get("title"),
             "priority": e["payload"].get("priority", "ordinary"), "verified_at": e["payload"].get("verified_at"),
             "why": "stale" if stale(e["payload"], now) else "stuck"} for e in rows[:limit]]


def dispatch(k, now):
    """Send every scheduled notice whose gate is open now; returns how many were handed to the host."""
    store, handed = k.store, 0
    for e in reversed(store.query("notice", state="drafted")):
        schedule(k, e, now)
    prefs, tz = preferences(k.home), k.tz()
    target = prefs.get("deliver") or k.adapter.config.get().get("deliver") or "bot-chat"
    rank = {p: i for i, p in enumerate(PRIORITIES)}
    queue = sorted(store.query("notice", state="scheduled"),
                   key=lambda e: (rank.get(e["payload"].get("priority"), 2), e["payload"].get("next_eligible") or 0))
    for e in queue:
        if (e["payload"].get("next_eligible") or 0) > now or stale(e["payload"], now):
            continue
        with store.atomic():  # budget check and claim in one write transaction: two dispatchers cannot both spend it
            current = store.get(e["id"])
            if current["state"] != "scheduled":
                continue
            p = current["payload"]
            priority = p.get("priority", "ordinary")
            eligible = gate(priority, prefs, now, tz, spent_today(store, priority, now, tz))
            if (eligible is None or eligible > now or p.get("topic") in prefs["blocked_topics"]
                    or effective(store, priority, prefs, now, tz) != priority):
                schedule(k, current, now, prefs)
                continue
            moved = patch(store, e["id"], "sending", reason="dispatch", now=now, payload={
                "delivery_key": delivery_key(current["natural_key"], p.get("generation", 0)), "target": target,
                "dispatched_at": now})
        apply(k, moved, k.adapter.deliver.send(target, render(moved["payload"]), moved["payload"]["delivery_key"]), now)
        handed += 1
    return handed


def apply(k, e, receipt, now):
    """Move a sending/unknown notice by a host receipt; transient host errors leave it for the next tick."""
    status, reason = receipt.get("status"), receipt.get("reason")
    to = {"sent": "sent", "failed": "failed"}.get(status)
    if status == "unknown" and reason != "host_error":
        to = "unknown"
    if reason == "host_error":
        to = None
    payload = {"job_id": receipt.get("job_id") or e["payload"].get("job_id"),
               "receipt": {f: receipt[f] for f in ("outcome", "reason", "detail") if receipt.get(f)}}
    if to == "sent":
        payload["sent_at"] = now
    if to is None or to == e["state"]:
        if payload["job_id"] == e["payload"].get("job_id"):
            return e
        to = e["state"]
    return patch(k.store, e["id"], to, expect=(e["state"],), reason=f"receipt:{status}:{reason or ''}", payload=payload, now=now)


def reconcile(k, now):
    """Read receipts of sending/unknown notices. A notice claimed but never handed to the host (crash) is sent
    again under the same key, which the host turns into the existing receipt if it did arrive."""
    for e in k.store.query("notice", state=("sending", "unknown")):
        p = e["payload"]
        if not p.get("delivery_key"):
            continue  # migrated v1 delivery: its TTL closes it
        if e["state"] == "sending" and not p.get("job_id"):
            receipt = k.adapter.deliver.send(p["target"], render(p), p["delivery_key"])
        else:
            receipt = k.adapter.deliver.receipt(p["delivery_key"])
        apply(k, e, receipt, now)
