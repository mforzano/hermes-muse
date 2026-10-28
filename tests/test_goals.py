import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_companion import prepare, Host
from hermes_muse.service import Companion
from hermes_muse.followup import context, snapshot, owner_status
from hermes_muse.migration import migrate, rollback, ARCHIVE
from hermes_muse.views import Views
from hermes_muse.content_io import DomainError


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name).resolve();self.store=prepare(self.home)
        self.now=1800000000;self.service=Companion(self.store,Host(),lambda:self.now)

    def goal(self,key='g'):
        return self.service.goal_create({'id':key,'title':key,'description':'Accepted work','completion_criteria':'Explain result'},session_id='parent')

    def test_one_status_done_and_cancelled_never_return_due(self):
        g=self.goal();self.service.goal_update({'id':'g','next_check_at':self.now-1})
        self.assertEqual(snapshot(self.home,now=self.now)['due_total'],1)
        self.service.signal('new','parent','A new unrelated question')
        done=self.service.goal_update({'id':'g','status':'done','note':'Result checked and told to the user'})
        self.assertEqual(done['status'],'done');self.assertNotIn('followup',done)
        self.assertEqual(snapshot(self.home,now=self.now+99999)['due_total'],0)
        self.goal('cancel');self.service.goal_update({'id':'cancel','status':'cancelled'})
        self.assertEqual(snapshot(self.home,now=self.now+99999)['due_total'],0)
        restarted=self.service.goal_update({'id':'cancel','status':'active','note':'User explicitly restarted tracking'})
        self.assertEqual(restarted['status'],'active')

    def test_optional_integer_revision_conflict_returns_current_goal(self):
        self.goal();current=self.service.goal_update({'id':'g','note':'Progress'})
        with self.assertRaises(DomainError) as caught:self.service.goal_update({'id':'g','expected_revision':1,'status':'done','note':'Stale'})
        self.assertEqual(caught.exception.details['current'],current)
        result=self.service.goal_update({'id':'g','expected_revision':current['revision'],'status':'done','note':'Done and explained'})
        self.assertEqual(result['revision'],current['revision']+1)

    def test_committed_goal_returns_warning_when_watch_cleanup_fails(self):
        self.goal()
        with patch.object(self.service.host,'pause_owner',side_effect=OSError('Synthetic Cron failure')):
            result=self.service.goal_update({'id':'g','status':'done','note':'Result explained','expected_revision':1})
        self.assertEqual(result['status'],'done');self.assertEqual(result['revision'],2)
        self.assertIn('Goal committed; watch cleanup failed',result['warning'])
        self.assertIn('do not repeat goal completion',result['warning'])
        stored=self.store.goal('g')[0]
        self.assertEqual(stored['status'],'done');self.assertNotIn('warning',stored)

    def test_concurrent_goal_revision_has_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        self.goal()
        def update(note):
            try:
                return self.service.goal_update({'id':'g','expected_revision':1,'note':note})['revision']
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(update,['First writer','Second writer']))
        self.assertCountEqual(results,[2,'revision_conflict'])
        self.assertEqual(len(self.store.goal('g')[0]['progress']),1)

    def test_owner_status_missing_interrupted_failed_and_corrupt_never_complete_goal(self):
        self.goal()
        handle={'kind':'native_delegation','id':'deleg_fixture'}
        self.service.goal_update({'id':'g','owner':handle,'next_check_at':self.now})
        self.assertEqual(owner_status(self.home,handle),'unknown')
        manifest=self.home/'cache/delegation/live/deleg_fixture/manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'delegation_id':'deleg_fixture','completed':True,'tasks':[{'status':'completed'}]}))
        with closing(sqlite3.connect(self.home/'state.db')) as db, db:
            db.execute('CREATE TABLE async_delegations(delegation_id TEXT,state TEXT,result_json TEXT)')
            db.execute("INSERT INTO async_delegations VALUES('deleg_fixture','completed','broken')")
        for result,expected in [('broken','unknown'),('[]','unknown'),('{"results":[]}','unknown'),
                                ('{"results":[{"status":"interrupted"}]}','interrupted'),
                                ('{"results":[{"status":"failed"}]}','failed'),
                                ('{"results":[{"status":"completed"}]}','completed')]:
            with self.subTest(result=result):
                with closing(sqlite3.connect(self.home/'state.db')) as db, db:
                    db.execute('UPDATE async_delegations SET result_json=?',(result,))
                self.assertEqual(owner_status(self.home,handle),expected)
                self.assertEqual(snapshot(self.home,now=self.now)['due_total'],1)
                self.assertEqual(self.store.goal('g')[0]['status'],'active')

    def test_old_signal_allowed_summary_small_and_owner_unknown_never_dispatches(self):
        self.service.signal('u','parent','Accepted work');self.now+=10*86400
        self.service.goal_create({'id':'g','title':'Fixture','description':'Work','completion_criteria':'Explain','signal_id':'u'})
        self.service.goal_update({'id':'g','owner':{'kind':'external','id':'https://example.test/job/unknown'},'next_check_at':self.now})
        row=snapshot(self.home,now=self.now)['items'][0];self.assertEqual(row['owner_status'],'unknown')
        self.assertLess(len(context(self.home,now=self.now).encode()),500)
        self.service.goal_update({'id':'g','next_check_at':self.now+3600})
        self.assertEqual(context(self.home,now=self.now),'')

    def test_due_pages_allow_done_between_pages_without_skipping(self):
        for i in range(33):self.goal(f'g{i:02}');self.service.goal_update({'id':f'g{i:02}','next_check_at':self.now})
        ids=[];cursor=''
        while True:
            page=snapshot(self.home,now=self.now,cursor=cursor,limit=7);ids += [g['id'] for g in page['items']]
            for g in page['items']:self.service.goal_update({'id':g['id'],'status':'done','note':'Result explained'})
            if not page['next_cursor']:break
            cursor=page['next_cursor']
        self.assertEqual(len(ids),33);self.assertEqual(len(set(ids)),33)

    def test_due_pages_fit_budget_without_truncating_owner_or_blocker(self):
        for i in range(50):
            self.goal(f'g{i:02}')
            self.service.goal_update({'id':f'g{i:02}','owner':{'kind':'external','id':'例'*2000},'blocked':'障'*2000,'next_check_at':self.now})
        ids=[];cursor=''
        while True:
            page=snapshot(self.home,now=self.now,cursor=cursor,limit=50)
            self.assertLessEqual(len(json.dumps({'ok':True,'result':page},ensure_ascii=False).encode()),262144)
            self.assertTrue(all(g['owner']['id']=='例'*2000 and g['blocked']=='障'*2000 for g in page['items']))
            ids += [g['id'] for g in page['items']]
            if not page['next_cursor']:break
            cursor=page['next_cursor']
        self.assertEqual(len(ids),50);self.assertEqual(len(set(ids)),50)

    def legacy(self,key,status):
        goal={'id':key,'title':key,'status':status,'created':self.now-100,'updated':self.now-10,'session_id':'parent','progress':[{'at':self.now-10,'text':'Keep this progress'}],
              'followup':{'execution_state':'outcome_unknown','result_state':'verified','delivery_state':'unknown','owner':{'kind':'native_delegation','id':'deleg_old'},'next_check_at':self.now-1}}
        self.store.save_goal(goal,'# Original body\nKeep links and evidence.\n')
        return self.store.path(f'workspace/goals/{key}/GOAL.md').read_bytes()

    def test_migration_archive_mapping_counts_and_exact_rollback(self):
        originals={k:self.legacy(k,s) for k,s in [('a','active'),('d','completed'),('c','cancelled')]}
        receipt={'id':'receipt','terminal':False};self.store.write('followup_receipt','receipt',receipt)
        self.store.write('followup_repair','repair',{'id':'repair','before':{'private':'preserved'}})
        result=migrate(self.store);self.assertEqual(result['goals'],3)
        self.assertEqual({g['id']:g['status'] for g in self.store.goals()},{'a':'active','d':'done','c':'cancelled'})
        self.assertEqual(snapshot(self.home,now=self.now)['due_total'],1)
        self.assertEqual(self.store.goal('a')[0]['progress'][0]['text'],'Keep this progress')
        self.assertEqual(self.store.goal('a')[0]['owner']['id'],'deleg_old')
        self.assertFalse(self.store.all('followup_receipt'));self.assertFalse(self.store.all('followup_repair'))
        self.assertFalse(migrate(self.store)['migrated'])
        rollback(self.store)
        for k,raw in originals.items():self.assertEqual(self.store.path(f'workspace/goals/{k}/GOAL.md').read_bytes(),raw)
        self.assertEqual(self.store.read('followup_receipt','receipt'),receipt)

    def test_migration_resume_after_file_write_and_rollback_refuses_new_work(self):
        self.legacy('g','completed');write=self.store.write_text
        def crash(path,*args,**kwargs):
            if path==ARCHIVE+'/manifest.json' and self.store.goal('g')[0]['status']=='done':raise RuntimeError('crash after goal file')
            return write(path,*args,**kwargs)
        with patch.object(self.store,'write_text',side_effect=crash):
            with self.assertRaises(RuntimeError):migrate(self.store)
        migrate(self.store);self.assertEqual(self.store.goal('g')[0]['status'],'done')
        self.service.goal_update({'id':'g','note':'New activity'})
        with self.assertRaises(ValueError):rollback(self.store)

    def test_sixty_goals_migrate_rollback_upgrade_and_new_downgraded_work(self):
        originals={f'g{i:02}':self.legacy(f'g{i:02}', 'active' if i<26 else 'completed' if i<52 else 'cancelled') for i in range(60)}
        receipt={'id':'receipt','terminal':False};self.store.write('followup_receipt','receipt',receipt)
        migrate(self.store);rollback(self.store)
        self.assertTrue(json.loads(self.store.path(ARCHIVE+'/manifest.json').read_text())['rolled_back_at'])
        self.assertTrue(all(self.store.path(f'workspace/goals/{k}/GOAL.md').read_bytes()==raw for k,raw in originals.items()))
        # A downgraded version can save new progress and new receipts before upgrading again.
        goal,body=self.store.goal('g00');goal['progress'].append({'at':self.now,'text':'New downgraded work'})
        self.store.save_goal(goal,body);latest=self.store.path('workspace/goals/g00/GOAL.md').read_bytes()
        self.store.write('followup_receipt','new',{'id':'new'})
        second=migrate(self.store)
        self.assertEqual(second['goals'],60);self.assertEqual(second['archived_records'],2)
        self.assertTrue(all(type(g['revision']) is int and 'followup' not in g for g in self.store.goals()))
        self.assertEqual({s:sum(g['status']==s for g in self.store.goals()) for s in ('active','done','cancelled')},{'active':26,'done':26,'cancelled':8})
        self.assertEqual(snapshot(self.home,now=self.now)['due_total'],26)
        closed=list(self.store.path('archive').glob('followup-0.2.x-*'));self.assertEqual(len(closed),1)
        self.assertEqual((closed[0]/'goals/g00.md').read_bytes(),originals['g00'])
        rollback(self.store)
        self.assertEqual(self.store.path('workspace/goals/g00/GOAL.md').read_bytes(),latest)
        self.assertEqual(self.store.read('followup_receipt','receipt'),receipt)
        self.assertEqual(self.store.read('followup_receipt','new'),{'id':'new'})
        migrate(self.store)
        updated=self.service.goal_update({'id':'g00','status':'done','expected_revision':1,'note':'Result checked and explained'})
        self.assertEqual(updated['revision'],2);self.assertEqual(updated['progress'][1]['text'],'New downgraded work')

    def test_unmarked_prior_rollback_reconverts_before_and_refuses_unexpected_file(self):
        raw=self.legacy('g','completed');migrate(self.store)
        # Simulate a rollback produced by 722a82f, which retained the after manifest.
        self.store.path('workspace/goals/g/GOAL.md').write_bytes(raw)
        with self.store.transaction() as db:self.store.delete(db,'meta','goal_schema')
        migrate(self.store)
        self.assertEqual(self.store.goal('g')[0]['status'],'done')
        self.assertEqual(self.store.goal('g')[0]['revision'],1)
        with self.store.transaction() as db:self.store.delete(db,'meta','goal_schema')
        changed=raw+b'Unexpected concurrent edit\n';self.store.path('workspace/goals/g/GOAL.md').write_bytes(changed)
        with self.assertRaises(ValueError):migrate(self.store)
        self.assertEqual(self.store.path('workspace/goals/g/GOAL.md').read_bytes(),changed)

    def test_pure_views_never_initialize_or_migrate(self):
        missing=self.home/'missing'
        with self.assertRaises(DomainError):snapshot(missing)
        self.assertFalse(missing.exists())
        self.legacy('old','completed');before=self.store.path('workspace/goals/old/GOAL.md').read_bytes()
        Views(self.home).read({'kind':'goal','id':'old'})
        self.assertEqual(self.store.path('workspace/goals/old/GOAL.md').read_bytes(),before)
        self.assertFalse(self.store.path(ARCHIVE).exists())

    def test_memory_feedback_current_reply_needs_no_followup_ledger(self):
        from hermes_muse.memory_feedback import MemoryFeedback
        self.service.signal('remember','parent','Remember this preference')
        ledger=MemoryFeedback(self.store,lambda:self.now)
        ledger.capture('memory',{'action':'add','content':'PRIVATE TEST FACT'},{'success':True},session_id='parent',turn_id='remember',tool_call_id='m')
        receipt=self.store.all('memory_feedback')[0]
        ledger.review({'receipt_id':receipt['id'],'expected_revision':receipt['revision'],'request_id':'inform','decision':'inform','reason':'Native save succeeded'},'parent','remember')
        with closing(sqlite3.connect(self.home/'state.db')) as db, db:
            db.execute('CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,tool_calls TEXT,display_kind TEXT)')
            db.execute("INSERT INTO messages VALUES(1,'parent','assistant','已记住。',?,NULL,NULL)",(self.now+1,))
        ledger.finish('parent','remember','已记住。')
        row=self.store.all('memory_feedback')[0]
        self.assertEqual(row['reply_ref'],{'session_id':'parent','message_id':1})
        self.assertEqual(row['reply_state'],'recorded_unconfirmed')
        self.assertNotIn('PRIVATE TEST FACT',json.dumps(row))
        self.assertFalse(self.store.all('followup_reply'))
