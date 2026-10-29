"""Packets: one bounded work packet per job and period, with code-filled refs, result schema and budget.

build(job) -> {id, type, items, schema, budget[, context]}. Same job and period: while a packet is issued,
every caller gets that same packet (lease = job period); an empty intake/nightly/feed packet is not stored
(id None) and the caller stays silent. Patrol always gets a packet: only the model can read its sources.
Item count adapts to the median seconds per item of the job's last 10 packets: clamp(0.6 T / median).
read/observe/submit accept only refs that belong to the packet (trust boundary).
"""
import hashlib
import json
import statistics

from . import goals, intake, notifier, review, sources
from .common import D, H, clip, patch
from .store import notice_key

EXCERPT_TOTAL = 24000
READ_MAX, READ_TOTAL = 12000, 24000
OBSERVE_MAX = 50
JOBS = {  # expected run seconds T, cron period (= packet lease), adaptive item bounds
    "intake": {"type": "intake", "expected": 180, "period": H, "items": (3, 15)},
    "patrol": {"type": "patrol", "expected": 360, "period": 30 * 60, "items": (2, 8)},
    "nightly": {"type": "nightly", "expected": 600, "period": D, "items": (2, 10)},
    "feed": {"type": "feed", "expected": 300, "period": D, "items": (1, 3)},
}
DUE_NOTICES, DUE_GOALS, FEED_GAP = 5, 5, 3 * D


def obj(required, **properties):
    return {"type": "object", "required": list(required), "properties": properties, "additionalProperties": False}


def enum(*values):
    return {"type": "string", "enum": list(values)}


S, REF = {"type": "string"}, {"type": "string", "description": "a ref from this packet"}
SCHEMAS = {  # design 2.6: one result shape per item kind group
    "intake": obj(["ref", "disposition"], ref=REF, disposition=enum(*intake.DISPOSITIONS),
                  facts={"type": "array", "items": S, "maxItems": 20},
                  goal_hint={"type": "string", "description": "an open goal id, or 'new' to propose a goal"}),
    "patrol": obj(["candidate_ref", "verdict"], candidate_ref=REF, verdict=enum(*notifier.VERDICTS),
                  text={"type": "string", "maxLength": notifier.TEXT_MAX}, priority=enum(*notifier.PRIORITIES)),
    "goals": obj([], goal_id=REF, assessment=enum(*goals.ASSESSMENTS), progress_note=S,
                 create=obj(["signal_ref", "title", "criteria"], signal_ref=REF, title=S, criteria=S)),
    "review": obj(["candidate_ref", "verdict", "reason"], candidate_ref=REF, verdict=enum(*review.VERDICTS),
                  reason={"type": "string", "maxLength": 500}),
    "feed": obj(["interest_ref"], interest_ref=REF,
                article=obj(["title", "body", "source_refs"], title=S, body=S, source_refs={"type": "array", "items": S, "minItems": 1})),
}
KIND_SCHEMA = {"intake": "intake", "observed": "patrol", "notice": "patrol", "goal": "goals", "signal": "goals",
               "review": "review", "interest": "feed"}
TYPE_SCHEMAS = {"intake": ("intake",), "patrol": ("patrol",), "nightly": ("goals", "review"), "feed": ("feed",)}


def size(k, job):
    spec = JOBS[job]
    low, high = spec["items"]
    rates = [p["payload"]["per_item"] for p in k.store.query("packet", state="submitted", limit=200)
             if p["payload"].get("job") == job and p["payload"].get("per_item")][:10]
    if not rates:
        return high
    return max(low, min(high, int(0.6 * spec["expected"] / statistics.median(rates))))


# -- builders: (items, context) ----------------------------------------------------------------------
def intake_items(k, n, now):
    items, chars = [], 0
    for e in sorted(k.store.query("signal", state="new"), key=lambda e: (e["payload"].get("created") or 0, e["id"])):
        item = intake.item(e)
        if items and (len(items) >= n or chars + len(item["excerpt"]) > EXCERPT_TOTAL):
            break
        items.append(item)
        chars += len(item["excerpt"])
    return items, None


def patrol_items(k, n, now):
    recent = [{"at": t["at"], "text": clip(t["text"], 500)} for t in k.adapter.sessions.user_turns(now - 6 * H)][-5:]
    return notifier.due_items(k, now, DUE_NOTICES), {
        "sources_due": sources.due(k.store, now, k.settings().get("source_intervals")),
        "recent_user_messages": recent, "send_limit": n}


def nightly_items(k, n, now):
    due = [goals.item(k, e) for e in k.store.due(now, kind="goal", limit=min(n, DUE_GOALS))]
    proposals = []
    for e in k.store.query("signal", state="disposed", since=now - D):
        if e["payload"].get("goal_hint") == "new" and not goals.read(k.home, goals.slug_for(e["natural_key"])):
            proposals.append({**intake.item(e), "kind": "signal", "facts": e["payload"].get("facts", [])})
    return due + review.candidates(k, now, n) + proposals[:n], None


def feed_items(k, n, now):
    fed = {e["payload"].get("interest_id") for e in k.store.query("feed", since=now - FEED_GAP)}
    items = [{"kind": "interest", "ref": e["id"], "title": e["payload"].get("title"), "rationale": e["payload"].get("rationale")}
             for e in reversed(k.store.query("interest", state="active")) if e["id"] not in fed]
    return items[:n], None


BUILDERS = {"intake": intake_items, "patrol": patrol_items, "nightly": nightly_items, "feed": feed_items}


def view(e, spec):
    p = e["payload"]
    out = {"id": e["id"], "type": spec["type"], "items": p["items"],
           "schema": {name: SCHEMAS[name] for name in TYPE_SCHEMAS[spec["type"]]}, "budget": p["budget"]}
    return {**out, "context": p["context"]} if p.get("context") else out


def build(k, job, now):
    if job not in JOBS:
        raise ValueError(f"job must be one of {', '.join(JOBS)}")
    spec, store = JOBS[job], k.store
    key = f"{job}:{int(now // spec['period'])}"
    issued = store.query("packet", state="issued", natural_key=key, limit=1)
    if issued:
        return view(issued[0], spec)
    n = size(k, job)
    items, context = BUILDERS[job](k, n, now)
    budget = {"seconds": int(0.6 * spec["expected"]), "items": n, "excerpt_chars": EXCERPT_TOTAL,
              "read_chars": READ_MAX, "read_total_per_item": READ_TOTAL, "lease_until": now + spec["period"]}
    if not items and job != "patrol":
        return {"id": None, "type": spec["type"], "items": [], "schema": {}, "budget": budget}
    with store.atomic():
        e = store.create("packet", key, {"job": job, "type": spec["type"], "items": items, "context": context,
                                         "budget": budget, "observed": [], "reads": {}, "handled": [], "issued_at": now},
                         deadline=now + spec["period"], now=now, dedupe=(frozenset({"issued"}), None))
        if not e["existing"]:
            store.attempt([store.query("signal", natural_key=i["ref"], limit=1)[0]["id"] for i in items if i["kind"] == "intake"], now)
    return view(e, spec)


def packet_of(k, packet_id):
    e = k.store.get(packet_id) if packet_id else None
    if not e or e["kind"] != "packet":
        raise KeyError(f"unknown packet {packet_id!r}")
    return e


def read(k, packet_id, ref, offset=0, now=None):
    """At most 12k redacted chars per call and 24k per item per packet of one item's original text."""
    packet = packet_of(k, packet_id)
    item = next((i for i in packet["payload"]["items"] if i["ref"] == ref), None)
    if item is None or not item.get("pointer"):
        raise KeyError(f"{ref!r} has no original text in this packet")
    allowed = min(READ_MAX, READ_TOTAL - packet["payload"]["reads"].get(ref, 0))
    if allowed <= 0:
        return {"ref": ref, "available": False, "reason": "read budget for this item is spent; dispose it from its excerpt"}
    page = k.adapter.sessions.read(item["pointer"], offset, allowed)
    used = len(page.get("text") or "")
    patch(k.store, packet["id"], reason="read", now=now,
          payload=lambda e: {"reads": {**e["payload"]["reads"], ref: e["payload"]["reads"].get(ref, 0) + used}})
    return {**page, "ref": ref}


def observe(k, packet_id, source_id, items, limitation_code=None, error=None, now=None):
    """Record a source check and its items (upstream IDs from the model) under packet refs o1, o2, …"""
    packet = packet_of(k, packet_id)
    if packet["payload"]["type"] != "patrol":
        raise ValueError("observe belongs to a patrol packet")
    if not isinstance(items, list) or len(items) > OBSERVE_MAX:
        raise ValueError(f"items must be a list of at most {OBSERVE_MAX}")
    source_id = sources.check_id(source_id)
    clean = []
    for raw in items:
        title = str((raw or {}).get("title") or "").strip()[:300]
        if not title:
            raise ValueError("every item needs a title")
        ts = raw.get("ts")
        clean.append({"kind": "observed", "source_id": source_id, "title": title,
                      "upstream_id": str(raw["upstream_id"])[:500] if raw.get("upstream_id") else None,
                      "url": str(raw["url"])[:1000] if raw.get("url") else None,
                      "ts": float(ts) if type(ts) in (int, float) else None})
    state = sources.record(k.store, source_id, limitation_code=limitation_code, error=error, now=now)
    out = []
    with k.store.atomic():
        observed = list(k.store.get(packet["id"])["payload"]["observed"])
        for item in clean:
            item["ref"] = f"o{len(observed) + 1}"
            key, _ = notice_key(item["upstream_id"], [item["url"]] if item["url"] else [], item["title"], item["ts"] or now)
            known = next((e for e in k.store.query("notice", natural_key=key, since=now - 7 * D)
                          if e["state"] in notifier.LIVE), None)
            observed.append(item)
            out.append({"ref": item["ref"], "notified": known["state"] if known else None})
        patch(k.store, packet["id"], reason="observe", payload={"observed": observed}, now=now)
    return {"source": {"id": source_id, "state": state["state"], "gap": state["payload"].get("gap")}, "items": out}


# -- submit ------------------------------------------------------------------------------------------
def feed_result(k, item, r, now):
    article = r.get("article")
    if not article:
        return {"result": "no_article"}
    title, body = str(article.get("title") or "").strip()[:200], str(article.get("body") or "").strip()
    refs = sorted({str(s)[:1000] for s in article.get("source_refs") or [] if s})
    if not title or not body or not refs:
        raise ValueError("article needs title, body and source_refs")
    key = hashlib.sha256(json.dumps(refs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]
    e = k.store.create("feed", key, {"title": title, "interest_id": item["ref"], "sources": refs, "created": now}, now=now)
    # Writing feed/*.md stays with the content writer; the article is returned for it.
    return {"result": "existing" if e["existing"] else "recorded", "feed": e["id"],
            **({} if e["existing"] else {"article": {"title": title, "body": body, "source_refs": refs}})}


HANDLERS = {
    "intake": lambda k, i, r, now: {"state": intake.dispose(k, i["ref"], r.get("disposition"), r.get("facts"), r.get("goal_hint"), now)["state"]},
    "observed": lambda k, i, r, now: notifier.decide(k, i, r.get("verdict"), text=r.get("text"), priority=r.get("priority"), now=now),
    "notice": lambda k, i, r, now: notifier.decide(k, i, r.get("verdict"), text=r.get("text"), priority=r.get("priority"), now=now),
    "goal": lambda k, i, r, now: goals.assess(k, i["ref"], r.get("assessment"), r.get("progress_note"), now),
    "signal": lambda k, i, r, now: goals.create(k, title=(r.get("create") or {}).get("title"),
                                                criteria=(r.get("create") or {}).get("criteria"), signal_ref=i["ref"], now=now),
    "review": lambda k, i, r, now: review.record(k, i["ref"], r.get("verdict"), r.get("reason"), now),
    "interest": feed_result,
}
REF_FIELDS = ("ref", "candidate_ref", "goal_id", "interest_ref")


def submit(k, packet_id, results, now):
    """Apply results item by item (idempotent); unhandled items stay in their queues with the attempt counted."""
    packet = packet_of(k, packet_id)
    if not isinstance(results, list) or len(results) > 100:
        raise ValueError("results must be a list of at most 100")
    p = packet["payload"]
    items = {i["ref"]: i for i in [*p["items"], *k.store.get(packet["id"])["payload"]["observed"]]}
    out, handled, sends = [], [], 0
    for r in results:
        r = r if isinstance(r, dict) else {}
        ref = next((r[f] for f in REF_FIELDS if r.get(f)), None) or (r.get("create") or {}).get("signal_ref")
        try:
            item = items.get(ref)
            if item is None:
                raise KeyError(f"{ref!r} is not in this packet")
            if item["kind"] == "observed" and r.get("verdict") in ("send", "supersede"):
                sends += 1
                if sends > p["budget"]["items"]:
                    raise ValueError(f"at most {p['budget']['items']} notices per patrol packet")
            out.append({"ref": ref, **HANDLERS[item["kind"]](k, item, r, now)})
            handled.append(ref)
        except (ValueError, KeyError, PermissionError) as exc:
            out.append({"ref": ref, "error": str(exc.args[0] if exc.args else exc)})

    def done(e):
        total = sorted(set(e["payload"]["handled"]) | set(handled))
        first = "submitted_at" not in e["payload"]
        return {"handled": total, **({"submitted_at": now, "per_item": (now - e["payload"]["issued_at"]) / max(1, len(total))} if first else {})}
    if not patch(k.store, packet["id"], "submitted", expect=("issued",), reason="submit", payload=done, now=now):
        patch(k.store, packet["id"], reason="submit", payload=done, now=now)  # a late or repeated submit on an expired/submitted packet
    return out
