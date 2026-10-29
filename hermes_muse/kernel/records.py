"""What the private main session records or removes for the user (design 2.6 writes): interests and ideas,
feedback, forget, Feed items and their files. surface maps tool arguments to these calls; entity state is
written only in kernel/ (goals, watches and notices live in their own modules)."""
from ..content_io import DomainError, Files, revision
from ..locks import file_lock
from . import notifier
from .common import D, patch
from .store import title_key

FEED_DIR = "workspace/your_files/feed"
REMEMBER_FOR = 14 * D


def lock(k):
    return file_lock(k.home / "muse" / "mutations.lock")


def remember(k, kind, title, *, rationale="", expires=None, extra=None, now):
    """interest/idea by title: recorded, or renewed (active again, new expiry) when the title is known."""
    store, deadline = k.store, expires or now + REMEMBER_FOR
    payload = {"title": title.strip(), "rationale": rationale or "", **(extra or {})}
    with store.atomic():
        e = store.create(kind, title_key(title), {**payload, "created": now}, deadline=deadline, now=now)
        if e["existing"]:
            e = patch(store, e["id"], "active", reason="renew", payload=payload, deadline=deadline, now=now)
    return {"result": "renewed" if e["version"] > 1 else "recorded", "id": e["id"], "expires": e["deadline"]}


def feedback(k, e, value, note="", *, now):
    """The user's reaction; a finding resolves or is dismissed, stop ends an interest/idea and withdraws an unsent notice."""
    mark = {"feedback": {"value": value, "note": note or "", "at": now}}
    to, expect = None, None
    if e["kind"] == "finding":
        to, expect = ("resolved" if value == "useful" else "dismissed"), ("open",)
    elif e["kind"] in ("interest", "idea") and value == "stop":
        to, expect = "stopped", ("active", "expired")
    if e["kind"] == "notice" and value == "stop":
        notifier.withdraw(k, e["id"], reason="user_feedback", now=now)
    moved = patch(k.store, e["id"], to, expect=expect, reason=f"feedback:{value}", payload=mark, now=now)
    return {"result": "recorded", "id": e["id"], "state": (moved or k.store.get(e["id"]))["state"]}


def forget(k, e, *, now):
    """Scrub a signal, interest or idea and leave a tombstone so old evidence cannot recreate it; a Feed item is deleted."""
    if e["kind"] == "feed":
        delete_feed(k, e, now=now)
        return {"result": "deleted", "id": e["id"]}
    kind, store = e["kind"], k.store
    with store.atomic():
        store.create("tombstone", e["natural_key"], {"kind": kind, "at": now}, now=now)
        scrub = {"excerpt": "", "facts": [], "pointer": {}} if kind == "signal" else {"title": "", "rationale": "", "sources": []}
        to = {"signal": "disposed", "interest": "stopped", "idea": "stopped"}[kind]
        patch(store, e["id"], to, expect=("new", "parked") if kind == "signal" else ("active", "expired"),
              reason="forget", payload={**scrub, "forgotten": True}, now=now) or \
            patch(store, e["id"], reason="forget", payload={**scrub, "forgotten": True}, now=now)
    return {"result": "forgotten", "id": e["id"]}


def feed_path(e):
    return f"{FEED_DIR}/{e['natural_key']}.md"


def mark_feed(k, e, change, text=None, *, now):
    mark = {"feedback": {"text": text, "at": now}} if change == "feedback" else {"pinned": change == "pin"}
    patch(k.store, e["id"], expect=("published",), reason=f"feed:{change}", payload=mark, now=now)
    return {"result": "updated", "id": e["id"], **mark}


def delete_feed(k, e, *, now):
    files, rel = Files(k.home), feed_path(e)
    with lock(k):
        try:
            files.unlink(rel, revision(files.read(rel)))
        except FileNotFoundError:
            pass
        patch(k.store, e["id"], "deleted", expect=("published",), reason="user_delete", now=now)


def write_feed(k, e, article):
    """feed/<key>.md for a newly recorded FeedItem; the Store entity is the record, the file its body.
    shortcut: a crash between the record and this write loses the body; resubmit returns `existing`."""
    rel = feed_path(e)
    raw = (f"# {article['title']}\n\n{article['body']}\n\n## Sources\n"
           + "".join(f"- {s}\n" for s in article["source_refs"])).encode("utf-8")
    with lock(k):
        try:
            Files(k.home).replace(rel, raw, "absent")
        except DomainError as exc:
            if exc.code != "revision_conflict":
                raise
    return rel
