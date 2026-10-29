"""Main-session hooks (design 2.1, 2.7): what a private user turn sees and what it starts.

pre_llm_call injects at most three things: the Health line while a high finding is open, a one-line Muse
summary only when it changed for this session, and failed/staged memory-write receipts once (a staged one
again a day later). post_llm_call runs one kernel tick in the background (captures the turn as a signal,
dispatches due notices) and arms the quiet wake: 5 minutes of silence may wake the intake job early.
post_tool_call records failed/staged results of native memory writes. Saved and unconfirmed writes stay
silent. Only `user` callers count; cron runs, delegated children and group chats are ignored.
"""
import json
import logging
import threading
import time

from . import health
from .host.sessions import failed_result
from .install import migrating, owned
from .kernel import intake
from .kernel.common import D, clip, patch

log = logging.getLogger(__name__)
MEMORY_TOOLS = frozenset({"memory", "skill_manage", "hindsight_retain"})
READ_ACTIONS = frozenset({"list", "view", "read", "search", "get"})
SHOWN_TITLES = 3


def receipt_state(result):
    """'staged' | 'failed' | None for a native memory write result; anything else stays silent."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(value, dict) and (value.get("staged") or value.get("proposal_staged")):
        return "staged"
    return "failed" if failed_result(text) else None


def kind_of(receipt):
    return receipt.get("state") or receipt.get("classified_state")  # the latter on receipts migrated from 0.3.x


def titles(goals):
    return "; ".join(clip(g["payload"].get("title") or g["natural_key"], 60) for g in goals[:SHOWN_TITLES]) + \
        (f"; +{len(goals) - SHOWN_TITLES} more" if len(goals) > SHOWN_TITLES else "")


class Hooks:
    def __init__(self, kernel, *, quiet_seconds=intake.QUIET_WAKE_AFTER, clock=time.time):
        self.k, self.quiet, self.clock = kernel, quiet_seconds, clock
        self.lock, self.ticking = threading.Lock(), threading.Lock()
        self.timers, self.shown = {}, {}  # shortcut: per-process memory; a restart re-injects the summary once
        self.closed = False

    # -- pre_llm_call ---------------------------------------------------------------------------------
    def pre_turn(self, session_id="", **kwargs):
        if self.closed or not session_id or self.k.adapter.caller(session_id=session_id, **kwargs) != "user":
            return None
        self._cancel(session_id)
        now = self.clock()
        lines = [health.injection(self.k.home, now), self._changed(session_id, self.summary(now)),
                 self.receipts(session_id, now)]
        lines = [line for line in lines if line]
        return {"context": "\n".join(lines)} if lines else None

    def summary(self, now):
        """One line of what needs attention; empty when nothing does."""
        store, parts = self.k.store, []
        marker = migrating(self.k.home)
        if marker and (marker.get("error") or now - marker.get("at", now) > 600):
            parts.append("installation did not finish (" + clip(marker.get("error") or "still marked as migrating", 160)
                         + "); background jobs stay silent until the plugin loads cleanly")
        due = [g for g in store.due(now, kind="goal") if g["state"] == "active"]
        waiting = store.query("goal", state="waiting")
        findings = store.query("finding", state="open")
        if due:
            parts.append(f"{len(due)} goal(s) due for a check: {titles(due)}")
        if waiting:
            parts.append(f"{len(waiting)} goal(s) waiting on the user: {titles(waiting)}")
        if findings:
            parts.append(f"{len(findings)} open conversation-review finding(s)")
        return "[Muse] " + " · ".join(parts) + ". Use muse_manage for details." if parts else ""

    def _changed(self, session_id, line):
        with self.lock:
            if self.shown.get(session_id) == line:
                return None
            self.shown[session_id] = line
        return line or None

    def receipts(self, session_id, now):
        """Failed/staged receipts of this session, each injected once; a staged one once more a day later."""
        store, hits = self.k.store, []
        for e in store.query("receipt", state="pending_notice"):
            if e["payload"].get("session_id") == session_id and patch(
                    store, e["id"], "noticed", expect=("pending_notice",), reason="injected",
                    payload={"noticed_at": now}, now=now):
                hits.append(e["payload"])
        for e in store.query("receipt", state="noticed", since=now - 3 * D):
            p = e["payload"]
            if (p.get("session_id") == session_id and kind_of(p) == "staged" and not p.get("reminded")
                    and (p.get("noticed_at") or now) <= now - D
                    and patch(store, e["id"], expect=("noticed",), reason="staged_reminder",
                              payload={"reminded": True}, now=now)):
                hits.append(p)
        if not hits:
            return None
        failed = sorted({str(p.get("tool")) for p in hits if kind_of(p) == "failed"})
        staged = sorted({str(p.get("tool")) for p in hits if kind_of(p) == "staged"})
        return ("[Muse memory receipts] " + "; ".join(filter(None, (
            failed and "write failed, nothing was saved: " + ", ".join(failed),
            staged and "write staged, it waits for the user's approval: " + ", ".join(staged))))
            + ". Tell the user once if it matters; never repeat the write.")

    # -- post_tool_call -------------------------------------------------------------------------------
    def post_tool(self, tool_name="", args=None, result=None, session_id="", tool_call_id="", **kwargs):
        if (self.closed or tool_name not in MEMORY_TOOLS or not session_id or not tool_call_id
                or (args or {}).get("action") in READ_ACTIONS or self.k.adapter.caller(session_id=session_id, **kwargs) != "user"):
            return None
        state = receipt_state(result)
        if state:
            now = self.clock()
            self.k.store.create("receipt", str(tool_call_id), {"session_id": session_id, "tool": tool_name,
                                                                "state": state, "at": now}, now=now)
        return None

    # -- post_llm_call --------------------------------------------------------------------------------
    def post_turn(self, session_id="", **kwargs):
        if self.closed or not session_id or self.k.adapter.caller(session_id=session_id, **kwargs) != "user":
            return
        self.tick_soon()
        timer = threading.Timer(self.quiet, self.quiet_wake, args=(session_id, self.clock()))
        timer.daemon = True
        with self.lock:
            old = self.timers.pop(session_id, None)
            if old:
                old.cancel()
            self.timers[session_id] = timer
        timer.start()

    def tick_soon(self):
        """One background tick at a time; a tick already running covers this turn's signal next time."""
        if migrating(self.k.home) or not self.ticking.acquire(blocking=False):
            return

        def run():
            try:
                self.k.tick()
            except Exception as exc:  # a background tick must never surface in the user's turn
                log.warning("Muse background tick failed: %s", exc)
            finally:
                self.ticking.release()
        threading.Thread(target=run, name="muse-tick", daemon=True).start()

    def quiet_wake(self, session_id, last_turn_at):
        with self.lock:
            self.timers.pop(session_id, None)
        if self.closed or migrating(self.k.home):
            return
        try:
            now = self.clock()
            self.k.tick(now)
            if intake.quiet_wake(self.k, last_turn_at, now):
                job = owned(self.k.adapter, "memory-upkeep")
                if job:
                    self.k.adapter.cron.wake(job["id"])
        except Exception as exc:
            log.warning("Muse quiet wake failed: %s", exc)

    def _cancel(self, session_id):
        with self.lock:
            timer = self.timers.pop(session_id, None)
        if timer:
            timer.cancel()

    def close(self):
        self.closed = True
        with self.lock:
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()
