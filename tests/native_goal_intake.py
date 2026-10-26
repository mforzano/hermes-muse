"""Native goal-intake boundary: nightly review may goal_create (capped, signal-bound); nothing else changes goals."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


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

        def call(action, data, ok=True, session='parent', task='ordinary-parent-task-id'):
            raw = handle_function_call('muse_manage', {'action': action, 'data': data}, session_id=session, turn_id='u', task_id=task, enabled_toolsets=['muse'])
            value = json.loads(raw)
            assert value['ok'] == ok, value
            return value.get('result') if ok else value

        def execution(job):
            attempt = create_execution(fixed[job], source='test')
            mark_execution_running(attempt['id'])
            return attempt['id'], f"cron:{fixed[job]}:{attempt['id']}"

        def goal(key, **extra):
            return {'id': key, 'title': 'Intake ' + key, 'description': 'Accepted in a conversation; paraphrased basis', 'completion_criteria': 'User sees the result', **extra}

        try:
            hint = invoke_hook('pre_llm_call', session_id='parent', turn_id='u', platform='cli', user_message='Please prepare the fixture report tomorrow')
            text = '\n'.join(h['context'] for h in hint if h)
            assert 'If you accepted a concrete request or promise in this turn' in text and 'Casual questions and passing mentions' in text, text
            assert 'created in the last 24h' not in text
            # Main session keeps goal access even when its task_id looks like a Cron one.
            night_id, night_task = execution('nightly-review')
            assert call('goal_create', goal('parent-goal', signal_id='u'), task=night_task)['status'] == 'active'
            hint = invoke_hook('pre_llm_call', session_id='parent', turn_id='u2', platform='cli', user_message='Another request')
            assert 'Goals created in the last 24h: Intake parent-goal.' in '\n'.join(h['context'] for h in hint if h)

            clear_session_vars(tokens)
            tokens = set_session_vars(platform='cron', source='cron', chat_type='private', session_id='cron-night', cron_session='1')
            err = call('goal_create', goal('no-signal'), False, session='cron-night', task=night_task)
            assert err['field'] == 'data.signal_id' and 'Fix:' in err['error'] and 'Traceback' not in err['error'], err
            assert call('goal_create', goal('unknown-signal', signal_id='missing'), False, session='cron-night', task=night_task)
            for index in range(5):
                made = call('goal_create', goal(f'night-{index}', signal_id='u'), session='cron-night', task=night_task)
                assert made['session_id'] == 'parent' and made['source_signal'] == 'u', made
            capped = call('goal_create', goal('night-5', signal_id='u'), False, session='cron-night', task=night_task)
            assert capped['code'] == 'size_limit' and capped['field'] == 'data' and 'at most 5' in capped['expected'] and 'already created 5' in capped['error'], capped
            assert not (home / 'muse/workspace/goals/night-5').exists()
            # A rejected create does not consume the cap; a later execution has its own cap.
            finish_execution(night_id, success=True)
            _, second = execution('nightly-review')
            call('goal_create', goal('night-0', signal_id='u'), False, session='cron-night', task=second)
            assert call('goal_create', goal('night-5', signal_id='u'), session='cron-night', task=second)['status'] == 'active'
            # Every other goal write stays parent-only for the nightly job.
            for action, data in (('goal_update', {'id': 'night-0', 'status': 'cancelled'}), ('goal_update', {'id': 'parent-goal', 'owner': None}),
                                 ('forget', {'kind': 'goal', 'id': 'night-0', 'signal_id': 'u', 'request_id': 'forget-night-0', 'expected_revision': call('content_read', {'kind': 'goal', 'id': 'night-0'}, session='cron-night', task=second)['revision']})):
                denied = call(action, data, False, session='cron-night', task=second)
                assert denied['code'] == 'permission_denied', denied
            assert runtime.store.goal('night-0')[0]['status'] == 'active' and runtime.store.goal('parent-goal')[0]['revision'] == 1
            # Other Cron jobs, task-less Cron sessions and child tasks cannot create goals.
            _, patrol = execution('proactive-watch')
            _, feed = execution('feed-pulse')
            for task in (patrol, feed, None, 'cron:unknown:unknown'):
                assert call('goal_create', goal('other-cron', signal_id='u'), False, session='cron-night', task=task)['code'] == 'permission_denied', task
            with delegated_child_context('child'):
                assert call('goal_create', goal('child', signal_id='u'), False, session='cron-night', task=second)['code'] == 'permission_denied'
            # An ended execution no longer carries the exception.
            ended, ended_task = execution('nightly-review')
            finish_execution(ended, success=True)
            assert call('goal_create', goal('late', signal_id='u'), False, session='cron-night', task=ended_task)['code'] == 'permission_denied'
            assert not (home / 'muse/workspace/goals/other-cron').exists() and not (home / 'muse/workspace/goals/late').exists()
        finally:
            clear_session_vars(tokens)
            db.close()
            get_plugin_manager().unload('hermes-muse')
        print(json.dumps({'native_goal_intake': 'PASS', 'nightly_cap': 5, 'model_provider_calls': 0, 'external_writes': 0}))


if __name__ == '__main__':
    run(Path(tempfile.mkdtemp(dir=os.environ.get('TMPDIR') or None, prefix='goal-intake-')) / 'home')
