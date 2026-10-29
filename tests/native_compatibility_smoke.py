"""Real registration/core dispatch/Cron isolation with synthetic native history/provider."""
import copy
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]


def run(home):
    home.mkdir(parents=True)
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\ndelegation:\n  max_concurrent_children: 1\n')
    shutil.copytree(PROJECT,home/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git','__pycache__','.venv','.hermes-core'))
    from hermes_muse.host import profile_scope
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.config import load_config_readonly
    from tools.registry import registry
    from model_tools import handle_function_call
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_state import SessionDB
    from cron.jobs import list_jobs,get_job
    from cron.executions import create_execution,mark_execution_running
    from cron.scheduler import _CronRunScope
    with profile_scope(home):
        discover_plugins(force=True)
        entry=registry.get_entry('muse_manage');rt=entry.handler.__self__
        frozen=copy.deepcopy(entry.schema)
        db=SessionDB(db_path=home/'state.db');db.create_session('source',source='cli',chat_type='private')
        invoke_hook('pre_llm_call',session_id='source',turn_id='user-one',platform='cli',user_message='Prepare only the synthetic first and second scopes.')
        texts=['Original accepted scope; do not discard this older promise.']+['Synthetic chat '+str(n) for n in range(45)]+['x'*30000+'Cancel first scope only; keep second scope.']
        for text in texts:db.append_message('source','user',text)
        def call(action,data=None,ok=True,task=None):
            from tool_client import arguments, transport
            result=json.loads(handle_function_call('muse_manage',transport({'action':action,'data':arguments(rt,action,data)}),session_id='source',turn_id='user-one',tool_call_id='fixture-'+str(time.monotonic_ns()),task_id=task,enabled_toolsets=['muse']))
            from tool_client import record
            record(action,result)
            assert result['ok'] is ok,result
            return result.get('result') if ok else result
        state=call('context',{'section':'conversations'})['items'][0]
        assert not state['complete'] and 'messages' not in state
        assert state['total_text_chars'] > 24000
        cursor=0;body=''
        while cursor is not None:
            part=call('conversation_read',{'session_id':'source','revision':state['revision'],'offset':cursor})
            body+=''.join(m['text'] for m in part['messages']);cursor=part['next_offset']
        assert part['complete'] and body==''.join(texts)
        assert call('context',{'section':'conversations'})['items'][0]['complete']
        db.append_message('source','assistant','A new correction invalidates the previous full revision.')
        changed=call('conversation_read',{'session_id':'source','revision':state['revision']})
        assert not changed['available'] and not changed['complete']
        goal=call('goal_create',{'id':'fixture-goal','title':'Synthetic','signal_id':'user-one','description':'Only synthetic scopes','completion_criteria':'Read verified fixture and hand off'})
        jobs=list_jobs(include_disabled=True);assert len(jobs)==6
        job_id=rt.host.manifest()['fixed']['memory-upkeep'];job=get_job(job_id)
        attempt=create_execution(job_id,source='test');mark_execution_running(attempt['id'])
        isolation=_CronRunScope(job,job_id,attempt['id']);isolation.enter()
        try:
            task=isolation.task_id
            watch={'goal_id':'fixture-goal','signal_id':'user-one','prompt':'Read the authorized fixture','stop_condition':'When the verified fixture is complete'}
            refused=call('watch_create',watch,ok=False,task=task)
            assert 'allow_agent_scheduling' in refused['error'] and len(list_jobs(include_disabled=True))==6
            # Cron cannot create parent goals; existing non-goal scheduling behavior is unchanged.
            call('goal_create',{'id':'recovered','title':'Recovered original scope','signal_id':'user-one','description':'No scheduling','completion_criteria':'Verified synthetic result'},task=task,ok=False)
            assert len(list_jobs(include_disabled=True))==6
            config=copy.deepcopy(load_config_readonly());config['cron']={'allow_agent_scheduling':True}
            with patch('hermes_cli.config.load_config_readonly',return_value=config):
                bad={**watch,'prompt':'a'*4001}
                call('watch_create',bad,ok=False,task=task);assert len(list_jobs(include_disabled=True))==6
                created=call('watch_create',{**watch,'prompt':'p'*3996+'TAIL','stop_condition':'s'*3996+'STOP'},task=task)
                assert created['schedule']['kind']=='interval' and created['schedule']['minutes']==30
                assert 'TAIL' in created['prompt'] and created['prompt'].endswith('STOP')
                shorter=call('watch_create',{**watch,'schedule':'every 10m'},task=task,ok=False)
                assert '30 minutes' in shorter['error']
            # Actual public delegate handler and actual core stateless fallback; provider work is stubbed.
            parent=SimpleNamespace(valid_tool_names={'delegate_task'},session_id='source',_delegate_depth=0,model='fixture-parent',provider='custom',base_url='https://fixture.invalid/v1',api_key='synthetic',api_mode='chat_completions',request_overrides=None)
            def children(tasks,schemas,creds,**kwargs):
                # Older Hermes passes one credentials dict; newer passes per-task routes resolved from host config.
                assert not isinstance(creds,dict) or creds['model'] is None and creds['provider'] is None
                return [(i,t,SimpleNamespace()) for i,t in enumerate(tasks)],None
            with bind_subagent_parent(parent),patch('tools.delegate_tool._build_children',side_effect=children),patch('tools.delegate_tool_dispatch._execute_and_aggregate',return_value={'results':[{'status':'completed','summary':'Synthetic joined result'}]}):
                result=call('delegate',{'goal':'Inspect synthetic evidence'},task=task)
                assert 'results' in result and 'SYNCHRONOUSLY' in result['note']
        finally:isolation.exit()
        assert entry.schema==frozen and call('calendar_window')['days']==30
        assert not rt.store.all('notification') and not rt.store.all('source_check')
        db.close();rt.close()
        print(json.dumps({'native_compatibility':'PASS','real_cron_isolation':True,'public_delegate_sync_fallback':True,'watch_policy_and_fields':True,'goal_without_job':True,'full_conversation_pages_and_change_detection':True,'provider_calls':0,'external_writes':0}))
