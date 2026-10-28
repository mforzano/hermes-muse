import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from test_companion import prepare, Host
from hermes_muse.service import Companion
from hermes_muse.context_pages import context
from hermes_muse.content_io import DomainError
from hermes_muse.conversation import descriptor, page, REVIEW_CHARS
from hermes_muse.store import digest
from hermes_muse.views import Views


class ContextPagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = prepare(Path(self.tmp.name))
        self.now = 1800000000
        self.service = Companion(self.store, Host(), lambda: self.now)

    def test_goal_list_projects_state_orders_open_first_and_filters_status(self):
        self.service.signal('s', 'parent', 'Plan several things')
        for key in ('quiet', 'wait', 'stuck', 'old'):
            self.service.goal_create({'id': key, 'title': key.title(), 'description': 'd',
                                      'completion_criteria': 'c', 'signal_id': 's'})
        self.service.goal_update({'id': 'wait', 'waiting_user': True, 'progress': 'x' * 600})
        self.service.goal_update({'id': 'stuck', 'blocked': 'Needs a key'})
        self.service.goal_update({'id': 'old', 'status': 'done', 'note': 'Finished'})
        views = Views(self.store.home)
        order = lambda **q: [r['id'] for r in views.read({'kind': 'goal', **q})['items']]
        self.assertEqual(order(), ['wait', 'stuck', 'quiet', 'old'])
        self.assertEqual(order(status='open'), ['wait', 'stuck', 'quiet'])
        self.assertEqual(order(status='closed'), ['old'])
        page = views.read({'kind': 'goal', 'status': 'open', 'limit': 2})
        self.assertEqual(page['total'], 3)
        rest = views.read({'kind': 'goal', 'status': 'open', 'limit': 2, 'cursor': page['next_cursor']})
        self.assertEqual([r['id'] for r in rest['items']], ['quiet'])
        wait = page['items'][0]
        self.assertTrue(wait['waiting_user'])
        self.assertEqual(len(wait['latest_progress']['text']), 512)
        self.assertTrue(wait['summary_truncated'])
        self.assertEqual(page['items'][1]['blocked'], 'Needs a key')
        for bad in ({'kind': 'goal', 'status': 'bogus'}, {'kind': 'feed', 'status': 'open'}):
            with self.assertRaises(DomainError):
                views.read(bad)

    def test_idea_list_reports_last_nightly_review(self):
        views = Views(self.store.home)
        self.assertIsNone(views.read({'kind': 'idea'})['last_review_at'])
        with self.store.transaction() as db:
            self.store.put(db, 'run', 'r1', {'id': 'r1', 'job': 'nightly-review', 'started': 123.0})
        self.assertEqual(views.read({'kind': 'idea'})['last_review_at'], 123.0)

    def test_selected_signals_batch_new_correction_and_ties_preserve_old_authority(self):
        with self.store.transaction() as db:
            for i in range(40):
                self.store.put(db,'signal',str(i),{'id':str(i),'session_id':'s','created':self.now-100+i//2,'text':'User decision '+str(i)})
        first=context(self.service,{'section':'signals','job':'memory-upkeep'})
        self.assertEqual(first['total'],30)
        data={'section':'signals','job':'memory-upkeep','window':first['window']}
        cp={'job':'memory-upkeep','window':first['window'],'through':first['through'],'ids':['39'],'summary':'Saved a recent explicit correction'}
        with self.assertRaises(DomainError):self.service.review_complete(cp,require_window=True)
        context(self.service,{**data,'ids':['39']})
        result=self.service.review_complete(cp,require_window=True)
        self.assertFalse(result['complete'])
        self.assertEqual(self.store.read('cursor','memory-upkeep',0),0)
        self.assertIsNotNone(self.store.read('signal','39')['upkeep'])
        reopened=context(self.service,{'section':'signals','job':'memory-upkeep'})
        ids=[r['id'] for r in reopened['items']]
        if reopened['next_cursor']:
            ids += [r['id'] for r in context(self.service,{'section':'signals','job':'memory-upkeep','window':reopened['window'],'cursor':reopened['next_cursor']})['items']]
        self.assertNotIn('39',ids)
        self.assertIsNotNone(self.store.read('signal','0'))
        with self.store.transaction() as db:
            self.service.require_signal(db,'0')  # Old unprocessed authority does not expire.
        first_part=context(self.service,{**data,'ids':['0','1']})
        self.assertEqual(len(first_part['items']),2)
        one=self.service.review_complete({**cp,'ids':['0']},require_window=True)
        self.assertEqual(self.store.read('cursor','memory-upkeep',0),0)  # Same timestamp tie is unfinished.
        self.service.review_complete({**cp,'ids':['1']},require_window=True)
        self.assertEqual(self.store.read('cursor','memory-upkeep'),self.now-100)
        changed=self.store.read('signal','1');changed['text']='A new correction';self.store.write('signal','1',changed)
        with self.assertRaises(DomainError):context(self.service,data)

    def test_errors_name_valid_sections_and_followups_cap_their_own_limit(self):
        with self.assertRaises(DomainError) as caught:
            context(self.service, {'section': 'preferences'})
        self.assertIn('notice', str(caught.exception));self.assertIn("kind:'preferences'", str(caught.exception))
        self.assertIn('items', context(self.service, {'section': 'followups', 'limit': 100}))

    def test_tool_recipes_are_not_mistaken_for_credentials(self):
        from hermes_muse.views import validate_tools_note
        validate_tools_note('- Inventory/authorization: `mcp__apple_pim__apple_pim(action="status")`')
        for secret in ('Authorization: Bearer abc123', 'authorization=token abc', 'api_key: abc123'):
            with self.subTest(secret=secret), self.assertRaises(DomainError):
                validate_tools_note(secret)

    def test_history_append_keeps_prefix_edit_does_not_and_dense_messages_fit(self):
        def state(texts):
            ms=[{'id':i,'text':t,'role':'user','at':self.now} for i,t in enumerate(texts)]
            return {'available':True,'session_id':'s','messages':ms,'revision':digest(json.dumps(ms)),'since':self.now-86400}
        s=state(['x'*20000,'y'*20000]);d=descriptor(self.store,s,self.now)
        p=page(self.store,s,{'revision':d['revision'],'offset':0},self.now)
        grown=state(['x'*20000,'y'*20000,'new correction'])
        p=page(self.store,grown,{'revision':s['revision'],'offset':12000},self.now)
        self.assertFalse(p['available']);self.assertEqual(p['next_offset'],40014-REVIEW_CHARS)  # Retained prefix plus review window.
        offset=p['next_offset']
        while offset is not None:
            p=page(self.store,grown,{'revision':grown['revision'],'offset':offset},self.now);offset=p['next_offset']
        self.assertTrue(p['complete'])
        changed=state(['EDIT'+'x'*20000,'y'*20000,'new correction'])
        self.assertEqual(descriptor(self.store,changed,self.now)['next_offset'],40018-REVIEW_CHARS)
        dense=state(['a']*15000)
        p=page(self.store,dense,{'revision':dense['revision'],'offset':0},self.now)
        self.assertLess(len(json.dumps(p).encode()),256*1024);self.assertLess(p['next_offset'],12000)

    def test_truncated_signal_requires_original_and_detects_edits(self):
        original = {'id':1,'role':'user','at':self.now,'text':'x'*45000}
        self.service.host.conversation=lambda sid,since: {'available':True,'messages':[original]}
        self.service.signal('long','s',original['text'])
        self.assertTrue(self.store.read('signal','long')['truncated'])
        first=context(self.service,{'section':'signals','job':'memory-upkeep'})
        data={'section':'signals','job':'memory-upkeep','window':first['window'],'id':'long','length':6000}
        part=context(self.service,data)
        cp={'job':'memory-upkeep','window':first['window'],'through':first['through'],'up_to':first['through'],'summary':'Reviewed original'}
        with self.assertRaises(DomainError):self.service.review_complete(cp,require_window=True)
        while part['next_offset'] is not None:
            part=context(self.service,{**data,'offset':part['next_offset']})
        original['text']+='edited suffix'
        with self.assertRaises(DomainError):self.service.review_complete(cp,require_window=True)
        self.service.host.conversation=lambda sid,since: {'available':True,'messages':[]}
        with self.assertRaises(DomainError):context(self.service,data)
        self.assertEqual(self.store.read('cursor','memory-upkeep',0),0)

    def test_native_long_signal_pages_compacted_copies_without_scanning_later_history(self):
        from hermes_muse.host import HermesHost
        original = "user attachment\n" + "evidence " * 52000
        path = self.store.home / 'state.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,chat_type TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
                    timestamp REAL,message_uid TEXT,display_kind TEXT,tool_calls TEXT,active INTEGER,compacted INTEGER);''')
            db.execute("INSERT INTO sessions VALUES('s','private')")
            db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)', [
                (1, 's', 'user', original, self.now, 'original', None, None, 0, 1),
                (2, 's', 'user', original, self.now, 'original', None, None, 1, 0),
                (3, 's', 'user', original, self.now + 3600, 'later', None, None, 1, 0)])
        self.service.host = HermesHost(self.store)
        self.service.host.conversation = lambda *a: self.fail('Long signals must not load later conversation history')
        self.service.signal('long', 's', original)
        first = context(self.service, {'section': 'signals', 'job': 'memory-upkeep'})
        args = {'section': 'signals', 'job': 'memory-upkeep', 'window': first['window'], 'id': 'long'}
        pieces, offset = [], 0
        while offset is not None:
            part = context(self.service, {**args, 'offset': offset})
            pieces.append(part['body']); offset = part['next_offset']
        self.assertEqual(''.join(pieces), original)
        checkpoint = {'job': 'memory-upkeep', 'window': first['window'], 'through': first['through'],
                      'up_to': first['through'], 'summary': 'Read the full original attachment'}
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (4, 's', 'user', original, self.now, 'distinct-turn', None, None, 1, 0))
        with self.assertRaisesRegex(DomainError, 'missing/ambiguous'):
            self.service.review_complete(checkpoint, require_window=True)
        self.assertEqual(self.store.read('cursor', 'memory-upkeep', 0), 0)

    def test_large_metadata_and_discovery_are_consumable(self):
        self.store.write('run','huge',{'id':'huge','job':'memory-upkeep','started':self.now,'status':'partial','summary':'x'*500000,'stages':{}})
        view=Views(self.store.home);p=view.read({'kind':'run','id':'huge'})
        self.assertFalse(p['metadata_complete']);ref=p['metadata_ref']['data'];off=0;body=''
        while off is not None:
            p=view.read({**ref,'offset':off});body+=p['body'];off=p['next_offset']
        self.assertEqual(len(json.loads(body)['summary']),500000)
        self.assertLess(len(json.dumps(context(self.service,{})).encode()),12000)
        self.assertIn('operation_review', context(self.service,{}))
        self.assertEqual(context(self.service,{})['task_runs'][0]['content_ref']['data']['id'], 'huge')
        self.store.save_goal({'id':'large-goal','title':'Goal','status':'active','progress':['x'*50000],
                              'revision':1,'owner':{'kind':'native_delegation','id':'native-owner'}}, '# Goal')
        projected=view.read({'kind':'goal','id':'large-goal'})
        self.assertFalse(projected['metadata_complete'])
        meta='';off=0
        while off is not None:
            part=view.read({**projected['metadata_ref']['data'],'offset':off});meta+=part['body'];off=part['next_offset']
        self.assertEqual(json.loads(meta)['owner']['id'],'native-owner')
