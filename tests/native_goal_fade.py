"""Native goal fade: dormant goals leave followups; only the nightly review may cancel a fade-due goal."""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DAY = 86400


def run(home):
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    repo = Path(__file__).resolve().parents[1]
    shutil.copytree(repo, home / 'plugins/hermes-muse', ignore=shutil.ignore_patterns('.git', '__pycache__'))
    from cron.executions import create_execution, finish_execution, mark_execution_running
    from hermes_muse.host import profile_scope
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
    from hermes_state import SessionDB
    from tools.registry import registry
    from model_tools import handle_function_call
    from gateway.session_context import set_session_vars, clear_session_vars
    from agent.delegation_context import delegated_child_context
    with profile_scope(home):
        discover_plugins(force=True)
        runtime = registry.get_entry('muse_manage').handler.__self__
        fixed = runtime.host.manifest()['fixed']
        db = SessionDB(db_path=home / 'state.db')
        db.create_session('parent', source='cli', chat_type='private')
        tokens = set_session_vars(platform='cli', chat_type='private', session_id='parent', cron_session='')
        real_clock = runtime.service.clock

        def call(action, data, ok=True, session='parent', task='ordinary-parent-task-id'):
            raw = handle_function_call('muse_manage', {'action': action, 'data': data}, session_id=session, turn_id='u', task_id=task, enabled_toolsets=['muse'])
            value = json.loads(raw)
            assert value['ok'] == ok, value
            return value.get('result') if ok else value

        def execution(job):
            attempt = create_execution(fixed[job], source='test')
            mark_execution_running(attempt['id'])
            return attempt['id'], f"cron:{fixed[job]}:{attempt['id']}"

        def make(key, days_ago, **extra):
            runtime.service.clock = lambda: real_clock() - days_ago * DAY
            try:
                return call('goal_create', {'id': key, 'title': f'Plan {key}', 'description': 'Accepted request', 'completion_criteria': 'User sees it', 'signal_id': 'u', **extra})
            finally:
                runtime.service.clock = real_clock

        def dormant():
            return {r['id']: r for r in call('context', {'section': 'goal', 'status': 'dormant', 'limit': 100})['items']}

        try:
            invoke_hook('pre_llm_call', session_id='parent', turn_id='u', platform='cli', user_message='Chat about lunch')
            future = datetime.fromtimestamp(time.time() + 10 * DAY, timezone.utc).isoformat()
            for key, days, extra in (('old', 20, {}), ('mid', 10, {}), ('young', 3, {}), ('dated', 20, {'deadline': future}), ('waiting', 20, {})):
                make(key, days, **extra)
            call('goal_update', {'id': 'waiting', 'waiting_user': True})  # real clock now: the update is activity
            make('waiting2', 20)
            runtime.service.clock = lambda: real_clock() - 20 * DAY
            call('goal_update', {'id': 'waiting2', 'waiting_user': True})
            runtime.service.clock = real_clock

            rows = dormant()
            assert set(rows) == {'old', 'mid', 'waiting2'}, set(rows)
            assert rows['old']['fade_due'] is False and 'not been asked yet' in rows['old']['fade_wait'] and rows['old']['fade_at'] < time.time(), rows  # past its cancel time, but never asked
            assert rows['mid']['fade_due'] is False and rows['old']['dormant_since'] < time.time(), rows
            assert all(r['status'] == 'active' for r in rows.values())
            listing = {r['id']: r for r in call('context', {'section': 'goal', 'limit': 100})['items']}
            assert 'dormant_since' in listing['old'] and 'dormant_since' not in listing['young'] and 'dormant_since' not in listing['dated'], listing
            due = {i['id'] for i in call('context', {'section': 'followups'})['items']}
            assert due == {'young', 'waiting', 'dated'}, due  # exempt/active goals stay; old, mid, waiting2 are dormant
            hint = '\n'.join(h['context'] for h in invoke_hook('pre_llm_call', session_id='parent', turn_id='u3', platform='cli', user_message='Anything new today?') if h)
            assert 'need attention' in hint and not any(f'Plan {k}' in hint for k in ('old', 'mid', 'waiting2')), hint

            clear_session_vars(tokens)
            tokens = set_session_vars(platform='cron', source='cron', chat_type='private', session_id='cron-night', cron_session='1')
            night_id, night = execution('nightly-review')
            cancel = {'id': 'old', 'status': 'cancelled'}
            # Not eligible: wrong stage, exempt, active, extra fields, other statuses, other actions.
            for key, reason in (('mid', 'cancel time has not come'), ('dated', 'future deadline'), ('young', 'not dormant'), ('waiting', 'not dormant')):
                refused = call('goal_update', {'id': key, 'status': 'cancelled'}, False, session='cron-night', task=night)
                assert refused['field'] == 'data.id' and reason in refused['error'] and 'Fix:' in refused['error'] and 'Traceback' not in refused['error'], refused
                assert runtime.store.goal(key)[0]['status'] == 'active'
            for data in ({'id': 'old', 'status': 'cancelled', 'owner': None}, {'id': 'old', 'status': 'done', 'note': 'x'}, {'id': 'old', 'progress': 'x'},
                         {'id': 'old', 'status': 'active'}):
                refused = call('goal_update', data, False, session='cron-night', task=night)
                assert refused['field'] == 'data' and 'status' in refused['expected'], refused
            forget = call('forget', {'kind': 'goal', 'id': 'old', 'signal_id': 'u', 'request_id': 'fade-forget',
                                     'expected_revision': call('content_read', {'kind': 'goal', 'id': 'old'}, session='cron-night', task=night)['revision']}, False, session='cron-night', task=night)
            assert forget['code'] == 'permission_denied', forget
            assert runtime.store.goal('old')[0]['status'] == 'active'
            # Other Cron jobs, task-less Cron, platforms delegate/subagent, children cannot cancel.
            _, patrol = execution('proactive-watch')
            for task in (patrol, None, 'cron:unknown:unknown'):
                assert call('goal_update', cancel, False, session='cron-night', task=task)['code'] == 'permission_denied', task
            for platform in ('subagent', 'delegate'):
                clear_session_vars(tokens)
                tokens = set_session_vars(platform=platform, source='cron', chat_type='private', session_id='cron-night', cron_session='1')
                assert call('goal_update', cancel, False, session='cron-night', task=night)['code'] == 'permission_denied', platform
            clear_session_vars(tokens)
            tokens = set_session_vars(platform='cron', source='cron', chat_type='private', session_id='cron-night', cron_session='1')
            with delegated_child_context('child'):
                assert call('goal_update', cancel, False, session='cron-night', task=night)['code'] == 'permission_denied'
            assert runtime.store.goal('old')[0]['status'] == 'active'
            # Past its cancel time but never asked: still refused, even from the right job.
            unasked = call('goal_update', cancel, False, session='cron-night', task=night)
            assert 'has not been asked yet' in unasked['error'] and rows['old']['ask_key'].startswith('goal-fade-ask:old:'), unasked
            # Cron cannot move the thresholds that eligibility is computed from, nor exempt goals (real dispatch).
            prefs = call('content_read', {'kind': 'preferences', 'id': 'current'}, session='cron-night', task=night)['revision']
            for days in ({'goal_dormant_days': 1}, {'goal_fade_days': 1}):
                blocked = call('preferences', {**days, 'expected_revision': prefs, 'request_id': 'fade-prefs-' + next(iter(days)), 'signal_id': 'u'}, False, session='cron-night', task=night)
                assert blocked['field'] == 'data.' + next(iter(days)) and 'private parent conversation' in blocked['error'] and 'run_complete' in blocked['hint'], blocked
            _, patrol_run = execution('proactive-watch')
            assert call('preferences', {'goal_dormant_days': 1, 'expected_revision': prefs, 'request_id': 'fade-prefs-patrol', 'signal_id': 'u'}, False, session='cron-night', task=patrol_run)['field'] == 'data.goal_dormant_days'
            assert (runtime.service.preferences()['goal_dormant_days'], runtime.service.preferences()['goal_fade_days']) == (7, 7)
            pinned = call('goal_create', {'title': 'Pinned', 'description': 'd', 'completion_criteria': 'c', 'signal_id': 'u', 'no_fade': True, 'id': 'sig'}, False, session='cron-night', task=night)
            assert pinned['field'] == 'data.no_fade' and not (home / 'muse/workspace/goals/sig').exists(), pinned
            # A question the nightly review adds and resolves itself, or one that expired unsent, never counts as asked.
            iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
            forged = call('notification_add', {'event_key': rows['waiting2']['ask_key'], 'message': 'Still want this?', 'rationale': 'dormant', 'sources': ['goal:waiting2'],
                                               'goal_id': 'waiting2', 'verified_at': iso(time.time()), 'expires': iso(time.time() + 3 * DAY),
                                               'request_id': 'fade-forged-ask', 'expected_revision': 'absent'}, session='cron-night', task=night)
            notice_id = forged['id']
            notice_revision = call('content_read', {'kind': 'notice', 'id': notice_id}, session='cron-night', task=night)['revision']
            resolved = call('notification_resolve', {'id': notice_id, 'verified_at': iso(time.time()), 'sources': ['goal:waiting2'], 'reason': 'No longer relevant',
                                                     'request_id': 'fade-forged-resolve', 'expected_revision': notice_revision}, session='cron-night', task=night)
            row = runtime.store.read('notification', notice_id)
            assert row['status'] != 'sent', row
            for status in (row['status'], 'expired', 'failed', 'dismissed', 'unknown'):
                with runtime.store.transaction() as store_db:
                    runtime.store.put(store_db, 'notification', notice_id, {**row, 'status': status, 'created': time.time() - 3 * DAY})
                refused = call('goal_update', {'id': 'waiting2', 'status': 'cancelled'}, False, session='cron-night', task=night)
                assert 'has not been delivered yet' in refused['error'], (status, refused)
                assert runtime.store.goal('waiting2')[0]['status'] == 'active'
            # A question the nightly review staged before the goal went dormant (its key is predictable) is not that question.
            early = call('notification_add', {'event_key': rows['mid']['ask_key'], 'message': 'Weather is nice today', 'rationale': 'x', 'sources': ['goal:mid'], 'goal_id': 'mid',
                                              'verified_at': iso(time.time()), 'expires': iso(time.time() + 3 * DAY), 'request_id': 'fade-early-ask', 'expected_revision': 'absent'},
                         session='cron-night', task=night)
            early_row = runtime.store.read('notification', early['id'])
            with runtime.store.transaction() as store_db:
                runtime.store.put(store_db, 'notification', early['id'], {**early_row, 'status': 'sent', 'created': rows['mid']['dormant_since'] - DAY, 'dispatched_at': time.time() - 3 * DAY})
            runtime.service.clock = lambda: real_clock() + 5 * DAY  # mid is now past its cancel time and its "question" was delivered long ago
            early_refused = call('goal_update', {'id': 'mid', 'status': 'cancelled'}, False, session='cron-night', task=night)
            runtime.service.clock = real_clock
            assert 'has not been asked yet' in early_refused['error'] and runtime.store.goal('mid')[0]['status'] == 'active', early_refused
            # The question is asked and delivered; a day later the cancel is allowed.
            ask = runtime.service.notification_add({'event_key': rows['old']['ask_key'], 'message': 'Still want this?', 'rationale': 'dormant', 'sources': ['goal:old'], 'goal_id': 'old',
                                                    'verified_at': time.time(), 'expires': time.time() + 3 * DAY})
            with runtime.store.transaction() as store_db:
                runtime.store.put(store_db, 'notification', ask['id'], {**ask, 'status': 'candidate'})
            assert 'not been delivered yet' in call('goal_update', cancel, False, session='cron-night', task=night)['error']
            with runtime.store.transaction() as store_db:
                runtime.store.put(store_db, 'notification', ask['id'], {**ask, 'status': 'sent', 'created': time.time() - 2 * DAY, 'dispatched_at': time.time() - 2 * DAY})
            assert call('content_read', {'kind': 'goal', 'id': 'old'}, session='cron-night', task=night)  # still readable
            # The eligible goal is cancelled with the canonical note; the caller's note cannot replace it.
            done = call('goal_update', {**cancel, 'note': 'my own words'}, session='cron-night', task=night)
            assert done['status'] == 'cancelled' and done['progress'][-1]['text'] == 'no user activity for 20 days; auto-faded', done
            assert call('goal_update', cancel, False, session='cron-night', task=night)['error'].count('not active')
            # Ended execution no longer carries the exception.
            finish_execution(night_id, success=True)
            assert call('goal_update', {'id': 'waiting2', 'status': 'cancelled'}, False, session='cron-night', task=night)['code'] == 'permission_denied'

            # The main session (even with a nightly-looking task_id) keeps full goal access and can revive a faded goal.
            clear_session_vars(tokens)
            tokens = set_session_vars(platform='cli', chat_type='private', session_id='parent', cron_session='')
            revived = call('goal_update', {'id': 'old', 'status': 'active', 'note': 'User asked again'}, task=night)
            assert revived['status'] == 'active' and 'old' not in dormant()
            assert 'old' in {i['id'] for i in call('context', {'section': 'followups'})['items']}
            # A real Muse watch (native Cron job, checked through Host.is_active_watch) exempts its goal until it is stopped.
            make('watchme', 20)
            assert 'watchme' in dormant()
            watch = call('watch_create', {'goal_id': 'watchme', 'signal_id': 'u', 'schedule': 'every 1h', 'prompt': 'Check the synthetic source', 'stop_condition': 'Verified',
                                          'request_id': 'fade-watch', 'expires': time.time() + 7 * DAY})
            job_id = watch.get('job_id') or watch['id']
            assert 'watchme' not in dormant(), 'an enabled watch exempts its goal'
            call('watch_stop', {'job_id': job_id})
            assert 'watchme' in dormant(), 'a stopped watch no longer exempts it'
            # The parent sets no_fade and may change the thresholds.
            call('goal_update', {'id': 'waiting2', 'no_fade': True})
            assert 'waiting2' not in dormant() and runtime.store.goal('waiting2')[0]['no_fade'] is True
            bad = call('goal_update', {'id': 'waiting2', 'no_fade': 'yes'}, False)
            assert bad['field'] == 'data.no_fade' and 'JSON boolean' in bad['hint'], bad
            # Configurable days (profile preferences).
            runtime.service.preference_update({'goal_dormant_days': 30})
            assert dormant() == {}
            runtime.service.preference_update({'goal_dormant_days': 5, 'goal_fade_days': 100})
            rows = dormant()
            assert 'mid' in rows and rows['mid']['fade_due'] is False and 'young' not in rows, set(rows)
        finally:
            runtime.service.clock = real_clock
            clear_session_vars(tokens)
            db.close()
            get_plugin_manager().unload('hermes-muse')
        print(json.dumps({'native_goal_fade': 'PASS', 'model_provider_calls': 0, 'external_writes': 0}))


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    for path in (os.environ.get('HERMES_SOURCE'), str(root / 'tests'), str(root)):
        if path:
            sys.path.insert(0, path)
    base = Path(os.environ['HERMES_HOME']) if os.environ.get('HERMES_HOME') else Path(tempfile.mkdtemp(prefix='goal-fade-'))
    run(base / 'goal-fade')
