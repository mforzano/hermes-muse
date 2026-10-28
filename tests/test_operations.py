from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_muse.operations import Operations, failed_result
from hermes_muse.store import Store, stamp


class OperationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = Store(self.home)
        self.now = 1800000000.0
        self.host = SimpleNamespace(manifest=lambda: {"jobs": {"owned": {}}})
        self.ops = Operations(self.store, self.host, lambda: self.now)
        self.db = sqlite3.connect(self.home / 'state.db')
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE sessions (id TEXT PRIMARY KEY,parent_session_id TEXT,chat_type TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,timestamp REAL);''')
        (self.home / 'cron').mkdir()
        with closing(sqlite3.connect(self.home / 'cron/executions.db')) as d, d:
            d.execute('CREATE TABLE executions (id TEXT,job_id TEXT,status TEXT,claimed_at TEXT,finished_at TEXT,error TEXT,delivery_outcome TEXT)')
        self.session('main')
        self.store.write('session', 'main', {'id': 'main'})

    def session(self, sid, parent=None, kind='private'):
        self.db.execute('INSERT INTO sessions VALUES(?,?,?)', (sid, parent, kind))
        self.db.commit()

    def message(self, sid='main', role='assistant', text=None, tool_calls=None, call_id=None, at=None):
        row = self.db.execute('INSERT INTO messages(session_id,role,content,tool_calls,tool_call_id,tool_name,timestamp) VALUES(?,?,?,?,?,?,?)',
                             (sid, role, text, json.dumps(tool_calls) if tool_calls else None, call_id, 'example', at or self.now - 10))
        self.db.commit()
        return row.lastrowid

    def call(self, key='write1', sid='main', name='write_file', args=None):
        return self.message(sid, tool_calls=[{'id': key, 'function': {'name': name, 'arguments': json.dumps(args or {'path': 'goal.md'})}}])

    def finish(self, page):
        return self.ops.complete({'token': page['token'], 'summary': 'Compared actual intent, receipts and state; saved relevant findings.'})


class OperationTests(OperationFixture):
    def test_complete_root_status_survives_large_bodies_without_quoted_failures(self):
        body = 'Reference examples: error, failed, denied. ' * 200
        for result in ({'ok': True, 'result': body}, {'success': True, 'content': body},
                       {'parameters': {'description': body}}, {'result': {'ok': False, 'error': body}},
                       {'status': [], 'error': {}}):
            with self.subTest(result_keys=list(result)):
                self.assertFalse(failed_result(json.dumps(result)))
        for flag in ({'ok': False}, {'success': False}, {'isError': True}, {'error': 'denied'},
                     {'status': 'failed'}, {'status': 'unknown'}, {'status': 'outcome_unknown'},
                     {'code': 'outcome_unknown'}):
            with self.subTest(flag=flag):
                text = json.dumps({'body': body, **flag})
                self.assertFalse(failed_result(text[:2000]))
                self.assertTrue(failed_result(text))
        self.assertFalse(failed_result(body))
        self.assertFalse(failed_result(json.dumps(body)))

    def test_successful_inspection_echo_does_not_reopen_but_late_root_failure_does(self):
        self.message(role='user', text='Inspect this failed fixture operation')
        self.call('first', name='tool_describe', args={'name': 'fixture'})
        self.message(role='tool', text='{"ok":false,"error":"fixture failed"}', call_id='first')
        case = self.ops.read({})['events'][0]
        self.ops.complete({'token': case['receipt'], 'summary': 'Inspected the original failure'})
        refs = self.store.read('operation_snapshot', case['receipt'])['refs']
        body = 'Quoted error failed denied, not a current failure. ' * 200
        for key, name, args, result in [
            ('skill', 'skill_view', {}, {'success': True, 'content': body}),
            ('schema', 'tool_describe', {}, {'parameters': {'description': body}}),
            ('echo', 'muse_manage', {'action': 'operations_evidence'}, {'ok': True, 'result': {'text': body}}),
        ]:
            self.call(key, name=name, args=args)
            self.message(role='tool', text=json.dumps(result), call_id=key)
        self.assertFalse(self.ops.read({})['events'])
        snapshot = self.store.read('operation_snapshot', case['receipt'])
        self.assertTrue(snapshot['reviewed'])
        self.assertEqual(snapshot['refs'], refs)
        self.call('late-failure', name='tool_describe', args={'name': 'fixture'})
        failure = self.message(role='tool', text=json.dumps({'body': body, 'status': 'unknown'}), call_id='late-failure')
        reopened = self.ops.read({})['events'][0]
        self.assertEqual(reopened['receipt'], case['receipt'])
        self.assertIn('message:' + str(failure), reopened['evidence_refs'])
        self.assertIn('reported_failure_or_unknown', reopened['reasons'])
        self.assertFalse(self.store.read('operation_snapshot', case['receipt'])['reviewed'])
        self.assertNotIn(body, json.dumps(reopened))

    def test_consequences_pair_context_and_claims_without_deep_successful_reads(self):
        user=self.message(role='user',text='Please update the report')
        self.call('read',name='read_file');self.message(role='tool',text='{"ok":true}',call_id='read')
        write=self.call('write');result=self.message(role='tool',text='{"ok":false,"error":"disk full"}',call_id='write')
        claim=self.message(text='I saved the report')
        page=self.ops.read({});self.assertEqual(len(page['events']),1)
        case=page['events'][0];self.assertIn('reported_failure_or_unknown',case['reasons'])
        self.assertEqual(set(case['evidence_refs']),{'message:'+str(i) for i in (user,write,result,claim)})
        body=self.ops.evidence({'receipt':case['receipt'],'ref':'message:'+str(result)})
        self.assertNotIn('Please update',body['text'])
        context=self.ops.evidence({'receipt':case['receipt'],'ref':'message:'+str(user)})
        self.assertIn('Please update',context['text'])
        self.assertEqual(self.ops.read({})['pending_cases'],1)
        self.ops.complete({'token':case['receipt'],'summary':'Actual failed write and unsupported completion remain as a finding'})
        self.assertFalse(self.ops.read({})['events'])

    def test_index_reads_only_current_page_and_never_copies_context_or_reasoning(self):
        self.message(role='user',text='PRIVATE original intent')
        for n in range(8):self.call(str(n))
        self.message(text='PRIVATE hidden reasoning',tool_calls=[{'id':'x','function':{'name':'edit_file','arguments':'{}'}}])
        case=self.ops.read({})['events'][0];queries=[]
        from unittest.mock import patch
        from hermes_muse.operations import reader
        from contextlib import contextmanager
        @contextmanager
        def traced(path):
            with reader(path) as db:
                db.set_trace_callback(queries.append);yield db
        before=(self.home/'muse/state.db').read_bytes()
        with patch('hermes_muse.operations.reader',traced):
            index=self.ops.evidence({'receipt':case['receipt'],'offset':2,'limit':2})
        self.assertEqual(len(index['events']),2)
        self.assertEqual(sum(q.startswith('SELECT') for q in queries),2)
        self.assertFalse(any('content' in q or 'tool_calls' in q for q in queries))
        self.assertEqual(before,(self.home/'muse/state.db').read_bytes())
        ref=next(r for r in case['evidence_refs'] if r!='message:1')
        self.assertNotIn('PRIVATE',self.ops.evidence({'receipt':case['receipt'],'ref':ref})['text'])
        with self.assertRaises(ValueError):self.ops.evidence({'receipt':case['receipt'],'ref':'message:999'})

    def test_retirement_keeps_old_cursor_owners_and_open_findings_unreviewed(self):
        self.store.write('meta','operation_cursor',{'message_id':2,'through':900,'since':self.now-100})
        old={'number':339,'pages':[{'receipt':'old','reviewed':False}]}
        self.store.write('meta','operation_wave',old)
        self.store.write('operation_dispatch','old',{'state':'unknown'})
        self.store.write('operation_finding','important',{'id':'important','status':'open','summary':'Unverified external write'})
        with self.store.transaction() as db:self.store.delete(db,'meta','operation_policy')
        self.call('old')
        policy=self.ops.policy()
        self.assertEqual(policy['legacy']['wave'],old);self.assertFalse(policy['legacy']['reviewed'])
        self.assertEqual(self.store.read('meta','operation_cursor')['message_id'],2)
        self.assertFalse(self.ops.read({})['events'])
        self.assertEqual(self.ops.summary()['findings'][0]['status'],'open')
        with self.assertRaises(ValueError):self.ops.complete({'token':'old','summary':'Not reviewed'})
        self.call('new');self.assertTrue(self.ops.read({})['events'])

    def test_profile_group_and_opaque_sample_are_explicit_not_safe_classification(self):
        self.session('group','main','group');self.call('secret',sid='group')
        for n in range(6):self.call(str(n),name='terminal',args={'command':'opaque '+str(n)})
        page=self.ops.read({});self.assertEqual(page['opaque_samples'],2)
        self.assertFalse(any(r['session_id']=='group' for r in page['events']))
        self.assertIn('side effects may be missed',page['checkpoint']['limits'])
        with tempfile.TemporaryDirectory() as other:
            self.assertFalse(Operations(Store(other),self.host,lambda:self.now).read({})['available'])

    def test_missing_evidence_does_not_complete_and_finding_closure_stays_truthful(self):
        call=self.call();case=self.ops.read({})['events'][0];ref='message:'+str(call)
        finding=self.ops.finding({'evidence':[ref],'summary':'Write needs verification','impact':'Result not checked'})
        self.assertEqual(finding['id'],self.ops.finding({'evidence':[ref],'summary':'Write needs verification','impact':'Result not checked'})['id'])
        with self.assertRaises(ValueError):self.ops.finding({'id':finding['id'],'status':'resolved'})
        self.db.execute('DELETE FROM messages WHERE id=?',(call,));self.db.commit()
        with self.assertRaises(ValueError):self.ops.complete({'token':case['receipt'],'summary':'Not verifiable'})
        self.assertFalse(self.store.read('operation_snapshot',case['receipt'])['reviewed'])

    def test_uid_copies_do_not_hide_changed_failures_or_new_calls(self):
        self.db.execute('ALTER TABLE messages ADD COLUMN message_uid TEXT')
        first=self.call();self.db.execute('UPDATE messages SET message_uid=? WHERE id=?',('same',first));self.db.commit()
        case=self.ops.read({})['events'][0];self.ops.complete({'token':case['receipt'],'summary':'Verified write'})
        self.db.execute('INSERT INTO messages(session_id,role,content,tool_calls,tool_call_id,tool_name,timestamp,message_uid) SELECT session_id,role,content,tool_calls,tool_call_id,tool_name,timestamp,message_uid FROM messages WHERE id=?',(first,));self.db.commit()
        self.assertFalse(self.ops.read({})['events'])
        changed=self.call('changed',args={'path':'new-target'});self.db.execute('UPDATE messages SET message_uid=? WHERE id=?',('same',changed));self.db.commit()
        self.assertTrue(self.ops.read({})['events'])
