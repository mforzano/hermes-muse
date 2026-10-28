"""Small selected-case compatibility facade; no worker waves or raw-log prefix."""
from .store import bounded


class Audit:
    def __init__(self, operations):
        self.ops, self.store = operations, operations.store

    def batch(self):
        page = self.ops.read({}, compact=True)
        if not page['available']:
            return page
        with self.store.transaction() as db:
            previous = self.store.get(db, 'meta', 'operation_wave', {})
            pages = [{'slot':i+1,'receipt':r['receipt'],'reviewed':False} for i,r in enumerate(page['events'])]
            wave = {'policy':'consequential-v1','number':previous.get('number',0)+1,'pages':pages}
            self.store.put(db,'meta','operation_wave',wave)
        return {**page,'batch':wave['number'],'cases':pages,'assignments':[],
                'batch_complete':not pages,'remaining_slots':len(pages),
                'instruction':'Parent reviews selected cases directly. Read original context once, save consequential findings, then operations_commit only actually reviewed slots. No default delegation or historical full audit.'}

    def commit(self, data):
        bounded_reviews = data.get('reviews')
        if not isinstance(bounded_reviews,list) or not bounded_reviews:
            raise ValueError('reviews needs actual reviewed slots and summaries')
        wave = self.store.read('meta','operation_wave',{})
        if wave.get('policy')!='consequential-v1' or data.get('batch')!=wave['number']:
            raise ValueError('Unknown selected-case batch; legacy full-audit waves are retained unreviewed')
        slots={p['slot']:p for p in wave['pages']}
        for item in bounded_reviews:
            if not isinstance(item,dict) or item.get('slot') not in slots:
                raise ValueError('Review slot must belong to this batch')
            bounded(item.get('summary'),2000)
        for item in bounded_reviews:
            p=slots[item['slot']]
            self.ops.complete({'token':p['receipt'],'summary':item['summary']})
        # Derive state from persisted case outcomes, not a stale batch overwrite.
        remaining=sum(not self.store.read('operation_snapshot',p['receipt'],{}).get('reviewed') for p in wave['pages'])
        return {'batch':wave['number'],'batch_complete':not remaining,'remaining_slots':remaining,
                'checkpoint':self.ops.policy(),'more':False,'scope':'selected_cases'}

    def dispatch(self, request, callback, *, session_id='', tool_call_id=None):
        policy=self.ops.policy()
        legacy=(policy.get('legacy') or {}).get('wave') or {}
        tasks=request.get('tasks',[request])
        receipts=[p['receipt'] for p in legacy.get('pages',[])]
        if any(str(t.get('goal','')).startswith('Review audit batch ') or
               any(r in str(t.get('goal','')) for r in receipts) for t in tasks):
            raise ValueError('Legacy audit owner is retained; do not relaunch it. Review selected cases directly; old history is not complete.')
        return callback()
