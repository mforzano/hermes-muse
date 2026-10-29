"""Read-only dashboard views of one profile: Feed, ideas and goals from Store v2 and the content files.

Opens state-v2.db read-only and never repairs or writes. Lists page by a plain offset cursor; an article
or goal body pages by offset and is pinned to the revision of its first page.
"""
from pathlib import Path

from .content_io import Files, revision
from .kernel import goals
from .kernel.store import Store

FEED_DIR = "workspace/your_files/feed"
BODY_MAX = 24000
SUMMARY = ("title", "status", "created", "updated", "goal_id", "interest_id", "why", "rationale", "deadline",
           "next_check_at", "waiting_user", "blocked", "no_fade")


class NotFound(LookupError):
    pass


def summary(entity_id, record):
    out = {"id": entity_id, **{k: record[k] for k in SUMMARY if record.get(k) is not None}}
    if isinstance(record.get("sources"), list):
        out["source_count"] = len(record["sources"])
    if record.get("progress"):
        out["latest_progress"] = dict(record["progress"][-1])
    return {k: v[:512] if isinstance(v, str) else v for k, v in out.items()}


def page(items, cursor, limit):
    offset = int(cursor) if str(cursor or "").isdigit() else 0
    end = offset + max(1, min(int(limit), 100))
    return {"items": items[offset:end], "next_cursor": str(end) if end < len(items) else None,
            "complete": end >= len(items), "total": len(items)}


class Views:
    def __init__(self, home):
        self.home = Path(home)
        self.files = Files(self.home)

    def entities(self, kind, state):
        path = self.home / "muse" / "state-v2.db"
        return Store(path, readonly=True).query(kind, state=state) if path.is_file() else []

    def feed_path(self, e):
        return e["payload"].get("path") or f"{FEED_DIR}/{e['natural_key']}.md"

    def feed_body(self, e):
        try:
            return self.files.read(self.feed_path(e))
        except FileNotFoundError:
            return b""

    def goal_records(self):
        found = (goals.read(self.home, slug) for slug in self.files.names(goals.ROOT))
        return [f for f in found if f]

    # -- lists ---------------------------------------------------------------------------------------
    def feed(self, query=None, topic=None, cursor=None, limit=25):
        items = []
        for e in self.entities("feed", "published"):
            p = e["payload"]
            if topic and topic not in (p.get("goal_id"), p.get("interest_id")):
                continue
            if query and query.casefold() not in (str(p.get("title")) + " " + self.feed_body(e).decode("utf-8", "replace")).casefold():
                continue
            items.append(summary(e["id"], p))
        items.sort(key=lambda r: (-(r.get("created") or 0), r["id"]))
        return page(items, cursor, limit)

    def ideas(self, cursor=None, limit=25):
        items = sorted((summary(e["id"], e["payload"]) for e in self.entities("idea", "active")),
                       key=lambda r: (-(r.get("created") or 0), r["id"]))
        return page(items, cursor, limit)

    def goals(self, status=None, cursor=None, limit=25):
        rows = []
        for record, _, _ in self.goal_records():
            if status and (record.get("status") == "active") != (status == "open"):
                continue
            rows.append(summary(record["id"], record))

        def order(r):  # needs you, blocked, in progress; closed last, newest first
            if r.get("status") != "active":
                return (3, -(r.get("updated") or 0), r["id"])
            return (0 if r.get("waiting_user") else 1 if r.get("blocked") else 2, r.get("next_check_at") or 0, r["id"])
        return page(sorted(rows, key=order), cursor, limit)

    # -- one object ----------------------------------------------------------------------------------
    def article(self, kind, object_id, offset=0, length=12000, pinned=None):
        if kind == "feed":
            e = next((e for e in self.entities("feed", "published") if e["id"] == object_id), None)
            if e is None:
                raise NotFound(f"no Feed article {object_id!r}")
            raw, metadata = self.feed_body(e), e["payload"]
        elif kind == "goal":
            found = goals.read(self.home, object_id) if "/" not in str(object_id) else None
            if not found:
                raise NotFound(f"no goal {object_id!r}")
            metadata, raw = found[0], found[1].encode()
        else:
            raise NotFound(f"no {kind} view")
        current = revision(raw)
        if offset and pinned != current:
            raise ValueError("content changed since the first page; reopen it")
        body = raw.decode("utf-8", "replace")
        end = offset + max(1, min(int(length), BODY_MAX))
        return {"kind": kind, "id": object_id, "revision": current, "metadata": metadata, "body": body[offset:end],
                "total_chars": len(body), "next_offset": end if end < len(body) else None, "complete": end >= len(body)}
