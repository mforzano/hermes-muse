"""Native memory upkeep by the intake job: Hermes owns the memory files, Muse only measures, guards and records.

When a store is at least 90% full and intake has new facts, the packet names it (`memory_full`, at most 3
packets a day); the model may then merge duplicate entries or shorten verbose ones in one native batch, never
delete an entry outright (the pre_tool_call guard refuses a remove without a patch beside it). Every change is
diffed from before/after snapshots, appended to the day's Muse note and handed to the next nightly packet
(`memory_changes`) for the dream, so the user can see and recover it.
"""
from datetime import datetime

from ..content_io import Files, revision
from ..host import HOST_ERROR
from ..host.sessions import redact
from ..locks import file_lock
from .common import D, clip, count_today, local_day

FULL, ASKS = 0.9, 3
KEEP = 7 * D
REFUSED = ("Muse intake may merge duplicate entries (patch one, remove the duplicates in the same batch) or shorten "
           "entries; it never deletes an entry outright. Nothing was written; leave the item unanswered if the fact does not fit.")


def usage(s):
    return f"{s['chars'] * 100 // max(1, s['limit'])}% {s['chars']}/{s['limit']}"


def full(k, now):
    """Stores at FULL or more for an intake packet with items; empty on a host error (a fault is recorded)."""
    stores = k.adapter.memory.stores()
    if stores.get("status") == HOST_ERROR:
        return []
    hit = [{"target": t, "usage": usage(s)} for t, s in stores.items() if s["chars"] >= FULL * s["limit"]]
    return hit if hit and count_today(k.store, "memory_full_asks", now, k.tz(), ASKS) else []


def guard(args):
    """REFUSED for a native memory write that removes an entry without a patch in the same call, else None.
    shortcut: a remove beside any patch passes; the recorded old text keeps it recoverable."""
    ops = args.get("operations") if isinstance(args.get("operations"), list) else [args]
    actions = {str((op or {}).get("action")) for op in ops if isinstance(op, dict)}
    return REFUSED if "remove" in actions and "patch" not in actions else None


def diff(before, after):
    """Per store: entries the write removed or rewrote (old) and entries it wrote (new); plain additions are not upkeep."""
    out = []
    for target, b in before.items():
        a = after[target]["entries"] if target in after else []
        old = [e for e in b["entries"] if e not in a]
        if old:
            out.append({"target": target, "old": old, "new": [e for e in a if e not in b["entries"]]})
    return out


def record(k, before, after, now):
    """Record one intake write's changes in Store meta and the day's note `memory/<day>.md`."""
    if "status" in before or "status" in after:
        return []
    changes = diff(before, after)
    if not changes:
        return []
    with k.store.atomic():
        log = [c for c in k.store.meta("memory_changes", []) if c["at"] > now - KEEP]
        k.store.set_meta("memory_changes", [*log, *({"at": now, **c} for c in changes)])
    rel = f"memory/{local_day(now, k.tz())}.md"  # the note id daily:<day> (surface.content_path)
    lines = [f"\n## Native memory upkeep {datetime.fromtimestamp(now, k.tz()):%H:%M}\n"]
    for c in changes:
        lines += [f"- {c['target']}: before\n", *(f"  - {e}\n" for e in c["old"]),
                  "  after\n", *(f"  - {e}\n" for e in c["new"] or ["(nothing)"])]
    files = Files(k.home)
    with file_lock(k.home / "muse" / "mutations.lock"):
        try:
            raw = files.read(rel)
            expected = revision(raw)
        except FileNotFoundError:
            raw, expected = f"# {local_day(now, k.tz())}\n".encode(), "absent"
        files.replace(rel, raw + "".join(lines).encode(), expected)
    return changes


def unreported(k, now):
    """Changes the nightly dream has not listed yet, redacted and clipped for the model."""
    since = k.store.meta("memory_reported_until", 0)
    return [{"at": c["at"], "target": c["target"], "old": [clip(redact(e), 500) for e in c["old"]],
             "new": [clip(redact(e), 500) for e in c["new"]]}
            for c in k.store.meta("memory_changes", []) if c["at"] > max(since, now - KEEP)]


def reported(k, changes):
    """The nightly batch that wrote the dream listed these changes."""
    if changes:
        k.store.set_meta("memory_reported_until", max(c["at"] for c in changes))
