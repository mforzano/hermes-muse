from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from hermes_muse.runs import Runs
from hermes_muse.store import Store, stamp


class RunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.store = Store(self.home)
        self.now = 1800000000.0
        self.jobs = {'hourly': {'key': 'memory-upkeep'}, 'night': {'key': 'nightly-review'}, 'patrol': {'key': 'proactive-watch'}}
        host = SimpleNamespace(manifest=lambda: {'jobs': self.jobs}, owned_job=lambda key:self.jobs.get(key), local_now=lambda t: datetime.fromtimestamp(t, timezone.utc))
        self.runs = Runs(self.store, host, lambda: self.now)
        (self.home/'cron').mkdir()
        self.path = self.home/'cron/executions.db'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE executions(id TEXT, job_id TEXT, status TEXT, started_at TEXT, claimed_at TEXT, error TEXT)')

    def start(self, job):
        execution = job + '-execution'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)', (execution, job, 'running', stamp(self.now), stamp(self.now), None))
        return self.runs.bind('cron:' + job + ':' + execution)

    def test_native_success_without_task_completion_is_partial(self):
        run = self.start('hourly')
        self.runs.stage(run, 'review', {'complete': True})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE executions SET status='completed'")
        self.assertEqual('partial', self.runs.recent()[0]['status'])
        with self.assertRaisesRegex(ValueError, 'Operation review'):
            self.runs.finish(run, {'outcome': 'complete', 'summary': 'Signals handled'})

    def test_resume_operation_review_then_finalize_without_delivery_claim(self):
        run = self.start('hourly')
        self.runs.stage(run, 'review', {'complete': True})
        partial = self.runs.finish(run, {'outcome': 'partial', 'summary': 'Signals saved', 'gaps': ['Operation pages remain; resume saved checkpoint']})
        self.assertEqual('partial', partial['status'])
        self.runs.stage(run, 'operations', {'complete': True})
        ready = self.runs.finish(run, {'outcome': 'complete', 'summary': 'Snapshot now reviewed'})
        self.assertEqual('ready', ready['status'])
        self.runs.finalize(run)
        result = self.runs.recent()[0]
        self.assertEqual('complete', result['status'])
        self.assertNotIn('delivered', result)

    def test_nightly_cannot_finish_without_fresh_artifacts(self):
        run = self.start('night')
        self.runs.stage(run, 'review', {'complete': True})
        day = datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()
        paths = [f'dreams/{day}.md', 'dreams/alignment/derived/ALIGNMENT_SYNTHESIS.md']
        for name in paths:
            self.store.write_text(name, 'Verified learning')
            os.utime(self.store.path(name), (self.now - 60, self.now - 60))
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            self.runs.finish(run, {'outcome': 'complete', 'summary': 'Done'})
        for name in paths:
            os.utime(self.store.path(name), (self.now + 1, self.now + 1))
        ready = self.runs.finish(run, {'outcome': 'complete', 'summary': 'Confirmed alignment written; historical audit remains with hourly upkeep'})
        self.assertIn('artifacts', ready['stages'])

    def test_patrol_does_not_require_historical_audit(self):
        run = self.start('patrol')
        self.runs.stage(run, 'review', {'complete': True})
        self.runs.finish(run, {'outcome': 'complete', 'summary': 'Sources checked; no worthwhile notice'})
        self.runs.finalize(run, notifications=True)
        self.assertEqual('complete', self.runs.recent()[0]['status'])
        self.assertIsNone(self.runs.bind('cron:foreign:patrol-execution'))
        with self.assertRaises(ValueError):
            self.runs.finish(None, {'outcome': 'complete', 'summary': 'Forged'})

    def test_last_complete_remains_visible_after_new_partial_attempt(self):
        self.store.write('run', 'earlier', {'id': 'earlier', 'job_id': 'hourly', 'job': 'memory-upkeep',
            'started': self.now - 3600, 'status': 'complete', 'stages': {'review': {'complete': True}}})
        run = self.start('hourly')
        self.runs.finalize(run)
        self.assertEqual({'earlier', run['id']}, {row['id'] for row in self.runs.recent()})
        self.assertEqual('partial', self.store.read('run', run['id'])['status'])

    def test_new_attempt_does_not_hide_previous_partial_work(self):
        run = self.start('hourly')
        self.runs.finalize(run)
        self.now += 3600
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)', ('next', 'hourly', 'running', stamp(self.now), stamp(self.now), None))
        self.runs.bind('cron:hourly:next')
        rows = self.runs.recent()
        self.assertEqual({'running', 'partial'}, {row['status'] for row in rows})
