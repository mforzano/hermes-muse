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
    shutil.copytree(repo, home / 'plugins/hermes-muse', ignore=shutil.ignore_patterns('.git', '__pycache__', '.serena'))
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
            assert rows['old']['fade_due'] is True and rows['mid']['fade_due'] is False and rows['old']['dormant_since'] < time.time(), rows
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
