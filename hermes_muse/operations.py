"""Read native operation evidence; keep only checkpoints, references and findings."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from .store import DAY, bounded, digest, epoch

GROUPS = {"group", "supergroup", "channel", "guild", "room"}
INSPECTION = {"context", "status", "conversation_read", "operations_read", "operations_complete", "operations_batch", "operations_evidence", "operations_commit", "finding_record"}
INSTRUCTION_CONTENT = {"working_lessons", "tools_note"}


@contextmanager
def reader(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def calls(row):
    value = row.get("tool_calls") or []
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value,list) or any(not isinstance(item,dict) for item in value):
        raise ValueError('Native tool_calls is not a structured call array')
    return value


def inspection(call):
    fn = call.get("function", {})
    try:
        args = fn.get("arguments", {})
        args = json.loads(args) if isinstance(args, str) else args
        if fn.get("name") == "tool_call":
            nested = args.get("calls")
            return bool(nested) and isinstance(nested, list) and all(
                inspection({"function": item}) for item in nested)
        return (fn.get("name") == "muse_manage" and (args.get("action") in INSPECTION
                    or args.get("action") == "content_read" and (args.get("data") or {}).get("kind") in INSTRUCTION_CONTENT)
                or fn.get("name") in {"skill_view", "tool_search", "tool_describe"})
    except (AttributeError, TypeError, ValueError):
        return False


def failed_result(content):
    """Keep failures even when the operation was only an inspection."""
    try:
        value = json.loads(content or "null")
    except (ValueError, TypeError):
        return any(word in str(content).lower() for word in ("error", "failed", "denied"))
    if isinstance(value, dict):
        return (value.get("ok") is False or value.get("success") is False
                or value.get("isError") is True or bool(value.get("error"))
                or value.get('status') in {'error','failed','unknown','outcome_unknown'}
                or value.get('code') == 'outcome_unknown')
    return False


class Operations:
    def __init__(self, store, host, clock=time.time):
        self.store, self.host, self.clock = store, host, clock

    def track(self, session_id, task_id):
        """Only host-supplied bindings, including silent jobs, not model-supplied IDs."""
        parts = str(task_id or "").split(":", 2)
        if session_id and len(parts) == 3 and parts[0] == "cron" and parts[1] in self.host.manifest()["jobs"]:
            self.store.write("operation_session", session_id, {"id": session_id, "job_id": parts[1], "execution_id": parts[2]})

    def policy(self):
        """One explainable boundary; the legacy cursor/wave are evidence, never reviewed."""
        existing=self.store.read('meta','operation_policy')
        if existing:return existing
        with self.store.transaction() as state:
            policy = self.store.get(state, 'meta', 'operation_policy')
            if policy:
                return policy
            old = self.store.get(state, 'meta', 'operation_cursor')
            ceiling = 0
            if old and (self.store.home/'state.db').exists():
                with reader(self.store.home/'state.db') as native:
                    ceiling = native.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
            wave=self.store.get(state,'meta','operation_wave')
            owners=[self.store.get(state,'operation_dispatch',p['receipt']) for p in (wave or {}).get('pages',[])]
            policy = {'version': 'consequential-v1', 'started': self.clock(), 'scan_after': ceiling,
                      'since': self.clock()-DAY, 'legacy': {'cursor': old,
                      'wave':wave,'owners':[o for o in owners if o], 'reviewed': False},
                      'limits': 'Selected consequential cases and bounded opaque samples, not a full tool-log audit. Hidden opaque side effects may be missed.'}
            self.store.put(state,'meta','operation_policy',policy)
            return policy

    def checkpoint(self):
        return self.policy()

    def summary(self):
        findings=[r for r in self.store.all('operation_finding') if r.get('status') in {'open','repairing'}]
        return {'selection': self.policy(), 'findings': [{k:r.get(k) for k in ('id','status','updated')} for r in findings[:12]],
                'open_finding_count':len(findings),
                'instruction': 'Review current health, unresolved findings and selected cases, not all historical tool rows. Legacy history remains unreviewed; full finding bodies are available by id.'}

    @staticmethod
    def reason(call):
        fn = call.get('function',call)
        name = fn.get('name','')
        args = fn.get('arguments',{})
        try:
            args = json.loads(args) if isinstance(args,str) else args
        except (ValueError,TypeError):
            return 'unknown_arguments'
        if not isinstance(args,dict):
            return 'unknown_arguments'
        if name == 'tool_call':
            nested = args.get('calls')
            if not isinstance(nested,list):
                return 'unknown_arguments'
            reasons = [Operations.reason(c) for c in nested if isinstance(c,dict)]
            return next((r for r in reasons if r and r != 'opaque_sample'), 'opaque_sample' if 'opaque_sample' in reasons else None)
        if name == 'muse_manage':
            action = args.get('action','')
            if action in {'goal_create','goal_update','watch_create','watch_stop','forget','feedback','preferences','feed_update','content_write','notification_add','notification_refresh','notification_prepare','notification_queue','notification_handoff','notification_resolve'}:
                return 'structured_write'
            return None
        if inspection(call) or name in {'read','read_file','list_files','glob','grep','web_search','web_extract','fetch_content'}:
            return None
        short_name=name.rsplit('__',1)[-1]
        if short_name in {'write','edit','write_file','edit_file','apply_patch','delete_file','send_message','send_email','send_mail'} or args.get('action',args.get('operation')) in {'send','create','update','delete','write','edit','publish'}:
            return 'structured_write'
        # A terminal name or zero exit code cannot prove absence of side effects.
        if name in {'terminal','execute_code','shell','bash'}:
            return 'opaque_execution'
        return 'opaque_sample'

    def read(self, data, *, page_cursor=None, compact=False):
        if 'up_to' in data or data.get('through') is not None:
            raise ValueError('Consequential review selects cases; omit legacy through/up_to boundaries. Old raw-log cursors remain unreviewed.')
        policy, now = self.policy(), self.clock()
        result = {'available': True, 'events': [], 'executions': [], 'gaps': [],
                  'scope': 'selected_cases', 'findings': [] if compact else self.summary()['findings']}
        path = self.store.home/'state.db'
        if not path.exists():
            return {**result, 'available': False, 'gaps': ['Native history unavailable; no scan or review advanced.']}
        roots = {r['id'] for r in self.store.all('session')} | {r['id'] for r in self.store.all('operation_session')}
        jobs = self.host.manifest()['jobs']
        try:
            accepted = {g.get('session_id') for g in self.store.goals() if g.get('status')=='active' and g.get('session_id')}
        except (ValueError,KeyError,TypeError):
            accepted = roots
            result['gaps'].append('Accepted-work metadata unreadable; opaque executions remain unknown, not safe.')
        chosen, sampled, after = {}, 0, policy['scan_after']
        call_index = {}
        with reader(path) as db:
            sessions = {r['id']:dict(r) for r in db.execute('SELECT id,parent_session_id,chat_type FROM sessions')}
            eligible = set()
            for sid in sessions:
                current, seen, matched, private = sid, set(), False, True
                while current in sessions and current not in seen:
                    seen.add(current); item = sessions[current]
                    private &= str(item.get('chat_type') or '').lower() not in GROUPS
                    matched |= current in roots or any(current.startswith('cron_'+jid+'_') for jid in jobs)
                    current = item.get('parent_session_id')
                if matched and private:
                    eligible.add(sid)
            columns = {r[1] for r in db.execute('PRAGMA table_info(messages)')}
            hidden = " AND COALESCE(display_kind,'')!='hidden'" if 'display_kind' in columns else ''
            uid = ',message_uid' if 'message_uid' in columns else ''
            rows = db.execute('SELECT id,session_id,role,substr(content,1,2000) AS content,substr(tool_calls,1,8000) AS tool_calls,tool_call_id,tool_name,timestamp'+uid+' '
                              'FROM messages WHERE id>? AND timestamp>=?'+hidden+' ORDER BY id LIMIT 400', (after,policy['since'])).fetchall()
            for raw in rows:
                row = dict(raw); after = row['id']
                if row['session_id'] not in eligible:
                    continue
                if row.get('message_uid'):
                    original=db.execute('SELECT id FROM messages WHERE session_id=? AND message_uid=? AND id<? '
                        'AND role IS (SELECT role FROM messages WHERE id=?) AND content IS (SELECT content FROM messages WHERE id=?) '
                        'AND tool_calls IS (SELECT tool_calls FROM messages WHERE id=?) AND tool_call_id IS (SELECT tool_call_id FROM messages WHERE id=?) '
                        'AND tool_name IS (SELECT tool_name FROM messages WHERE id=?) AND timestamp IS (SELECT timestamp FROM messages WHERE id=?) LIMIT 1',
                        (row['session_id'],row['message_uid'],row['id'],*[row['id']]*6)).fetchone()
                    if original:continue
                reason, related = None, None
                if row['role']=='tool':
                    if failed_result(row['content']):
                        reason='reported_failure_or_unknown'
                    if row.get('tool_call_id'):
                        cached=call_index.get((row['session_id'],row['tool_call_id']))
                        if cached:
                            related,match=cached;reason=reason or self.reason(match)
                        else:
                            for candidate in db.execute('SELECT id,tool_calls FROM messages WHERE session_id=? AND id<? AND instr(tool_calls,?)>0 ORDER BY id DESC', (row['session_id'],row['id'],row['tool_call_id'])):
                                try:match=next((c for c in calls(dict(candidate)) if c.get('id')==row['tool_call_id']),None)
                                except (ValueError,TypeError):continue
                                if match:
                                    related=candidate['id']; reason=reason or self.reason(match); break
                elif row['tool_calls']:
                    try:
                        parsed=calls(row)
                        for c in parsed:
                            if c.get('id'):call_index[(row['session_id'],c['id'])]=(row['id'],c)
                        reasons=[self.reason(c) for c in parsed]
                        reason=next((r for r in reasons if r and r!='opaque_sample'),'opaque_sample' if 'opaque_sample' in reasons else None)
                    except (ValueError,TypeError,AttributeError):
                        reason='unknown_arguments'
                elif row['role']=='assistant' and not any(row['session_id'].startswith('cron_'+jid+'_') for jid in jobs):
                    import re
                    if re.search(r'已完成|已发送|已保存|已更新|已修复|\b(completed|sent|saved|fixed)\b',row['content'] or '',re.I):
                        reason='completion_claim_needs_verification'
                if not reason:
                    continue
                if reason=='opaque_execution':
                    reason='opaque_accepted_work' if row['session_id'] in accepted else 'opaque_sample'
                if reason=='opaque_sample':
                    if sampled>=2:continue
                    sampled+=1
                user=db.execute("SELECT id FROM messages WHERE session_id=? AND id<? AND role='user'"+hidden+" ORDER BY id DESC LIMIT 1",(row['session_id'],row['id'])).fetchone()
                # One case per original user turn; calls/results and context stay linked once.
                user_id=user['id'] if user else None
                key=digest(row['session_id']+':'+str(user_id or related or row['id']))
                case=chosen.setdefault(key,{'session_id':row['session_id'],'refs':[],'reasons':[], 'user_ref':'message:'+str(user_id) if user_id else None})
                refs=['message:'+str(n) for n in [related,row['id']] if n]
                for ref in refs:
                    if ref not in case['refs']:case['refs'].append(ref)
                if reason not in case['reasons']:case['reasons'].append(reason)
                if len(chosen)>=12:break
        ledger=self.store.home/'cron/executions.db'
        if ledger.exists():
            with reader(ledger) as db:
                seen=set()
                for raw in db.execute('SELECT * FROM executions ORDER BY claimed_at DESC LIMIT 1000'):
                    execution=dict(raw); jid=execution['job_id']
                    if jid not in jobs or jid in seen:continue
                    seen.add(jid)
                    if execution['status'] in {'failed','cancelled'} or execution.get('delivery_outcome')=='unknown':
                        chosen[digest('health:'+jid)]={'session_id':None,'refs':['execution:'+execution['id']],
                            'reasons':['unresolved_execution_or_delivery'],'user_ref':None}
        with self.store.transaction() as state:
            # Concurrent scans merge their references; never replace newer writes.
            current=self.store.get(state,'meta','operation_policy')
            current['scan_after']=max(current['scan_after'],after)
            self.store.put(state,'meta','operation_policy',current)
            for key,case in chosen.items():
                token='case-'+key
                old=self.store.get(state,'operation_snapshot',token,{})
                refs=list(dict.fromkeys(old.get('refs',[])+case['refs']+([case['user_ref']] if case['user_ref'] else [])))
                changed=refs!=old.get('refs')
                reasons=list(dict.fromkeys(old.get('reasons',[])+case['reasons']))
                self.store.put(state,'operation_snapshot',token,{**old,**case,'reasons':reasons,'token':token,'policy':current['version'],
                    'refs':refs,'execution_through':now,'reviewed':False if changed else old.get('reviewed',False),'created':old.get('created',now)})
        pending=[r for r in self.store.all('operation_snapshot') if r.get('policy')==policy['version'] and not r.get('reviewed')]
        # Reserve a place for the oldest continuation; recent cases do not queue behind raw history.
        pending.sort(key=lambda r:r['created'])
        continuation=pending[:1]
        rest=sorted(pending[1:],key=lambda r:(not any('failure' in k or 'unknown' in k or 'claim' in k for k in r['reasons']),-r['created']))
        selected=continuation+rest[:11]
        for case in selected:
            result['events'].append({'ref':case['token'],'receipt':case['token'],'session_id':case['session_id'],
                'reasons':case['reasons'],'evidence_refs':case['refs'],'user_context_ref':case.get('user_ref')})
        result.update(checkpoint=self.policy(),more=bool(rows) and (after<rows[-1]['id'] or len(rows)==400),pending_cases=len(pending),opaque_samples=sampled,
                      instruction='Selected cases only. Expand each case receipt, read relevant original context once, verify consequences and save findings. Complete only actually reviewed receipts. Unselected history is not certified safe.')
        return result

    def complete(self, data):
        token=data.get('token');bounded(data.get('summary'),2000)
        with self.store.transaction() as db:
            row=self.store.get(db,'operation_snapshot',token,{})
            if row.get('policy')!='consequential-v1':
                raise ValueError('Legacy full-audit history is retired, not reviewed; inspect its evidence when relevant, never advance its old cursor')
            refs=row['refs']
        # Existence is not semantic acceptance; the parent supplies that after checking evidence.
        for start in range(0,len(refs),100):
            if self.evidence({'receipt':token,'offset':start,'limit':100})['missing']:
                raise ValueError('Missing evidence: preserve this case as partial')
        with self.store.transaction() as db:
            current=self.store.get(db,'operation_snapshot',token)
            if current['refs']!=refs:
                raise ValueError('New evidence arrived for this case; review it before completion')
            duplicate=bool(current.get('reviewed'))
            current.update(reviewed=True,summary=data['summary'],reviewed_at=self.clock())
            self.store.put(db,'operation_snapshot',token,current)
        return {'reviewed':True,'already_reviewed':duplicate,'checkpoint':self.policy(),'more':False,'scope':'selected_case'}

    def dispatch_result(self, dispatch):
        """Historical original receipt only, never a relaunch or an acceptance."""
        if not dispatch.get('session_id') or not dispatch.get('tool_call_id'):return None
        with reader(self.store.home/'state.db') as db:
            row=db.execute("SELECT id,content FROM messages WHERE session_id=? AND role='tool' AND tool_name='muse_manage' AND tool_call_id=? ORDER BY id DESC LIMIT 1",(dispatch['session_id'],dispatch['tool_call_id'])).fetchone()
        if not row:return None
        try:return {'ref':'message:'+str(row['id']),'envelope':json.loads(row['content'])}
        except (ValueError,TypeError):return {'ref':'message:'+str(row['id']),'text':row['content'] or ''}

    def evidence(self, data):
        from .views import Views
        snapshot=Views(self.store.home).record('operation_snapshot',data.get('receipt')) or {}
        if not snapshot:
            raise ValueError('Unknown evidence receipt; ask the parent for its selected case')
        refs=snapshot['refs']; ref=data.get('ref')
        if ref is not None and ref not in refs:raise ValueError('Evidence must belong to this profile receipt')
        offset,limit=data.get('offset',0),data.get('limit',25)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise ValueError('Use offset>=0 and limit 1..100')
        selected=[ref] if ref else refs[offset:offset+limit]
        # Slice references BEFORE loading any native row. Index never loads bodies/context.
        events,executions,missing=[],[],[]
        for item in selected:
            kind,ident=item.split(':',1)
            path=self.store.home/('state.db' if kind=='message' else 'cron/executions.db')
            if not path.exists():missing.append(item);continue
            with reader(path) as db:
                if kind=='message':
                    cols='id,session_id,role,tool_call_id,tool_name,timestamp'
                    if ref:cols+=',content,tool_calls'
                    row=db.execute('SELECT '+cols+' FROM messages WHERE id=?',(int(ident),)).fetchone()
                else:row=db.execute('SELECT * FROM executions WHERE id=?',(ident,)).fetchone()
            if not row:missing.append(item);continue
            row=dict(row);row['ref']=item
            at=row.get('timestamp',epoch(row.get('finished_at') or row.get('claimed_at') or 0))
            if data.get('session_id') and row.get('session_id')!=data['session_id'] or data.get('job_id') and row.get('job_id')!=data['job_id'] or at<epoch(data.get('since',0)) or at>epoch(data.get('until',self.clock())):continue
            if kind=='message' and ref and row.get('tool_calls'):
                row['content']=None
            (events if kind=='message' else executions).append(row)
        identity={'receipt':data['receipt'],'expected_ref_count':len(refs),'returned_refs':[r['ref'] for r in events+executions],
                  'missing':missing,'historical_content':True,'observed_at':self.clock(),'user_context_ref':snapshot.get('user_ref')}
        if not ref:return {**identity,'events':events,'executions':executions,'total':len(refs),'next_offset':offset+limit if offset+limit<len(refs) else None}
        if not events and not executions and not missing:raise ValueError('Evidence does not match the requested filters')
        text=json.dumps((events+executions)[0],ensure_ascii=False) if events or executions else ''
        length=data.get('length',12000)
        if type(length) is not int or not 1<=length<=24000:raise ValueError('Use length 1..24000')
        return {**identity,'ref':ref,'text':text[offset:offset+length],'offset':offset,'next_offset':offset+length if offset+length<len(text) else None,'total_chars':len(text)}

    def finding(self, data):
        with self.store.transaction() as db:
            snapshot = self.store.get(db, "meta", "operation_snapshot", {})
            refs_seen = set(snapshot.get("refs", []))
            for receipt in self.store.rows(db, "operation_snapshot"):
                if receipt["execution_through"] >= self.clock() - 7 * DAY:
                    refs_seen.update(receipt["refs"])
            key = data.get("id")
            old = self.store.get(db, "operation_finding", key, {}) if key else {}
            if key and not old:
                raise ValueError("Unknown finding")
            refs = data.get("evidence", old.get("evidence", []))
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in refs_seen | set(old.get("evidence", [])) for r in refs):
                raise ValueError("Finding needs evidence references returned by operations_read")
            key = key or digest("|".join(sorted(refs)))
            if self.store.get(db, "forgotten", "operation_finding:" + key):
                raise ValueError("This finding was explicitly forgotten; do not recreate it")
            previous = self.store.get(db, "operation_finding", key, old)
            row = {**previous, "id": key, "evidence": refs, "updated": self.clock()}
            for field in ("summary", "impact", "verification", "disclosure", "no_notice_reason"):
                if field in data:
                    row[field] = bounded(data[field], 4000)
            if not row.get("summary") or not row.get("impact"):
                raise ValueError("Finding needs a factual summary and impact")
            row["status"] = data.get("status", row.get("status", "open"))
            if row["status"] not in {"open", "repairing", "resolved", "dismissed"}:
                raise ValueError("Invalid finding status")
            if row["status"] in {"resolved", "dismissed"} and (not row.get("verification") or not (row.get("disclosure") or row.get("no_notice_reason"))):
                raise ValueError("Closure requires verification and disclosure evidence or a reason no notice is needed")
            self.store.put(db, "operation_finding", key, row)
            return row
