"""Bounded context discovery and receipt-backed signal review, without transcript copies."""
import base64
import json

from .content_io import DomainError, revision
from .views import Views, canonical
from .store import is_background_message, GOVERNANCE_JOBS
from .conversation import _merge

COLLECTIONS = ('goal', 'feed', 'interest', 'idea', 'notice', 'run', 'source_check', 'finding', 'research')
JOBS = ('memory-upkeep', 'nightly-review')


def limit(data):
    value = data.get('limit', 25)
    if type(value) is not int or not 1 <= value <= 100:
        raise DomainError('invalid_argument', 'limit must be 1..100')
    return value


def index(items, data, scope):
    """Fail explicitly on changed indexes; body reads use their own revisions."""
    fingerprint = revision(canonical([scope, items]))
    start = 0
    if data.get('cursor'):
        try:
            token = json.loads(base64.urlsafe_b64decode(data['cursor']))
            if token['revision'] != fingerprint or type(token['offset']) is not int or token['offset'] < 0:
                raise ValueError()
            start = token['offset']
        except Exception as exc:
            raise DomainError('cursor_expired', 'data.cursor is no longer valid: the index changed or the cursor was edited. Fix: resend without data.cursor '
                              '(and without data.window/revision) to restart this section, then pass each next_cursor back exactly as returned.',
                              field='data.cursor', hint='drop data.cursor and restart', retryable=True) from exc
    end = min(len(items), start + limit(data))
    return {'items': items[start:end], 'total': len(items), 'snapshot_revision': fingerprint,
            'next_cursor': base64.urlsafe_b64encode(canonical({'revision': fingerprint, 'offset': end})).decode() if end < len(items) else None}


def original_signal(service, row):
    # Legacy inbox rows have no native message ID. Correlate narrowly or fail closed.
    state = service.host.signal_original(row)
    matches = [m for m in state.get('messages', []) if m.get('role') == 'user'
               and m.get('text', '').startswith(row['text'])
               and abs((m.get('at') or 0) - row['created']) <= 300]
    if not state.get('available') or len(matches) != 1:
        raise DomainError('source_unavailable', 'Truncated signal needs one matching original private user message; missing/ambiguous evidence cannot advance the checkpoint')
    message = matches[0]
    return message['text'], {'message_id': message['id'], 'revision': revision(canonical(message)), 'total_chars': len(message['text'])}


def signal_revision(row):
    return revision(canonical({k:v for k,v in row.items() if k != 'upkeep'}))


def pending_signals(store, db=None):
    old = store.get(db,'cursor','memory-upkeep',0) if db is not None else store.read('cursor','memory-upkeep',0)
    rows = store.rows(db,'signal') if db is not None else store.all('signal')
    return sorted((r for r in rows if not is_background_message(r.get('text')) and
        ((r.get('upkeep',{}).get('revision') != signal_revision(r)) if r.get('upkeep') else r['created']>old)),key=lambda r:(r['created'],r['id']))


def signals(service, data):
    store, job = service.store, data.get('job')
    if job not in JOBS:
        raise DomainError('invalid_argument','signals requires job=memory-upkeep|nightly-review')
    if job=='nightly-review':
        # Exact original lookup is available; nightly has no second intake queue.
        if not data.get('id'):
            return {'job':job,'scope':'synthesis','items':[],'total':0,'next_cursor':None,
                    'unprocessed_user_signals':len(pending_signals(store)),
                    'instruction':'Synthesize confirmed memory, changes and unresolved cases. For a specific unverified decision, use signals with its exact id; do not replay all raw signals.'}
        row=store.read('signal',data['id'])
        if not row or is_background_message(row.get('text')):
            raise DomainError('not_found','Original user signal unavailable')
        text=row['text']
        if row.get('truncated'):text,_=original_signal(service,row)
        offset,length=data.get('offset',0),data.get('length',12000)
        if type(offset) is not int or not 0<=offset<=len(text) or type(length) is not int or not 1<=length<=24000:
            raise DomainError('invalid_argument','Use offset within body and length 1..24000')
        end=min(len(text),offset+length)
        return {'id':row['id'],'body':text[offset:end],'offset':offset,'next_offset':end if end<len(text) else None,'scope':'original_evidence_only'}
    token=data.get('window')
    if not token:
        if data.get('cursor') or data.get('id') or data.get('ids'):
            raise DomainError('invalid_argument','Open the signal scope first and use its window')
        pending=pending_signals(store)
        selected={r['id']:r for r in pending[:15]+pending[-15:]}
        refs=[{'id':r['id'],'created':r['created'],'revision':signal_revision(r),'total_chars':None if r.get('truncated') else len(r['text']),
               'requires_original':bool(r.get('truncated'))} for r in sorted(selected.values(),key=lambda r:(r['created'],r['id']))]
        through=service.clock(); token=revision(canonical([Views(store.home).scope(),job,through,refs]))
        store.write('review_window',token,{'job':job,'scope':Views(store.home).scope(),'through':through,'refs':refs,'served':{},'originals':{},'selection':'oldest-and-newest'})
    with store.transaction() as db:
        window=store.get(db,'review_window',token)
        if not window or window['scope']!=Views(store.home).scope() or window['job']!=job:
            raise DomainError('permission_denied','Review scope is not in this profile/job')
        window.setdefault('originals',{})
        byid={r['id']:r for r in window['refs']}
        for ident,ref in byid.items():
            row=store.get(db,'signal',ident)
            if not row or signal_revision(row)!=ref['revision']:
                raise DomainError('revision_conflict','Selected user text changed; reopen this scope, preserve prior real progress')
        base={'window':token,'job':job,'through':window['through'],'scope':'selected_signals',
              'backlog':len(pending_signals(store,db)), 'processed_through':store.get(db,'cursor',job,0),
              'instruction':'Read selected bodies (ids batches or id/offset pages), save facts, then review_complete(window,job,ids,summary). Unselected/unread signals remain pending; no global all-read claim.'}
        requested=data.get('ids') if 'ids' in data else [data['id']] if data.get('id') else None
        if requested is None:
            return {**base,**index(window['refs'],data,token)}
        if not isinstance(requested,list) or not 1<=len(requested)<=25 or any(not isinstance(i,str) or i not in byid for i in requested):
            raise DomainError('invalid_argument','data.ids must contain 1..25 IDs from this window')
        pieces=[]; budget=20000
        for ident in dict.fromkeys(requested):
            row=store.get(db,'signal',ident);text=row['text']; ref=byid[ident]
            original = None
            try:
                if ref.get('requires_original'):
                    text,original=original_signal(service,row)
                    previous=window['originals'].get(ident)
                    if previous and previous!=original:raise DomainError('revision_conflict','Original user text changed')
                    window['originals'][ident]=original
            except DomainError as exc:
                if 'ids' not in data:raise
                pieces.append({'id':ident,'gap':str(exc),'body':None});continue
            offset,length=data.get('offset',0),data.get('length',12000)
            if type(offset) is not int or not 0<=offset<=len(text) or type(length) is not int or not 1<=length<=24000:
                raise DomainError('invalid_argument','Use offset within body and length 1..24000')
            end=min(len(text),offset+length,offset+budget)
            part={'id':ident,'session_id':row['session_id'],'body':text[offset:end],'offset':offset,
                  'next_offset':end if end<len(text) else None,'total_chars':len(text),'revision':ref['revision']}
            if original:part['message_id']=original['message_id']
            if end>offset:window['served'][ident]=_merge(window['served'].get(ident,[])+[[offset,end]])
            pieces.append(part);budget-=end-offset
            if budget<=0:break
        from .responses import require_fit
        result={**base,'items':pieces} if 'ids' in data else {**base,**pieces[0]}
        require_fit(result)
        store.put(db,'review_window',token,window)
        return result


def check_review(service, data, db, old, up_to, through):
    row=service.store.get(db,'review_window',data.get('window'))
    if not row or row['job']!='memory-upkeep' or row['scope']!=Views(service.store.home).scope() or row['through']!=through:
        raise DomainError('permission_denied','Use the original profile/job/window/through')
    refs={r['id']:r for r in row['refs']}
    ids=data.get('ids')
    if ids is None:ids=[r['id'] for r in row['refs'] if r['created']<=up_to]
    if not isinstance(ids,list) or any(not isinstance(i,str) or i not in refs for i in ids):
        raise DomainError('invalid_argument','data.ids must name actually processed signals in this scope')
    for ident in set(ids):
        ref=refs[ident];signal=service.store.get(db,'signal',ident)
        if not signal or signal_revision(signal)!=ref['revision']:
            raise DomainError('revision_conflict','Selected original signal changed')
        size=ref['total_chars']
        if ref.get('requires_original'):
            _,original=original_signal(service,signal)
            if row.get('originals',{}).get(ident)!=original:
                raise DomainError('source_unavailable','Read the actual original before confirming this signal')
            size=original['total_chars']
        covered=row['served'].get(ident,[])
        if size and (not covered or covered[0][0]!=0 or covered[0][1]<size):
            raise DomainError('revision_required','Read the complete selected signal before confirming it')
    for ident in set(ids):
        signal=service.store.get(db,'signal',ident)
        signal['upkeep']={'at':service.clock(),'revision':refs[ident]['revision']}
        service.store.put(db,'signal',ident,signal)
    # Cursor only describes a real continuous prefix. Later processed IDs survive separately.
    all_rows=sorted((r for r in service.store.rows(db,'signal') if not is_background_message(r.get('text'))),key=lambda r:(r['created'],r['id']))
    cursor=old
    from itertools import groupby
    for at,group in groupby((r for r in all_rows if r['created']>old),key=lambda r:r['created']):
        if any(r.get('upkeep',{}).get('revision')!=signal_revision(r) for r in group):break
        cursor=at
    if not pending_signals(service.store,db):cursor=max(cursor,through)
    service.store.put(db,'cursor','memory-upkeep',cursor)
    remaining=[r for r in row['refs'] if service.store.get(db,'signal',r['id']).get('upkeep',{}).get('revision')!=r['revision']]
    return {'complete':not remaining,'remaining':len(remaining),'processed_ids':list(dict.fromkeys(ids)),
            'backlog':len(pending_signals(service.store,db)),'processed_through':cursor}


def context(service, data, *, view=None):
    views = Views(service.store.home, now=service.clock())
    section = data.get('section', 'overview')
    if section == 'followups':
        from .followup import snapshot
        # Followup pages hold at most 50 goals; larger requests page with next_cursor.
        return snapshot(service.store.home, now=service.clock(), cursor=data.get('cursor', ''), limit=min(limit(data), 50))
    if section in COLLECTIONS:
        return views.read({'kind': section, **{k: v for k, v in data.items() if k in {'cursor','limit','id','offset','length','revision','part','status'}}})
    if section == 'signals':
        return signals(service, data)
    if section == 'conversations':
        sessions = sorted(service.store.all('session'), key=lambda r: r['id'])
        result = index([{'id': s['id']} for s in sessions], data, views.scope())
        # Only descriptors: never mark a hidden/unreturned preview as served.
        result['items'] = [service.conversation_read({'session_id': s['id'], 'descriptor': True}) for s in result['items']]
        return result
    if section != 'overview':
        raise DomainError('invalid_argument', f"data.section {section!r} is not a section. Valid sections: {', '.join((*COLLECTIONS, 'conversations', 'followups', 'signals'))}. "
                          "Preferences and installation are not sections: content_read {kind:'preferences'|'installation', id:'current'}. "
                          "Arguments go inside data, e.g. {action:'context', data:{section:'notice', limit:25}}. Fix: set data.section to a valid section.", field='data.section')
    pending = pending_signals(service.store)
    signal_reviews = {
        'memory-upkeep': {'action':'context','data':{'section':'signals','job':'memory-upkeep','limit':25},
            'processed_through':service.store.read('cursor','memory-upkeep',0),'pending':len(pending),
            'oldest_pending':min((r['created'] for r in pending),default=None)},
        'nightly-review': {'mode':'synthesis','legacy_processed_through':service.store.read('cursor','nightly-review',0),
            'instruction':'No second raw-signal queue; use confirmed memory and actual changes.'}}
    runs = service.runs.recent()
    from .operations import unknown_outcomes
    unknown=unknown_outcomes(service.store.home)
    return {'now': service.clock(), 'job': view,
            'task_runs': [{'id': r['id'], 'job': r['job'], 'status': r['status'], 'started': r['started'],
                           'content_ref': {'action': 'content_read', 'data': {'kind': 'run', 'id': r['id']}}}
                          for r in runs],
            'health': [{'id':r['id'],'job':r['job'],'status':r['status'],'gap_count':len(r.get('gaps',[])),
                        'stages':{k:{'complete':v.get('complete')} for k,v in r.get('stages',{}).items()}}
                       for r in runs if r['status']=='partial'],
            'read_semantics': 'Bounded index. Collection pages use content_read revisions; signal windows freeze through and record served ranges.',
            'sections': {k: {'action': 'context', 'data': {'section': k, 'limit': 25}} for k in (*COLLECTIONS, 'conversations', 'followups')},
            'operation_review': {'policy':service.operations.policy()['version'],
                                 'scan_after':service.operations.policy()['scan_after'],
                                 'legacy_history_reviewed':False,
                                 'unknown_outcomes':unknown[:12], 'unknown_outcome_count':len(unknown),
                                 'instruction':'Inspect original unknown outcomes and active goal/followup owners; never retry them to obtain a nicer receipt.',
                                 'findings_ref': {'action': 'context', 'data': {'section': 'finding'}}},
            'reviews': {job: service.store.read('meta', 'last_review:' + job) for job in GOVERNANCE_JOBS},
            'signal_reviews': signal_reviews,
            'preferences_ref': {'action': 'content_read', 'data': {'kind': 'preferences', 'id': 'current'}},
            'installation_ref': {'action': 'content_read', 'data': {'kind': 'installation', 'id': 'current'}},
            'instruction': 'Read only task-relevant sections and selected signal bodies; collection pagination is available when a complete list is actually needed. Review newest required conversation text for notices. Existing unknown owners are inspected, never redispatched.'}
