"""Synthetic connector receipts through Companion, without calendar/cron APIs."""
import copy
import contextlib
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch

from hermes_muse.calendar import assemble, window
from hermes_muse.host_v1 import JOBS
from hermes_muse.service import Companion, DEFAULT_PREFERENCES
from hermes_muse.runtime import Runtime, SCHEMA
from test_companion import Host, prepare


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = prepare(self.temp.name)
        self.now = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo('America/New_York'))
        self.host = Host()
        self.host.local_now = lambda timestamp: datetime.fromtimestamp(timestamp, self.now.tzinfo)
        self.service = Companion(self.store, self.host, lambda: self.now.timestamp())
        self.query = self.service.calendar_window({})

    def page(self, items, **kwargs):
        return {'status': 'checked', 'observed_at': self.now.isoformat(), 'items': items, **kwargs}

    def event(self, eid='event', start='2026-10-20T10:00:00-04:00', end='2026-10-20T11:00:00-04:00', **kwargs):
        return {'id': eid, 'title': 'Original literal title', 'start': start, 'end': end,
                'all_day': False, 'recurring': False, **kwargs}

    def source(self, events=(), connection='apple', account='fixture', resources=None):
        resources = resources if resources is not None else [{'id': 'work', 'title': 'Work calendar', 'timezone': 'America/New_York'}]
        return {'source': {'connection': connection, 'account': account, 'resource': self.query['source_resource']},
                'title': connection + ' synthetic', 'inventory': {'pages': [self.page(resources)]},
                'calendars': [{'resource_id': resource['id'], 'window': self.query,
                               'pages': [self.page(list(events))]} for resource in resources]}

    def view(self, *sources, **kwargs):
        return self.service.calendar_view({'window': self.query, 'discovery_complete': True,
                                           'sources': list(sources), **kwargs})

    def test_rolling_vs_natural_month_dst_and_boundaries(self):
        self.assertEqual(self.query['end'], '2026-11-11T12:00:00-05:00')
        a = datetime.fromisoformat(self.query['start']).timestamp()
        b = datetime.fromisoformat(self.query['end']).timestamp()
        self.assertEqual((b-a)/3600, 721)
        month = self.service.calendar_window({'mode': 'month'})
        self.assertEqual(month['start'], '2026-10-01T00:00:00-04:00')
        self.assertEqual(month['end'], '2026-11-01T00:00:00-04:00')
        spring = window(datetime(2026, 3, 1, 12, tzinfo=self.now.tzinfo))
        self.assertEqual((datetime.fromisoformat(spring['end']).timestamp()-datetime.fromisoformat(spring['start']).timestamp())/3600, 719)
        gap = window(datetime(2026, 2, 6, 2, 30, tzinfo=self.now.tzinfo))
        self.assertEqual(gap['end'], '2026-03-08T03:30:00-04:00')
        fold = window(datetime(2026, 10, 2, 1, 30, tzinfo=self.now.tzinfo))
        self.assertEqual(fold['end'], '2026-11-01T01:30:00-04:00')
        year_end = window(datetime(2026, 12, 15, tzinfo=timezone.utc), 'month')
        self.assertEqual(year_end['end'], '2027-01-01T00:00:00+00:00')
        edges = [self.event('left', end=self.query['start'], start='2026-10-12T10:00:00-04:00'),
                 self.event('right', start=self.query['end'], end='2026-11-11T13:00:00-05:00'),
                 self.event('ongoing', start='2026-10-12T11:00:00-04:00', end='2026-10-12T13:00:00-04:00')]
        result = self.view(self.source(edges))
        self.assertEqual([r['event']['id'] for r in result['events']], ['ongoing'])
        wrong = copy.deepcopy(self.query); wrong['end'] = '2026-10-19T12:00:00-04:00'
        with self.assertRaises(ValueError):
            assemble({'window': wrong, 'sources': []}, self.now)

    def test_fixed_offset_profile_window_roundtrip_without_utc_or_iana_guess(self):
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone(timedelta(hours=8), name='CST'))
        self.query = self.service.calendar_window({})
        self.assertEqual(self.query['timezone'], 'CST')
        self.assertEqual(self.query['timezone_kind'], 'fixed')
        self.assertEqual(self.query['utc_offset_seconds'], 28800)
        self.assertEqual(self.query['end'], '2026-11-11T12:00:00+08:00')
        self.assertIn('DST transitions unavailable', self.query['timezone_note'])
        source = self.source([self.event()])
        source['calendars'][0]['window'] = {k: v for k, v in self.query.items() if k != 'timezone_note'}
        result = self.view(source)
        self.assertTrue(result['complete'], result['gaps'])
        source['calendars'][0]['window']['end'] = '2026-10-19T12:00:00+08:00'
        self.assertFalse(self.view(source)['complete'])
        self.assertEqual(result['events'][0]['local_start'], '2026-10-20T22:00:00+08:00')
        self.query = self.service.calendar_window({'mode': 'month'})
        self.assertEqual(self.query['start'], '2026-10-01T00:00:00+08:00')
        self.assertTrue(self.view(self.source())['complete'])

    def test_all_day_cross_zone_multi_day_order_and_conflicts(self):
        events = [self.event('later', '2026-10-20T05:00:00-04:00', '2026-10-20T06:00:00-04:00'),
                  self.event('trip', '2026-10-20T17:00:00+09:00', '2026-10-22T10:00:00+09:00', timezone='Asia/Tokyo'),
                  self.event('observance', '2026-10-20', '2026-10-21', all_day=True, timezone='Asia/Tokyo'),
                  self.event('busy-day', '2026-10-21', '2026-10-22', all_day=True, busy=True)]
        result = self.view(self.source(events))
        self.assertTrue(result['complete'], result['gaps'])
        by_id = {r['event']['id']: i for i, r in enumerate(result['events'])}
        days = {d['date']: d for d in result['days']}
        self.assertIn(by_id['observance'], days['2026-10-19']['events'])
        self.assertIn(by_id['observance'], days['2026-10-20']['events'])
        self.assertNotIn(by_id['observance'], days['2026-10-20']['busy'])
        self.assertEqual(days['2026-10-20']['overlaps'], [[by_id['trip'], by_id['later']]])
        self.assertIn([by_id['trip'], by_id['busy-day']], days['2026-10-21']['overlaps'])
        self.assertEqual(result['events'][by_id['observance']]['event']['start'], '2026-10-20')
        self.assertEqual(result['events'][by_id['observance']]['all_day_dates'],
                         {'start': '2026-10-20', 'end_exclusive': '2026-10-21', 'timezone': 'Asia/Tokyo'})
        self.assertEqual(result['events'][by_id['trip']]['event']['timezone'], 'Asia/Tokyo')
        self.assertEqual(result['events'][by_id['trip']]['calendar']['title'], 'Work calendar')

    def test_conservative_uid_occurrence_identity_and_exceptions(self):
        original = self.event('copy-a', uid='one')
        events = [original, {**original, 'id': 'copy-b'}, self.event('distinct', uid='other'),
                  self.event('recur1', uid='series', recurring=True, occurrence_id='2026-10-20T10:00:00-04:00'),
                  self.event('moved', '2026-10-22T12:00:00-04:00', '2026-10-22T13:00:00-04:00',
                             uid='series', recurring=True, occurrence_id='2026-10-21T10:00:00-04:00', exception=True),
                  self.event('cancelled', '2026-10-23T10:00:00-04:00', '2026-10-23T11:00:00-04:00',
                             uid='series', recurring=True, occurrence_id='2026-10-23T10:00:00-04:00', status='cancelled')]
        result = self.view(self.source(events), self.source([original], connection='google'), self.source([original], account='other'))
        self.assertTrue(result['complete'], result['gaps'])
        self.assertEqual(len(result['events']), 7)
        merged = next(r for r in result['events'] if len(r['copies']) == 2)
        self.assertEqual(merged['copies'], ['copy-a', 'copy-b'])
        cancelled = next(i for i, r in enumerate(result['events']) if r['event']['id'] == 'cancelled')
        self.assertTrue(all(cancelled not in day['events'] for day in result['days']))
        bad = self.view(self.source([self.event(recurring=True, uid='series')]))
        self.assertFalse(bad['complete'])
        self.assertEqual(bad['coverage'][0]['source_check']['checked_resources'], [])
        conflict = self.view(self.source([original, {**original, 'status': 'cancelled'}]))
        self.assertFalse(conflict['complete'])
        self.assertEqual(len(conflict['events']), 2)
        calendars = [{'id': rid, 'title': rid, 'timezone': 'UTC'} for rid in ('work', 'home')]
        copies = self.view(self.source([original], resources=calendars))
        self.assertEqual(len(copies['events']), 2)  # Cross-calendar UID alone proves no alias.
        distinct = self.view(self.source([self.event('native-a'), self.event('native-b')]))
        self.assertEqual(len(distinct['events']), 2)  # No UID and same title/time is not identity.

    def test_patrol_day8_day30_preparation_and_no_schedule_or_calendar_writes(self):
        before = {path.relative_to(self.store.root): path.read_bytes() for path in self.store.root.rglob('*') if path.is_file()}
        events = [self.event('day8', preparation={'reason': 'Travel booking lead time', 'due': '2026-10-13'}),
                  self.event('day30', '2026-11-11T10:00:00-05:00', '2026-11-11T11:00:00-05:00', preparation={'reason': 'Prepare application'}),
                  self.event('routine', '2026-11-02T10:00:00-05:00', '2026-11-02T11:00:00-05:00')]
        result = self.view(self.source(events))
        self.assertEqual([result['events'][i]['event']['id'] for i in result['preparation_candidates']], ['day8', 'day30'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['calendar_writes'], 0)
        self.assertEqual(self.host.jobs, [])
        self.assertEqual(self.host.woken, [])
        self.assertEqual(self.store.all('notification'), [])
        self.assertEqual(JOBS['proactive-watch'], '*/30 * * * *')
        self.assertEqual(len(JOBS), 6)
        self.assertEqual(self.service.preferences(), DEFAULT_PREFERENCES)
        after = {path.relative_to(self.store.root): path.read_bytes() for path in self.store.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        # The real patrol context exposes the same executable 30-day query contract.
        self.assertEqual(self.service.context(view='proactive-watch')['calendar_window'], self.query)

    def test_inventory_and_event_pagination_partial_failure_and_watermarks(self):
        source = self.source([self.event()])
        resource = {'id': 'home', 'title': 'Home', 'timezone': 'UTC'}
        source['inventory']['pages'][0]['next_token'] = 'inventory-2'
        source['inventory']['pages'].append(self.page([resource], request_token='inventory-2'))
        read = source['calendars'][0]
        read['pages'][0]['next_token'] = 'events-2'
        read['pages'].append(self.page([self.event('day30', '2026-11-11T10:00:00-05:00', '2026-11-11T11:00:00-05:00')], request_token='events-2'))
        source['calendars'].append({'resource_id': 'home', 'window': self.query, 'pages': [self.page([])]})
        source['cursor'] = 'success'
        complete = self.view(source)
        self.assertTrue(complete['complete'], complete['gaps'])
        check = complete['coverage'][0]['source_check']
        saved = self.service.source_check(check)
        self.now += timedelta(minutes=30)
        self.query = self.service.calendar_window({})
        failed_source = copy.deepcopy(source)
        for page in failed_source['inventory']['pages']:
            page['observed_at'] = self.now.isoformat()
        for read in failed_source['calendars']:
            read['window'] = self.query
            for page in read['pages']:
                page['observed_at'] = self.now.isoformat()
        failed_source['calendars'][0]['pages'][1] = self.page([], request_token='events-2', status='error', error='synthetic timeout')
        failed_source['cursor'] = 'must-not-advance'
        failed_source['source_id'] = saved['id']
        result = self.view(failed_source, self.source([], connection='google'))
        self.assertFalse(result['complete'])
        self.assertEqual(result['coverage'][0]['source_check']['checked_resources'], ['home'])
        self.assertEqual(result['coverage'][1]['source_check']['status'], 'checked')
        failed = self.service.source_check(result['coverage'][0]['source_check'])
        for field in ('last_success', 'cursor', 'last_success_scope', 'last_success_resources', 'last_success_window', 'last_success_observed_at'):
            self.assertEqual(failed[field], saved[field], field)
        self.assertEqual(failed['query_window'], self.query)
        self.assertEqual(self.service.source_check(check), failed)  # stale concurrent completion
        self.assertEqual(result['availability'], 'observed_busy_only')

    def test_proposed_source_check_carries_current_revision_and_duplicate_explains_merge(self):
        from hermes_muse.views import Views
        first = self.view(self.source())['coverage'][0]['source_check']
        self.assertEqual(first['expected_revision'], 'absent')
        saved = self.service.source_check(first)
        again = self.view(self.source())['coverage'][0]['source_check']
        self.assertEqual(again['expected_revision'], Views(self.store.home).object('source_check', saved['id'])[2])
        with self.assertRaisesRegex(ValueError, 'merge every calendar'):
            self.view(self.source(), self.source())

    def test_window_checkpoints_do_not_overwrite_legacy_or_each_other(self):
        old = self.service.source_check({'source': {'connection': 'apple', 'account': 'fixture', 'resource': 'calendars:all'},
            'scope': 'Legacy next seven days', 'started_at': self.now.timestamp(), 'status': 'checked',
            'expected_resources': ['work'], 'checked_resources': ['work'], 'summary': 'Legacy success', 'cursor': 'legacy'})
        rolling = self.service.source_check(self.view(self.source())['coverage'][0]['source_check'])
        self.query = self.service.calendar_window({'mode': 'month'})
        month = self.service.source_check(self.view(self.source())['coverage'][0]['source_check'])
        self.assertEqual(len({old['id'], rolling['id'], month['id']}), 3)
        self.assertEqual(self.store.read('source_check', old['id']), old)
        self.assertEqual(self.store.read('source_check', rolling['id']), rolling)
        bad = self.source(); bad['source']['resource'] = 'calendars:all'
        with self.assertRaisesRegex(ValueError, 'separate'):
            self.view(bad)
        with self.assertRaises(ValueError):
            self.service.source_check({**self.view(self.source())['coverage'][0]['source_check'], 'source_id': old['id']})
        forged = self.view(self.source())['coverage'][0]['source_check']
        forged['source'] = old['source']
        with self.assertRaisesRegex(ValueError, 'source resource'):
            self.service.source_check(forged)
        self.assertEqual(self.store.read('source_check', old['id']), old)

    def test_stale_inventory_or_event_receipts_cannot_advance_success(self):
        original = self.source([self.event()])
        saved = self.service.source_check(self.view(original)['coverage'][0]['source_check'])
        self.now += timedelta(minutes=30)
        self.query = self.service.calendar_window({})
        for stale_part in ('inventory', 'events'):
            with self.subTest(stale_part=stale_part):
                source = copy.deepcopy(original)
                source['calendars'][0]['window'] = self.query
                if stale_part == 'events':
                    source['inventory']['pages'][0]['observed_at'] = self.now.isoformat()
                else:
                    source['calendars'][0]['pages'][0]['observed_at'] = self.now.isoformat()
                result = self.view(source)
                self.assertFalse(result['complete'])
                self.assertTrue(any('predates query' in gap for gap in result['gaps']))
                check = self.service.source_check(result['coverage'][0]['source_check'])
                for field in ('last_success', 'last_success_window', 'last_success_scope', 'cursor'):
                    self.assertEqual(check[field], saved[field])

    def test_same_query_older_observation_cannot_replace_newer_completion(self):
        initial = self.view(self.source())['coverage'][0]['source_check']
        self.service.source_check(initial)
        self.now += timedelta(seconds=2)
        latest = self.service.source_check({**initial, 'observed_at': self.now.isoformat(), 'cursor': 'newer'})
        self.assertEqual(self.service.source_check(initial), latest)

    def test_runtime_action_entrypoints_and_private_session_boundary(self):
        runtime = Runtime(None, self.temp.name)
        self.addCleanup(runtime.close)
        runtime.service = self.service
        child_context = types.ModuleType('agent.delegation_context')
        child_context.is_delegated_child_context = lambda: False
        with patch.dict('sys.modules', {'agent.delegation_context': child_context}), \
             patch.object(runtime.host, 'current_delivery', return_value=None), \
             patch('hermes_muse.runtime.profile_scope', side_effect=lambda home: contextlib.nullcontext()), \
             patch('hermes_muse.runtime.session_info', return_value={'chat_type': 'private', 'cron': False}):
            query = json.loads(runtime.handle({'action': 'calendar_window'}))
            self.assertTrue(query['ok'], query)
            self.assertEqual(query['result'], self.query)
            result = json.loads(runtime.handle({'action': 'calendar_view', 'data': {'window': query['result'],
                'sources': [self.source([self.event(preparation={'reason': 'Book travel'})])], 'discovery_complete': True}}))
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['result']['preparation_candidates'], [0])
            malformed = json.loads(runtime.handle({'action': 'calendar_view', 'data': {}}))
            self.assertFalse(malformed['ok'])
        self.assertTrue({'calendar_window', 'calendar_view'} <= set(SCHEMA['parameters']['properties']['action']['enum']))
        with patch('hermes_muse.runtime.session_info', return_value={'chat_type': 'group'}):
            rejected = json.loads(runtime.handle({'action': 'calendar_window'}))
        self.assertFalse(rejected['ok'])
        self.assertIn('private/direct', rejected['error'])

    def test_empty_unavailable_missing_inventory_and_bad_pages(self):
        empty = self.view(self.source([], resources=[]))
        self.assertTrue(empty['complete'])
        self.assertTrue(empty['coverage'][0]['source_check']['checked_resources'])
        unavailable = self.source(); unavailable['inventory'] = {'status': 'unavailable', 'error': 'not authorized', 'pages': []}
        result = self.view(unavailable)
        self.assertFalse(result['complete'])
        self.assertEqual(result['coverage'][0]['source_check']['status'], 'unavailable')
        self.assertFalse(self.view(discovery_complete=False)['complete'])
        for kind in ('missing', 'wrong-token', 'loop', 'wrong-window', 'bad-time', 'missing-read', 'partial-page'):
            with self.subTest(kind=kind):
                source = self.source([self.event()]); read = source['calendars'][0]
                if kind == 'missing': read['pages'][0]['next_token'] = 'unread'
                if kind == 'wrong-token': read['pages'][0]['request_token'] = 'wrong'
                if kind == 'loop':
                    read['pages'][0]['next_token'] = 'loop'
                    read['pages'].append(self.page([], request_token='loop', next_token='loop'))
                if kind == 'wrong-window': read['window'] = {**self.query, 'end': '2026-10-19T12:00:00-04:00'}
                if kind == 'bad-time': read['pages'][0]['items'][0]['start'] = '2026-10-20T10:00:00'
                if kind == 'missing-read': source['calendars'] = []
                if kind == 'partial-page': read['pages'][0]['status'] = 'partial'
                result = self.view(source)
                self.assertFalse(result['complete'])
                self.assertEqual(result['coverage'][0]['source_check']['checked_resources'], [])
                if kind == 'partial-page': self.assertEqual(len(result['events']), 1)


if __name__ == '__main__':
    unittest.main()
