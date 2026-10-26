"""Plugin registration, conversation hooks and the single state tool."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from .handoff import MARKER, receiving_instructions, render_result
from .audit import Audit
from .host import HermesHost, ROOT, profile_scope
from .service import Companion
from .store import Store, is_background_message
from .followup import context as followup_context

log = logging.getLogger(__name__)
ACTIONS = ("content_read", "content_write", "memory_feedback_review", "installation_prepare", "context", "status", "conversation_read", "goal_create", "goal_update", "interest_record", "idea_add", "feedback",
           "preferences", "notification_add", "notification_refresh", "notification_queue", "notification_handoff", "notification_prepare", "feed_add", "feed_list",
           "feed_update", "calendar_window", "calendar_view", "source_check", "notification_resolve", "notification_review", "review_complete", "run_complete", "watch_create", "watch_stop", "delegate", "research_start", "research_status", "operations_read", "operations_complete", "operations_batch", "operations_evidence", "operations_commit", "finding_record", "forget")
SCHEMA = {"name": "muse_manage", "description": "Assemble calendar views, manage companion goals, temporary interests, reminders, Feed and background research and inspect native operations/findings. First read hermes-muse:companion for action-specific data fields. Every argument goes inside data, never beside action: {'action':'context','data':{'section':'notice','limit':25}}. Parent context/status without data return a bounded section directory listing valid sections. section=signals with job freezes a review window; read its bodies before checkpointing. child context/status are pure paginated projections. Children may read current-profile content/history/audit evidence but cannot change state or checkpoints. Read result content_ref or error_ref through content_read pages. Writes to non-goal objects need request_id (new key per logical write) and expected_revision (revision string from the object's context list row or content_read; 'absent' for a new object); errors return current_revision. Optional request_id on external/checkpoint calls preserves replay; outcome_unknown never permits redispatch. Goals use active/done/cancelled; after telling the user, goal_update(status=done,note=...). Optional expected_revision is one integer; no request_id or scope token is needed for goals. Read due goals and owner status with context(section=followups). Never invent a signal ID, delivery receipt or authorization.",
          "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": list(ACTIONS)}, "data": {"type": "object", "description": "Action arguments documented by the companion Skill; omit for context/status section discovery; use returned section/cursor/window arguments."}}, "required": ["action"]}}


def session_info():
    from gateway.session_context import get_session_env
    fields = {key: get_session_env("HERMES_SESSION_" + key.upper(), "") for key in
              ("id", "key", "platform", "chat_id", "chat_type", "thread_id", "user_id", "message_id", "scope_id", "parent_chat_id")}
    fields["cron"] = get_session_env("HERMES_CRON_SESSION", "") == "1"
    return fields


def private_session(info):
    return str(info.get("chat_type", "")).lower() not in {"group", "supergroup", "channel", "guild", "room"}


def route_from(info):
    if not private_session(info) or not info.get("platform") or not info.get("chat_id"):
        return None
    return {key: info[key] for key in ("platform", "chat_id", "chat_type", "thread_id", "user_id", "message_id", "scope_id", "parent_chat_id") if info.get(key)} | {"session_key": info.get("key", "")}


class Runtime:
    def __init__(self, ctx, home, *, quiet_seconds=300, plugin_root=ROOT):
        self.ctx, self.home, self.quiet_seconds = ctx, Path(home).resolve(), quiet_seconds
        self.store = Store(self.home)
        self.host = HermesHost(self.store, plugin_root=plugin_root)
        self.service = Companion(self.store, self.host)
        from .memory_feedback import MemoryFeedback
        self.memory_feedback = MemoryFeedback(self.store, self.service.clock)
        self.timers = {}
        self.delivery_turns = {}
        self.run_turns = {}
        self.lock = threading.RLock()
        self.closed = False

    def handle(self, args, **kwargs):
        from .responses import ARCHIVE, archive, preflight, invoke_once
        token = ARCHIVE.set(None)
        try:
            from .contracts import tidy
            if isinstance(args, dict) and isinstance(args.get("data"), dict):
                args = {**args, "data": tidy(args.get("action"), args["data"])}
            preflight(args)
            if not private_session(session_info()):
                from .content_io import DomainError
                raise DomainError('permission_denied', 'Personal Muse data is available in private/direct conversations only')
            from agent.delegation_context import is_delegated_child_context
            # Pure reads never create result records, including rejected child calls.
            reads = {'content_read', 'feed_list', 'calendar_window', 'calendar_view', 'operations_evidence', 'conversation_read'}
            if private_session(session_info()) and not is_delegated_child_context() and args['action'] not in reads:
                ARCHIVE.set(lambda envelope: archive(self.store, kwargs.get('session_id', ''), args['action'], envelope))
            from .mutations import invoke
            raw = invoke_once(self, args, kwargs, lambda: invoke(self, args, kwargs, self._handle))
            return self._receipt(json.loads(raw), action=args['action'], data=args.get('data', {}))
        except Exception as exc:
            return self._error(exc, *self._call_of(args))
        finally:
            ARCHIVE.reset(token)

    @staticmethod
    def _call_of(args):
        return (args.get("action"), args.get("data")) if isinstance(args, dict) else (None, None)

    @staticmethod
    def _error(exc, action=None, data=None):
        from .guidance import normalize
        exc = normalize(exc, action, data)
        error = {"ok": False, "error": str(exc), "code": getattr(exc, "code", "invalid_argument"),
                 "retryable": getattr(exc, "code", "") in {"revision_conflict", "revision_required", "cursor_expired", "source_unavailable"},
                 **getattr(exc, "details", {})}
        if hasattr(exc, "native_result"):
            error['native'] = exc.native_result
        return Runtime._receipt(error)

    def _handle(self, args, **kwargs):
        try:
            if self.closed:
                raise ValueError("Plugin unloaded")
            # This core forwards session/task only to registry handlers. Its
            # execution wrapper binds trusted correlation ContextVars instead.
            # Version-specific read-only adapter; absence leaves IDs unknown.
            try:
                from tools import approval_context
                if approval_context._approval_session_id.get() == kwargs.get("session_id"):
                    kwargs = {**kwargs, "turn_id": approval_context._approval_turn_id.get(),
                              "tool_call_id": approval_context._approval_tool_call_id.get()}
            except (ImportError, AttributeError):
                pass
            info = session_info()
            if not private_session(info):
                raise ValueError("Personal Muse data is available in private/direct conversations only")
            from .content_io import DomainError
            from .views import Views
            from .contracts import validate
            from .guidance import bad_envelope
            if not isinstance(args, dict) or set(args) - {"action", "data"}:
                raise bad_envelope(args)
            if len(json.dumps(args, ensure_ascii=False, allow_nan=False).encode()) > 512 * 1024:
                raise DomainError("size_limit", "Request exceeds 512 KiB; split unstarted work into several calls")
            data = args.get("data", {})
            action = args.get("action")
            validate(action, data)
            from .contracts import CAS, CREATES, REPLAY_ACTIONS
            if (action in CAS or action in CREATES) and not (action == "forget" and data.get("kind") == "content"):
                data = {k:v for k,v in data.items() if k not in {"expected_revision", "request_id"}}
            if action in REPLAY_ACTIONS:
                data = {k: v for k, v in data.items() if k != "request_id"}
            from agent.delegation_context import is_delegated_child_context
            delegated = is_delegated_child_context()
            goal_write = action in {'goal_create', 'goal_update'} or action == 'forget' and data.get('kind') == 'goal'
            intake = None
            if goal_write and (delegated or info['cron'] or info.get('platform') in {'cron', 'delegate', 'subagent'} or not kwargs.get('session_id')):
                # The sole exception: goal_create from the nightly review Cron. Cron identity comes from the
                # session; task_id only names which Cron job, since the real parent also carries a task_id.
                if action == 'goal_create' and not delegated and kwargs.get('session_id') \
                        and (info.get('platform') == 'cron' or info['cron'] and info.get('platform') not in {'delegate', 'subagent'}):
                    with profile_scope(self.home):
                        run = self.service.runs.bind(kwargs.get('task_id'))
                    intake = run if run and run['job'] == 'nightly-review' else None
                if not intake:
                    raise DomainError('permission_denied', 'Only a private parent conversation can change goals.' + (
                        ' From the nightly review only goal_create (with signal_id) is allowed: name this goal in the run_complete summary instead.'
                        if info['cron'] or info.get('platform') == 'cron' else ''))
            # Children inherit the native profile/user scope. Reading needs no
            # plugin grant service; global writes and lifecycle refresh stay parent-owned.
            child_reads = {"context", "status", "content_read", "feed_list", "operations_evidence",
                           "conversation_read", "calendar_window", "calendar_view"}
            if delegated and action not in child_reads:
                raise DomainError("permission_denied", "Child tasks may read current-profile evidence; state changes and checkpoints belong to the parent")
            if delegated and action in {"context", "status"}:
                view = Views(self.home)
                from .context_pages import COLLECTIONS
                section = data.get('section', 'overview')
                if section == 'followups':
                    from .followup import snapshot
                    return self._response(snapshot(self.home, now=self.service.clock(), cursor=data.get('cursor', ''), limit=data.get('limit', 25)))
                if section in COLLECTIONS:
                    return self._response(view.read({'kind': section, **{k:v for k,v in data.items() if k not in {'section','job','window'}}}))
                if section != 'overview':
                    raise DomainError('permission_denied', 'Review windows and coverage belong to the parent; use conversation_read(read_only=true) for child history')
                return self._response({'read_semantics': 'pure read; parent owns review windows/checkpoints',
                    'sections': {kind: {'action':'content_read','data':{'kind':kind,'limit':25}} for kind in COLLECTIONS}})
            if action == "operations_evidence":
                return self._response(self.service.operations.evidence(data))
            if action == "conversation_read" and (delegated or data.get("read_only")):
                from .conversation import read_page
                return self._response(read_page(self.home, data, self.service.clock()))
            if action == "conversation_read" and any(k in data for k in ("since", "until")):
                raise DomainError("invalid_argument", "History time filters require read_only=true; filtered reads cannot certify notice coverage")
            if action in {"content_read", "feed_list", "calendar_window", "calendar_view"}:
                if action == "content_read":
                    view = Views(self.home, session_id=kwargs.get("session_id", ""))
                    parts = str(kwargs.get("task_id") or "").split(":", 2)
                    if len(parts) == 3 and parts[0] == "cron":
                        stored = view.record("run", parts[2]) or {}
                        owned = view.manifest()["jobs"].get(parts[1], {})
                        view.allow_hidden = bool(stored.get("status") == "running" and stored.get("job_id") == parts[1]
                                                 and (owned.get("key") == "nightly-review" or owned.get("owner") == data.get("id")))
                    result = view.read(data)
                elif action == "feed_list":
                    result = Views(self.home).read({**data, "kind": "feed"})
                else:
                    result = getattr(self.service, action)(data)
                return self._response(result)
            with profile_scope(self.home):
                self.service.operations.track(kwargs.get("session_id"), kwargs.get("task_id"))
                run = self.service.runs.bind(kwargs.get("task_id"))
                if (action == 'content_write' or action == 'forget' and data.get('kind') == 'content') and not run and (info['cron'] or info.get('platform') in {'cron', 'delegate', 'subagent'}):
                    raise DomainError('permission_denied', 'Background content writes require their own native maintenance run')
                if run and kwargs.get("session_id"):
                    with self.lock:
                        self.run_turns[kwargs["session_id"]] = run
                delivery = self.host.current_delivery(kwargs.get("task_id"))
                if delivery and kwargs.get("session_id"):
                    with self.lock:
                        self.delivery_turns[kwargs["session_id"]] = delivery
                if action == "forget" and data.get("kind") == "content":
                    from .content import Content
                    result = Content(self.service).forget(data, session_id=kwargs.get("session_id", ""))
                elif action == "content_write":
                    from .content import Content
                    result = Content(self.service).write(data, session_id=kwargs.get("session_id", ""), run=run)
                elif action == "memory_feedback_review":
                    result = self.memory_feedback.review(data, kwargs.get("session_id", ""), kwargs.get("turn_id", ""))
                elif action == "installation_prepare":
                    from .installation import prepare
                    result = prepare(self, data, kwargs.get("session_id", ""), kwargs.get("turn_id", ""))
                elif action == 'goal_create' and intake:
                    result = self.service.intake_goal_create(data, intake)
                elif action == 'goal_create':
                    result = self.service.goal_create(data, session_id=kwargs.get('session_id', ''))
                elif action == 'goal_update':
                    result = self.service.goal_update(data)
                elif action in {"context", "status"}:
                    self.host.reconcile()
                    parts = str(kwargs.get("task_id") or "").split(":", 2)
                    view = None
                    if len(parts) == 3 and parts[0] == "cron":
                        view = self.host.manifest()["jobs"].get(parts[1], {}).get("key")
                    from .context_pages import context
                    result = context(self.service, data, view=view)
                    if data.get('section', 'overview') == 'overview':
                        result['memory_feedback'] = self.memory_feedback.candidates(kwargs.get('session_id', ''))
                elif action in {"operations_batch", "operations_commit"}:
                    audit = Audit(self.service.operations)
                    from agent.subagent_lifecycle import get_active_subagent_parent
                    parent = get_active_subagent_parent()
                    remaining = getattr(getattr(parent, "iteration_budget", None), "remaining", None)
                    if action == "operations_batch" and isinstance(remaining, int) and remaining < 8:
                        result = {"available": True, "deferred": True, "assignments": [],
                                  "instruction": "The host has fewer than eight tool iterations left. Save findings and run_complete(partial) with the audit continuation, then finish normally. Do not launch another worker wave; the next hourly run resumes saved work."}
                    else:
                        result = audit.batch() if action == "operations_batch" else audit.commit(data)
                    self.service.runs.stage(run, "operations", {
                        "complete": bool(result.get("batch_complete")) and not result.get("more", True),
                        "phase": "allocated" if action == "operations_batch" else "reviewed",
                        **{k: result[k] for k in ("checkpoint", "allocated_slots", "reviewed_slots", "newly_reviewed_slots", "continuous_slots", "remaining_slots", "observed_at", "deferred") if k in result}})
                elif action in {"operations_read", "operations_complete", "finding_record"}:
                    method = {"operations_read": "read", "operations_complete": "complete", "finding_record": "finding"}[action]
                    result = getattr(self.service.operations, method)(data)
                    if action == "operations_read":
                        self.service.runs.stage(run, "operations", {"complete": False, "phase": "allocated", "allocated_events": len(result.get("events", [])), "allocated_executions": len(result.get("executions", [])), "receipt": result.get("token")})
                    if action == "operations_complete":
                        self.service.runs.stage(run, "operations", {"complete": not result["more"], "checkpoint": result["checkpoint"]})
                elif action == "run_complete":
                    result = self.service.runs.finish(run, data)
                elif action == "review_complete":
                    if run and data.get("job") != run["job"]:
                        raise ValueError("Review checkpoint must belong to this Cron job")
                    if run and run["job"].startswith("watch-"):
                        owned = self.host.manifest()["jobs"].get(run["job_id"], {})
                        goal, _ = self.store.goal(owned["owner"])
                        refs = data.get("source_check_ids")
                        if (data.get("goal_id") != goal["id"] or not isinstance(refs, list) or not refs
                                or data.get("stop_condition") != owned.get("stop_condition")):
                            raise ValueError("Watch review needs its actual goal, stop_condition and source_check_ids")
                        rows = [self.store.read("source_check", ref, {}) for ref in refs]
                        complete = all(r.get("status") == "checked" and r.get("started_at", r.get("started", 0)) >= run["started"] for r in rows)
                        result = {"complete": complete, "through": self.service.clock(), "goal_id": goal["id"],
                                  "goal_status": goal["status"], "stop_condition": data["stop_condition"], "source_check_ids": refs}
                        self._response(result)  # Reject an unaddressable oversized result before staging completion.
                    else:
                        result = self.service.review_complete(data, require_window=True)
                    self.service.runs.stage(run, "review", {"complete": result["complete"], "through": result["through"], "summary": data["summary"]})
                elif action == "preferences":
                    result = self.service.preference_update({k:v for k,v in data.items() if k != "signal_id"}, cron=info["cron"] or str(kwargs.get("task_id", "")).startswith("cron:"))
                elif action == "watch_create":
                    # Native cron identity, never a model-supplied ownership flag.
                    if info["cron"] or str(kwargs.get("task_id") or "").startswith("cron:"):
                        from hermes_cli.config import load_config_readonly
                        if load_config_readonly().get("cron", {}).get("allow_agent_scheduling") is not True:
                            raise ValueError("Cron watch creation is disabled by cron.allow_agent_scheduling; no job created. Existing goal follow-through continues.")
                    result = self.host.watch(data)
                elif action == "watch_stop":
                    result = self.host.stop_watch(data["job_id"])
                elif action == "delegate":
                    from .delegation import dispatch, validate
                    request = {k: v for k, v in data.items() if k != "goal_id"}
                    validate(request, owned_cron=bool(run or delivery))
                    result = Audit(self.service.operations).dispatch(request,
                        lambda: dispatch(self.ctx, request, owned_cron=bool(run or delivery)),
                        session_id=kwargs.get('session_id', ''), tool_call_id=kwargs.get('tool_call_id'))
                elif action in {"research_start", "research_status"}:
                    from .research import start, poll
                    result = (start(self.store, self.ctx, data, owned_cron=bool(run or delivery),
                                    session_id=kwargs.get("session_id", ""), turn_id=kwargs.get("turn_id", ""), tool_call_id=kwargs.get("tool_call_id"))
                              if action == "research_start" else poll(self.store, self.ctx, data))
                elif action in {"notification_prepare", "notification_queue", "notification_handoff"}:
                    result = self.service.notification_queue(data, delivery if delivery and delivery.get("kind") != "report" else None)
                elif action == "source_check":
                    result = self.service.source_check(data, run=run)
                else:
                    if action == "notification_add" and not data.get("session_id"):
                        data = dict(data)
                        if data.get("goal_id"):
                            data["session_id"] = self.store.goal(data["goal_id"])[0].get("session_id")
                        elif data.get("interest_id"):
                            data["session_id"] = self.store.read("interest", data["interest_id"], {}).get("session_id")
                        elif not info["cron"]:
                            data["session_id"] = info["id"]
                    result = getattr(self.service, action)(data)
            return self._response(result, action=action, data=data)
        except Exception as exc:
            log.warning("Hermes Muse action %s failed: %s", args.get("action"), exc)
            return self._error(exc, *self._call_of(args))

    @staticmethod
    def _response(result, *, action=None, data=None):
        return Runtime._receipt({"ok": True, "result": result}, action=action, data=data)

    @staticmethod
    def _receipt(envelope, *, action=None, data=None):
        """Bound the final envelope, including journal revision/recovery fields."""
        from .content_io import DomainError
        result = envelope.get("result") if envelope.get("ok") else None
        raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 256 * 1024:
            if envelope.get('ok') and action == 'operations_read' and isinstance(result, dict) and result.get('token'):
                return Runtime._receipt({**envelope, 'result': {
                    'token': result['token'], 'result_is_reference': True,
                    'content_ref': {'action': 'operations_evidence', 'data': {'receipt': result['token'], 'offset': 0, 'limit': 25}},
                    'findings_ref': {'action': 'content_read', 'data': {'kind': 'finding', 'limit': 25}},
                    'instruction': 'Allocation is saved, not reviewed. Page the native evidence index and expand every ref before operations_complete; no transcript copy is stored here.'}})
            from .contracts import CAS
            kinds = {**CAS, 'feed_add': 'feed', 'notification_add': 'notice', 'goal_create': 'goal', 'goal_update': 'goal', 'interest_record': 'interest', 'idea_add': 'idea', 'run_complete': 'run', 'installation_prepare': 'installation', 'research_start': 'research',
                     'notification_prepare': 'notice', 'notification_queue': 'notice', 'notification_handoff': 'notice'}
            kind = kinds.get(action)
            if action == 'feedback':
                kind = {'notification': 'notice', 'interest': 'interest'}.get((data or {}).get('kind'))
            record = result.get('notification', result) if isinstance(result, dict) else {}
            key = record.get('id') if isinstance(record, dict) else None
            if action in {'preferences', 'installation_prepare'}:
                key = 'current'
            if envelope.get("ok") and kind and key:
                bounded = {'id': key, 'result_is_reference': True,
                    'content_ref': {'action': 'content_read', 'data': {'kind': kind, 'id': key}},
                    'instruction': 'State committed. Read its paginated content_ref. If this action accepts request_id, replay only the original key/arguments; do not repeat a committed change.'}
                for field in ('changed', 'reviewed', 'partial', 'requires_host_action', 'final_response', 'warning'):
                    if isinstance(result, dict) and field in result:
                        bounded[field] = result[field]
                return Runtime._receipt({**envelope, "result": bounded})
            from .responses import ARCHIVE
            archive = ARCHIVE.get()
            if archive:
                # Exact completed results/errors, not another business state machine.
                return Runtime._receipt(archive(envelope))
            if not envelope.get('ok'):
                return json.dumps({'ok': False, 'code': str(envelope.get('code', 'invalid_argument'))[:128],
                                   'error': str(envelope.get('error', 'Tool failed'))[:2000],
                                   'error_truncated': True, 'retryable': False}, ensure_ascii=False)
            raise DomainError("size_limit", "Response exceeds 256 KiB; request a smaller page or source/window batch")
        return raw

    def transform_output(self, response_text="", session_id="", **kwargs):
        with self.lock:
            delivery = self.delivery_turns.pop(session_id, None)
            run = self.run_turns.pop(session_id, None)
        if self.closed:
            return None
        with profile_scope(self.home):
            result = None
            if delivery:
                result = (render_result(response_text, delivery["route"]["deliver"])
                          if delivery.get("kind") == "report" else self.service.finalize_delivery(delivery))
            if run:
                self.service.runs.finalize(run, notifications=bool(delivery))
            return result

    def handoff(self, key):
        return self.service.notification_queue({"id": key})

    def pre_turn(self, session_id="", turn_id="", user_message="", platform="", parent_session_id="", **kwargs):
        info = session_info()
        if not self.closed and not parent_session_id and private_session(info) and str(kwargs.get("task_id", "")).startswith("cron:"):
            with profile_scope(self.home):
                run = self.service.runs.bind(kwargs.get("task_id"))
                if run:
                    with self.lock:
                        self.run_turns[session_id] = run
                    self.service.operations.track(session_id, kwargs.get("task_id"))
        text = user_message if isinstance(user_message, str) else json.dumps(user_message, ensure_ascii=False)
        from agent.delegation_context import is_delegated_child_context
        if self.closed or parent_session_id or info["cron"] or platform in {"cron", "delegate", "subagent", "webhook", "msgraph_webhook", "kanban"} or not private_session(info):
            return None
        if text.lstrip().startswith(('[Cronjob "', "[Hermes Muse notification ", MARKER)):
            return {"context": receiving_instructions()}
        if is_background_message(text):
            value = self._followup_context(session_id, turn_id)
            return {"context": value + "\nBackground process completion is intermediate evidence. Report only blockers, changed direction or verified results; do not infer goal completion."} if value else None
        if not session_id or not turn_id or not text.strip():
            return None
        with self.lock:
            timer = self.timers.pop(session_id, None)
            if timer:
                timer.cancel()
        self.service.signal(turn_id, session_id, text, route_from(info))
        return {"context": f"[Muse] User signal: {turn_id}. Use muse_manage for accepted goals and preferences. If you accepted a concrete request or promise in this turn, make sure an active goal covers it (context section=goal); create one if not. Casual questions and passing mentions do not become goals.\n" + self._recent_goals(session_id) + self._followup_context(session_id, turn_id) + self.memory_feedback.context(session_id)}

    def _recent_goals(self, session_id):
        try:
            since = self.service.clock() - 86400
            recent = sorted((g for g in self.store.goals() if g.get('created', 0) >= since and g.get('session_id') == session_id),
                            key=lambda g: g['created'], reverse=True)
            titles = [' '.join(str(g.get('title', '')).split())[:60] for g in recent]
        except (OSError, ValueError):
            return ''
        if not titles:
            return ''
        more = f'; +{len(titles) - 5} more' if len(titles) > 5 else ''
        return '[Muse] Goals created in the last 24h: ' + '; '.join(titles[:5]) + more + '.\n'

    def _followup_context(self, session_id, turn_id):
        try:
            return followup_context(self.home, now=self.service.clock(), session_id=session_id)
        except (OSError, ValueError):
            return '[Muse] Goal data needs inspection; no automatic action was taken.'

    def post_tool(self, tool_name="", args=None, result=None, **kwargs):
        from agent.delegation_context import is_delegated_child_context
        if self.closed or is_delegated_child_context() or not private_session(session_info()):
            return None
        with profile_scope(self.home):
            self.memory_feedback.capture(tool_name, args or {}, result, **kwargs)

    def post_turn(self, session_id="", turn_id="", **kwargs):
        if self.closed:
            return
        self.memory_feedback.finish(session_id, turn_id, kwargs.get("assistant_response") or "", kwargs.get("conversation_history", ()))
        if private_session(session_info()):
            self.service.operations.track(session_id, kwargs.get("task_id"))
        session = self.store.read("session", session_id)
        if not session or session.get("turn_id") != turn_id:
            return
        with self.store.transaction() as db:
            session = self.store.get(db, "session", session_id, {})
            if session.get("turn_id") != turn_id:
                return
            session["active"] = False
            self.store.put(db, "session", session_id, session)
        signal = self.store.read("signal", turn_id, {})
        if len(signal.get("text", "")) < 80:
            return
        with self.lock:
            if self.closed:
                return
            previous = self.timers.pop(session_id, None)
            if previous:
                previous.cancel()
            timer = threading.Timer(self.quiet_seconds, self.quiet_pass, args=(session_id, turn_id))
            timer.daemon = True
            self.timers[session_id] = timer
            timer.start()

    def quiet_pass(self, session_id, turn_id):
        with self.lock, profile_scope(self.home):
            if self.closed:
                return
            session = self.store.read("session", session_id, {})
            if session.get("active") or session.get("turn_id") != turn_id:
                return
            today = self.host.local_now(time.time()).date().isoformat()
            with self.store.transaction() as db:
                count = self.store.get(db, "budget", "quiet:" + today, 0)
                if count >= 3:
                    return
                self.store.put(db, "budget", "quiet:" + today, count + 1)
            self.host.wake("memory-upkeep")
            self.timers.pop(session_id, None)

    def end_turn(self, session_id="", turn_id="", **kwargs):
        if self.closed:
            return
        from agent.delegation_context import is_delegated_child_context
        if private_session(session_info()) and not is_delegated_child_context() and not session_info()['cron']:
            self.memory_feedback.finish(session_id, turn_id)
        with self.lock:
            self.delivery_turns.pop(session_id, None)
            self.run_turns.pop(session_id, None)
        # Failed/interrupted turns must also release the local attention hold.
        with self.store.transaction() as db:
            row = self.store.get(db, "session", session_id)
            if row and row.get("turn_id") == turn_id:
                row["active"] = False
                self.store.put(db, "session", session_id, row)

    def close(self):
        with self.lock:
            self.closed = True
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()
            self.delivery_turns.clear()
            self.run_turns.clear()


def register(ctx, *, plugin_root=ROOT):
    from hermes_constants import get_hermes_home
    required = ("register_tool", "register_skill", "register_system_prompt_section", "register_hook", "on_unload")
    if any(not callable(getattr(ctx, name, None)) for name in required):
        raise RuntimeError("Hermes Muse needs Hermes 0.21.5+ with prompt sections, plugin skills and unload hooks")
    runtime = Runtime(ctx, get_hermes_home(), plugin_root=plugin_root)
    runtime.host.initialize()
    prompt = (runtime.host.root / "prompts/system.md").read_text(encoding="utf-8").replace("{{MUSE_HOME}}", str(runtime.store.root)).replace("{{HERMES_HOME}}", str(runtime.home))
    ctx.register_skill("companion", runtime.host.root / "skills/companion/SKILL.md", description="Calendar views, goals, memory upkeep, proactive reminders, reflection, Feed and research lifecycle")
    ctx.register_system_prompt_section("hermes-muse.companion", prompt, position="after_memory", max_chars=4000)
    ctx.register_tool(name="muse_manage", toolset="muse", schema=SCHEMA, handler=runtime.handle)
    ctx.register_hook("post_tool_call", runtime.post_tool)
    ctx.register_hook("on_skill_lifecycle", runtime.memory_feedback.lifecycle)
    ctx.register_hook("pre_llm_call", runtime.pre_turn)
    ctx.register_hook("post_llm_call", runtime.post_turn)
    ctx.register_hook("transform_llm_output", runtime.transform_output)
    ctx.register_hook("on_session_end", runtime.end_turn)
    ctx.on_unload(runtime.close)
