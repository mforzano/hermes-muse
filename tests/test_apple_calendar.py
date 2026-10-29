"""Public Apple PIM shape with invented fixtures, no private calendar data."""
import copy
import unittest
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import test_calendar as fixtures
from hermes_muse.service import Companion
from test_companion import Host, prepare


class AppleCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = prepare(self.temp.name)
        self.now = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo('America/New_York'))
        self.host = Host()
        self.host.local_now = lambda timestamp: datetime.fromtimestamp(timestamp, self.now.tzinfo)
        self.service = Companion(self.store, self.host, lambda: self.now.timestamp())
        self.query = self.service.calendar_window({})

    def page(self, items):
        return {'status': 'checked', 'observed_at': self.now.isoformat(), 'items': items}

    def view(self, source):
        return self.service.calendar_view({'window': self.query, 'sources': [source], 'discovery_complete': True})

    def raw(self, **changes):
        return {'id': 'fixture-instance', 'title': 'Synthetic observance',
                'calendarId': 'fixture-calendar', 'calendar': 'Synthetic calendar',
                'isAllDay': True, 'startDate': '2026-10-19T16:00:00Z',
                'endDate': '2026-10-21T16:00:00Z',
                'localStart': '2026-10-20 12:00 AM', 'localEnd': '2026-10-22 12:00 AM', **changes}

    def full(self, **changes):
        # Full EventKit fields, shaped like the real CLI: all-day ends at 23:59:59 of its last day.
        return {'id': 'full-instance', 'title': 'Synthetic meeting', 'calendarId': 'fixture-calendar',
                'calendar': 'Synthetic calendar', 'isAllDay': False,
                'startDate': '2026-10-20T01:00:00Z', 'endDate': '2026-10-20T02:00:00Z',
                'localStart': '2026-10-20 9:00 AM', 'localEnd': '2026-10-20 10:00 AM',
                'timeZone': 'Asia/Shanghai', 'occurrenceDate': '2026-10-20T01:00:00Z',
                'isDetached': False, 'isRecurring': False, 'status': 'none', 'availability': 'busy',
                'calendarSourceId': 'synthetic-source', **changes}

    def full_all_day(self, **changes):
        # Floating all-day events carry no timeZone key at all.
        return {key: value for key, value in self.full(**{'id': 'full-day', 'isAllDay': True, 'startDate': '2026-10-19T16:00:00Z',
                            'endDate': '2026-10-20T15:59:59Z', 'localStart': '2026-10-20 12:00 AM',
                            'localEnd': '2026-10-20 11:59 PM', 'occurrenceDate': '2026-10-19T16:00:00Z',
                            'allDayStart': '2026-10-20', 'allDayEnd': '2026-10-20', 'availability': 'notSupported',
                            'timeZone': None, **changes}).items() if value is not None}

    def source(self, events=None, inventory_source_id=False):
        calendar = {'id': 'fixture-calendar', 'title': 'Synthetic calendar', 'source': 'Synthetic source', 'type': 'caldav'}
        if inventory_source_id:
            calendar['sourceId'] = 'synthetic-source'
        return {'source': {'connection': 'apple-pim', 'account': 'eventkit-local-store',
                           'resource': self.query['source_resource']},
                'title': 'Apple synthetic', 'receipt_format': 'apple-pim',
                'device_timezone': {'timezone': 'Asia/Shanghai', 'timezone_kind': 'iana'},
                'inventory': {'pages': [self.page([calendar])]},
                'calendars': [{'resource_id': 'fixture-calendar', 'window': self.query,
                               'pages': [self.page([self.raw()] if events is None else events)]}]}

    def test_real_shape_display_dates_utc_and_provenance_without_invention(self):
        source = self.source(); before = copy.deepcopy(source)
        result = self.view(source)
        self.assertEqual(source, before)
        self.assertFalse(result['complete'])
        self.assertEqual(result['coverage'][0]['source_check']['status'], 'partial')
        self.assertEqual(result['coverage'][0]['source_check']['checked_resources'], ['fixture-calendar'])
        row = result['events'][0]
        self.assertEqual(row['event']['original'], self.raw())
        self.assertEqual(row['event']['start'], self.raw()['startDate'])
        self.assertNotIn('timezone', row['calendar'])
        self.assertNotIn('all_day_dates', row)
        self.assertEqual(row['display_all_day_dates']['start'], '2026-10-20')
        self.assertEqual(row['display_all_day_dates']['end_exclusive'], '2026-10-22')
        self.assertEqual(row['display_all_day_dates']['basis'], 'device-display')
        # Profile is New York, device is Shanghai: retain both representations explicitly.
        self.assertTrue(row['local_start'].startswith('2026-10-19'))
        self.assertTrue(any(day['events'] for day in result['days']))
        self.assertTrue(all(not day['busy'] for day in result['days']))
        self.assertEqual(row['event']['status'], 'observed')

    def test_shared_native_id_expanded_instances_and_moved_observations_stay_distinct(self):
        first = self.raw(recurrence=[{'frequency': 'daily', 'interval': 1}])
        second = self.raw(startDate='2026-10-22T16:00:00Z', endDate='2026-10-23T16:00:00Z',
                          localStart='2026-10-23 12:00 AM', localEnd='2026-10-24 12:00 AM',
                          recurrence=first['recurrence'])
        result = self.view(self.source([first, second, copy.deepcopy(first)]))
        self.assertEqual(len(result['events']), 2)
        self.assertEqual(result['events'][0]['copies'], ['fixture-instance', 'fixture-instance'])
        for row in result['events']:
            self.assertTrue(row['event']['recurring'])
            self.assertNotIn('occurrence_id', row['event'])
            self.assertEqual(row['event']['recurrence_identity'], 'unavailable')
        self.assertFalse(result['complete'])

    def test_bad_local_time_resource_and_offset_preserve_valid_neighbors(self):
        for invalid in (self.raw(localStart='2026-10-19 12:00 AM'), self.raw(calendarId='other'),
                        self.raw(startDate='2026-10-19T16:00:00'), self.raw(isAllDay='true'),
                        self.raw(endDate='2026-10-18T16:00:00Z', localEnd='2026-10-19 12:00 AM')):
            with self.subTest(invalid=invalid):
                result = self.view(self.source([self.raw(), invalid]))
                self.assertEqual(len(result['events']), 1)
                self.assertFalse(result['coverage'][0]['source_check']['checked_resources'])
                self.assertFalse(result['complete'])

    def test_device_zone_required_and_dst_receipts_verified(self):
        source = self.source(); del source['device_timezone']
        with self.assertRaises(KeyError): self.view(source)
        source = self.source(); source['device_timezone'] = {'timezone': 'CST'}
        with self.assertRaises(KeyError): self.view(source)
        source = self.source(); source['device_timezone'] = {'timezone': 'CST', 'timezone_kind': 'fixed', 'utc_offset_seconds': 28800}
        self.assertIn('fixed offset', ' '.join(self.view(source)['gaps']))
        source = self.source([self.raw(startDate='2026-11-01T05:30:00Z', endDate='2026-11-01T06:30:00Z',
                           localStart='2026-11-01 1:30 AM', localEnd='2026-11-01 1:30 AM', isAllDay=False)])
        source['device_timezone'] = {'timezone': 'America/New_York'}
        result = self.view(source)
        self.assertEqual(len(result['events']), 1)
        self.assertNotEqual(result['events'][0]['local_start'], result['events'][0]['local_end'])

    def test_partial_page_and_limit_receipt_not_upgraded(self):
        source = self.source(); page = source['calendars'][0]['pages'][0]
        page.update(status='partial', error='limit reached without continuation')
        result = self.view(source)
        self.assertEqual(len(result['events']), 1)
        self.assertFalse(result['coverage'][0]['source_check']['checked_resources'])
        empty = self.view(self.source([]))
        self.assertFalse(empty['complete'])
        self.assertEqual(empty['events'], [])

    def test_no_persistence_and_partial_does_not_advance_last_success(self):
        result = self.view(self.source())
        self.assertFalse(self.store.all('source_check'))
        self.assertFalse(self.store.all('notification'))
        self.assertFalse(self.host.jobs)
        check = self.service.source_check(result['coverage'][0]['source_check'])
        self.assertFalse(check.get('last_success'))
        self.assertEqual(result['calendar_writes'], 0)
        fixture = fixtures.CalendarTests()
        fixture.query, fixture.now = self.query, self.now
        strict = fixture.source(connection='apple-pim', account='eventkit-local-store')
        baseline = self.service.source_check(self.view(strict)['coverage'][0]['source_check'])
        partial = self.service.source_check(result['coverage'][0]['source_check'])
        self.assertEqual(partial['last_success'], baseline['last_success'])
        self.assertEqual(partial['last_success_resources'], baseline['last_success_resources'])

    def test_supported_preparation_annotation_without_automatic_alert(self):
        result = self.view(self.source([self.raw(preparation={'reason': 'Synthetic travel booking evidence'})]))
        self.assertEqual(result['preparation_candidates'], [0])
        self.assertFalse(self.store.all('notification'))
        bad = self.view(self.source([self.raw(preparation={'due': '2026-10-13'})]))
        self.assertFalse(bad['events'])

    def test_strict_contract_cannot_opt_out_with_observed_marker(self):
        fixture = fixtures.CalendarTests()
        fixture.query = self.query
        fixture.now = self.now
        source = fixture.source([fixture.event(recurring=True,
                    time_basis='apple-pim-observed', status='observed')])
        source['inventory']['pages'][0]['items'][0]['display_timezone'] = {'timezone': 'UTC'}
        result = self.view(source)
        self.assertFalse(result['complete'])
        self.assertFalse(result['events'])

    def full_event(self, result, event_id):
        return next(row for row in result['events'] if row['event']['id'] == event_id)

    def test_full_fields_produce_strict_events_and_checked_coverage(self):
        timed = self.full()
        day = self.full_all_day()
        result = self.view(self.source([timed, day], inventory_source_id=True))
        self.assertTrue(result['complete'], result['gaps'])
        coverage = result['coverage'][0]
        self.assertEqual(coverage['source_check']['status'], 'checked')
        self.assertTrue(any('EXDATE' in note for note in coverage['known_limitations']))
        row = self.full_event(result, 'full-instance')
        self.assertNotIn('time_basis', row['event'])
        self.assertEqual(row['event']['timezone'], 'Asia/Shanghai')
        self.assertTrue(row['event']['busy'])
        self.assertEqual(row['event']['original'], timed)
        all_day = self.full_event(result, 'full-day')
        # 23:59:59 last-day end becomes Muse's exclusive end date; floating dates use the device zone.
        self.assertEqual(all_day['all_day_dates'], {'start': '2026-10-20', 'end_exclusive': '2026-10-21',
                                                    'timezone': 'Asia/Shanghai'})
        self.assertEqual(all_day['event']['timezone_basis'], 'floating-device')
        self.assertNotIn('display_all_day_dates', all_day)
        busy_days = [day for day in result['days'] if day['busy']]
        self.assertEqual([[result['events'][i]['event']['id'] for i in day['busy']] for day in busy_days], [['full-instance']])

    def test_empty_full_inventory_is_checked(self):
        result = self.view(self.source([], inventory_source_id=True))
        self.assertTrue(result['complete'], result['gaps'])
        self.assertEqual(result['coverage'][0]['source_check']['status'], 'checked')

    def test_cancelled_events_are_not_live_commitments(self):
        cancelled = self.full(status='canceled', preparation={'reason': 'Synthetic booking evidence'})
        result = self.view(self.source([cancelled], inventory_source_id=True))
        self.assertEqual(result['events'][0]['event']['status'], 'cancelled')
        self.assertFalse(any(day['events'] or day['busy'] for day in result['days']))
        self.assertEqual(result['preparation_candidates'], [])
        # A cancellation is honoured even when other full fields were filtered out.
        partial = self.view(self.source([self.raw(status='canceled')]))
        self.assertEqual(partial['events'][0]['event']['status'], 'cancelled')
        self.assertFalse(any(day['events'] for day in partial['days']))

    def test_recurring_and_detached_instances_keep_original_occurrence(self):
        first = self.full(isRecurring=True, recurrence=[{'frequency': 'daily', 'interval': 1}])
        moved = self.full(isRecurring=True, isDetached=True, recurrence=first['recurrence'],
                          startDate='2026-10-21T03:00:00Z', endDate='2026-10-21T04:00:00Z',
                          localStart='2026-10-21 11:00 AM', localEnd='2026-10-21 12:00 PM',
                          occurrenceDate='2026-10-21T01:00:00Z')
        result = self.view(self.source([first, moved], inventory_source_id=True))
        self.assertEqual([row['event']['occurrence_id'] for row in result['events']],
                         ['2026-10-20T01:00:00Z', '2026-10-21T01:00:00Z'])
        self.assertTrue(result['complete'], result['gaps'])

    def test_availability_drives_busy(self):
        for availability, busy in (('busy', True), ('unavailable', True), ('tentative', True), ('free', False)):
            with self.subTest(availability=availability):
                result = self.view(self.source([self.full(availability=availability)], inventory_source_id=True))
                self.assertIs(result['events'][0]['event']['busy'], busy)
        observed = self.view(self.source([self.raw(isAllDay=False, availability='busy')]))
        self.assertTrue(observed['events'][0]['event']['busy'])
        self.assertEqual(observed['events'][0]['event']['status'], 'observed')

    def test_mixed_or_unusable_full_fields_fall_back_to_observation(self):
        result = self.view(self.source([self.full(), self.raw()]))
        self.assertFalse(result['complete'])
        self.assertTrue(any('fields filter' in gap for gap in result['gaps']))
        self.assertEqual(self.full_event(result, 'fixture-instance')['event']['time_basis'], 'apple-pim-observed')
        fallback = self.view(self.source([self.full(timeZone='GMT+0800')], inventory_source_id=True))
        self.assertEqual(fallback['events'][0]['event']['time_basis'], 'apple-pim-observed')
        self.assertFalse(fallback['complete'])


if __name__ == '__main__':
    unittest.main()
