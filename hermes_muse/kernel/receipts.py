"""MemoryReceipt: failed or staged results of native memory writes in a private user turn (design 2.2).

Only failed/staged are recorded (saved and unconfirmed stay silent). The next user turn of that session sees
each once; a staged one is shown once more a day later, then left alone.
"""
from .common import D, patch

KIND = "memory_receipt"


def state_of(receipt):
    return receipt.get("state") or receipt.get("classified_state")  # the latter on receipts migrated from 0.3.x


def record(k, tool_call_id, *, session_id, tool, state, now):
    return k.store.create(KIND, str(tool_call_id), {"session_id": session_id, "tool": tool, "state": state, "at": now}, now=now)


def due(k, session_id, now):
    """Payloads to show in this session's next turn; each is marked so it is shown once (staged: once more)."""
    store, hits = k.store, []
    for e in store.query(KIND, state="pending_notice"):
        if e["payload"].get("session_id") == session_id and patch(
                store, e["id"], "noticed", expect=("pending_notice",), reason="injected",
                payload={"noticed_at": now}, now=now):
            hits.append(e["payload"])
    for e in store.query(KIND, state="noticed", since=now - 3 * D):
        p = e["payload"]
        if (p.get("session_id") == session_id and state_of(p) == "staged" and not p.get("reminded")
                and (p.get("noticed_at") or now) <= now - D
                and patch(store, e["id"], expect=("noticed",), reason="staged_reminder",
                          payload={"reminded": True}, now=now)):
            hits.append(p)
    return hits
