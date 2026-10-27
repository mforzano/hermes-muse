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


def signal_refs(store, after, through, db=None):
    rows = sorted((r for r in (store.rows(db, 'signal') if db is not None else store.all('signal')) if after < r['created'] <= through
                   and not is_background_message(r.get('text'))), key=lambda r: (r['created'], r['id']))
    return [{'id': r['id'], 'created': r['created'], 'revision': revision(canonical(r)),
             'total_chars': None if r.get('truncated') else len(r['text']),
             'excerpt_chars': len(r['text']) if r.get('truncated') else None, 'requires_original': bool(r.get('truncated'))} for r in rows]


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


def signals(service, data):
    store = service.store
    job = data.get('job')
    if job not in JOBS:
        raise DomainError('invalid_argument', 'signals requires job=memory-upkeep|nightly-review')
    scope = Views(store.home).scope()
    token = data.get('window')
    if not token:
        if data.get('cursor') or data.get('id'):
            raise DomainError('invalid_argument', 'Continue with the returned window')
        after, through = store.read('cursor', job, 0), service.clock()
        refs = signal_refs(store, after, through)
        token = revision(canonical([scope, job, after, through, refs]))
        window = {'job': job, 'scope': scope, 'after': after, 'through': through, 'refs': refs, 'served': {}}
        store.write('review_window', token, window)
    with store.transaction() as db:
        window = store.get(db, 'review_window', token)
        if not window or window['scope'] != scope or window['job'] != job:
            raise DomainError('permission_denied', 'Review window is not in this profile/job')
        # Appends after through belong to the next window; edits/backdated inserts invalidate this one.
        if signal_refs(store, window['after'], window['through'], db) != window['refs']:
            raise DomainError('revision_conflict', 'Signal window changed; open a new window at the saved checkpoint')
        base = {'window': token, 'job': job, 'through': window['through'], 'after': window['after'], 'processed_through': store.get(db, 'cursor', job, 0),
                'instruction': 'Skip IDs at/before processed_through. Index is not reviewed evidence. Read each remaining id with window, offset and length; then review_complete(window,job,up_to,through,summary). Preserve timestamp ties.'}
        if not data.get('id'):
            return {**base, **index(window['refs'], data, token)}
        ref = next((r for r in window['refs'] if r['id'] == data['id']), None)
        if not ref:
            raise DomainError('permission_denied', 'Signal is outside this review window')
        row = store.get(db, 'signal', ref['id'])
        text = row['text']
        if ref.get('requires_original'):
            text, original = original_signal(service, row)
            old = window.get('originals', {}).get(ref['id'])
            if old and old != original:
                raise DomainError('revision_conflict', 'Original signal changed; preserve partial work and reopen its review window')
            window.setdefault('originals', {})[ref['id']] = original
        else:
            original = {'revision': ref['revision'], 'total_chars': len(text)}
        offset, length = data.get('offset', 0), data.get('length', 12000)
        if type(offset) is not int or not 0 <= offset <= len(text) or type(length) is not int or not 1 <= length <= 24000:
            raise DomainError('invalid_argument', 'Use offset within body and length 1..24000')
        end = min(len(text), offset + length)
        result = {**base, **ref, **original, 'session_id': row['session_id'], 'body': text[offset:end],
                  'offset': offset, 'next_offset': end if end < len(text) else None}
        from .responses import require_fit
        require_fit(result)
        window['served'][ref['id']] = _merge(window['served'].get(ref['id'], []) + [[offset, end]])
        store.put(db, 'review_window', token, window)
        return result


def check_review(service, data, db, old, up_to, through):
    token = data.get('window')
    if not token:
        raise DomainError('revision_required', 'Read context(section=signals,job=...) and supply its window before advancing signals')
    row = service.store.get(db, 'review_window', token)
    if not row or row['job'] != data['job'] or row['scope'] != Views(service.store.home).scope() or row['through'] != through:
        raise DomainError('permission_denied', 'Checkpoint must use its original profile/job/window/through')
    if old < row['after'] or signal_refs(service.store, row['after'], through, db) != row['refs']:
        raise DomainError('revision_conflict', 'Signal window changed; reopen from the current checkpoint')
    for ref in row['refs']:
        if not old < ref['created'] <= up_to:
            continue
        count = ref['total_chars']
        if ref.get('requires_original'):
            signal = service.store.get(db, 'signal', ref['id'])
            _, original = original_signal(service, signal)
            if row.get('originals', {}).get(ref['id']) != original:
                raise DomainError('permission_denied', 'Original long signal has not been fully read in this window')
            count = original['total_chars']
        if count and row['served'].get(ref['id']) != [[0, count]]:
            raise DomainError('permission_denied', 'Checkpoint skips unread signal evidence: ' + ref['id'])


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
    signal_reviews = {}
    signal_rows = [r for r in service.store.all('signal') if not is_background_message(r.get('text'))]
    for job in JOBS:
        checkpoint = service.store.read('cursor', job, 0)
        pending = [r['created'] for r in signal_rows if r['created'] > checkpoint]
        signal_reviews[job] = {'action': 'context', 'data': {'section': 'signals', 'job': job, 'limit': 25},
                               'processed_through': checkpoint, 'pending': len(pending),
                               'oldest_pending': min(pending) if pending else None}
    return {'now': service.clock(), 'job': view,
            'task_runs': [{'id': r['id'], 'job': r['job'], 'status': r['status'], 'started': r['started'],
                           'content_ref': {'action': 'content_read', 'data': {'kind': 'run', 'id': r['id']}}}
                          for r in service.runs.recent()],
            'read_semantics': 'Bounded index. Collection pages use content_read revisions; signal windows freeze through and record served ranges.',
            'sections': {k: {'action': 'context', 'data': {'section': k, 'limit': 25}} for k in (*COLLECTIONS, 'conversations', 'followups')},
            'operation_review': {'checkpoint': service.store.read('meta', 'operation_cursor'), 'findings_ref': {'action': 'context', 'data': {'section': 'finding'}}},
            'reviews': {job: service.store.read('meta', 'last_review:' + job) for job in GOVERNANCE_JOBS},
            'signal_reviews': signal_reviews,
            'preferences_ref': {'action': 'content_read', 'data': {'kind': 'preferences', 'id': 'current'}},
            'installation_ref': {'action': 'content_read', 'data': {'kind': 'installation', 'id': 'current'}},
            'instruction': 'Follow every section cursor. Read signal bodies before review_complete; no index is completion. Notification review requires the newest text of each tracked conversation. Existing followup unknown owners are inspected, never redispatched.'}
