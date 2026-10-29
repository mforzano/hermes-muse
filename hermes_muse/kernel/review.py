"""Conversation review: code picks at most 10 turns of the last 24h of private user sessions by fixed rules;
the model only judges each (agent_gap / latent_need / fine). One entity per turn, ever.

Rules: a completion claim with no successful write-type tool result in the same turn; a failed tool not
retried successfully; a user correction or near-repeat of the previous request; a stated need that matches
no goal, interest or idea title.
"""
import re
from difflib import SequenceMatcher

from .common import D, clip, one

LIMIT = 10
VERDICTS = ("agent_gap", "latent_need", "fine")
CLAIM = re.compile(r"已完成|已发送|已修复|已部署|已提交|搞定|\b(?:done|fixed|completed|sent|deployed|resolved)\b", re.I)
CORRECTION = re.compile(r"不是|还是没|又|我说的是|不对|\b(?:not what I|still (?:not|doesn'?t|broken)|again|I said|that'?s wrong)\b", re.I)
NEED = re.compile(r"帮我|记得|提醒我|以后|我想|打算|计划|\b(?:remind me|I want to|I'd like to|plan to|don'?t forget|todo)\b", re.I)
WRITE_TOOL = re.compile(r"write|edit|patch|send|create|update|delete|commit|deploy|memory|terminal|manage|cron|todo", re.I)
FAILED = re.compile(r'"(?:success|ok)"\s*:\s*false|"error"\s*:\s*"[^"]|"status"\s*:\s*"(?:error|failed)"'
                    r'|^\s*(?:error|failed|traceback)\b', re.I | re.M)


def turns(rows):
    """Group time-ordered user/assistant/tool rows into turns per session."""
    out, current = [], {}
    for row in rows:
        sid = row["session_id"]
        if row["role"] == "user":
            previous = current.get(sid)
            turn = {"ref": row["ref"], "at": row["at"], "user": row["text"], "replies": [], "tools": [],
                    "previous": previous["user"] if previous else None,
                    "previous_reply": previous["replies"][-1] if previous and previous["replies"] else None}
            current[sid] = turn
            out.append(turn)
        elif sid in current:
            if row["role"] == "assistant":
                current[sid]["replies"].append(row["text"])
            else:
                current[sid]["tools"].append((row.get("tool_name") or "", not FAILED.search(row["text"][:2000])))
    return out


def rules(turn, titles):
    found, tools, user = [], turn["tools"], turn["user"]
    if CLAIM.search("\n".join(turn["replies"])) and not any(ok and WRITE_TOOL.search(name) for name, ok in tools):
        found.append("claim_without_write")
    if any(not ok and not any(ok2 and name2 == name for name2, ok2 in tools[i + 1:]) for i, (name, ok) in enumerate(tools)):
        found.append("tool_failure_unretried")
    previous = turn["previous"]
    if CORRECTION.search(user) or previous and len(user) >= 8 and SequenceMatcher(None, previous[:500], user[:500]).ratio() >= 0.8:
        found.append("user_correction")
    if NEED.search(user) and not any(len(t) >= 3 and t in user.lower() for t in titles):
        found.append("unrecorded_need")
    return found


def candidates(k, now, limit=LIMIT):
    """Newest first; turns that already have a finding or idea are never picked again."""
    rows = k.adapter.sessions.user_turns(now - D, roles=("user", "assistant", "tool"), limit=2000)
    titles = [str(e["payload"].get("title") or "").lower() for kind in ("goal", "interest", "idea") for e in k.store.query(kind)]
    picked = []
    for turn in reversed(turns(rows)):
        ref = "review:" + turn["ref"]
        if one(k.store, "finding", ref) or one(k.store, "idea", ref):
            continue
        found = rules(turn, titles)
        if not found:
            continue
        picked.append({"kind": "review", "ref": ref, "at": turn["at"], "rules": found, "user": clip(turn["user"], 1500),
                       "reply": clip(turn["replies"][-1] if turn["replies"] else "", 1500),
                       **({"previous_reply": clip(turn["previous_reply"], 800)} if "user_correction" in found and turn["previous_reply"] else {}),
                       "tools": [{"name": n, "ok": ok} for n, ok in turn["tools"][:10]], "pointer": turn["ref"]})
        if len(picked) == limit:
            break
    return picked


def record(k, ref, verdict, reason, now):
    """agent_gap -> open Finding (14 days); latent_need -> Idea candidate; fine -> dismissed Finding."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    reason = str(reason or "").strip()[:500]
    if not reason:
        raise ValueError("reason is required")
    payload = {"reason": reason, "verdict": verdict, "turn": ref.removeprefix("review:")}
    with k.store.atomic():
        found = one(k.store, "finding", ref) or one(k.store, "idea", ref)
        if found:
            return {"result": "existing", "entity": found["id"]}
        if verdict == "latent_need":
            e = k.store.create("idea", ref, {"title": reason[:200], "origin": "review", **payload}, now=now)
        else:
            e = k.store.create("finding", ref, payload, state="open" if verdict == "agent_gap" else "dismissed", now=now)
    return {"result": "recorded", "entity": e["id"], "state": e["state"]}
