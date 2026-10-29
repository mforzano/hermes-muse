"""Native goal E2E: parent task IDs, owner readback, done, child/Cron boundaries."""
import json
import shutil
import time
from pathlib import Path


def run(home):
    home.mkdir(parents=True)
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    repo=Path(__file__).resolve().parents[1]
    shutil.copytree(repo,home/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git','__pycache__'))
    from hermes_muse.host import profile_scope
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
    from hermes_state import SessionDB
    from tools.registry import registry
    from model_tools import handle_function_call
    from gateway.session_context import set_session_vars, clear_session_vars
    from agent.delegation_context import delegated_child_context
    from tools.async_delegation import _persist_dispatch, _persist_completion
    from cron.jobs import list_jobs
    with profile_scope(home):
        discover_plugins(force=True)
        runtime=registry.get_entry('muse_manage').handler.__self__
        db=SessionDB(db_path=home/'state.db');db.create_session('parent',source='cli',chat_type='private')
        tokens=set_session_vars(platform='cli',chat_type='private',session_id='parent',cron_session='')
        observed=[]
        def call(action,data,ok=True,session='parent',task='ordinary-parent-task-id'):
            raw=handle_function_call('muse_manage',{'action':action,'data':data},session_id=session,turn_id='u',task_id=task,enabled_toolsets=['muse'])
            observed.append(len(raw.encode()));assert observed[-1]<=262144
            value=json.loads(raw);assert value['ok']==ok,value
            return value.get('result') if ok else value
        try:
            invoke_hook('pre_llm_call',session_id='parent',turn_id='u',platform='cli',user_message='Research the fixture')
            g=call('goal_create',{'id':'fixture','title':'Fixture','description':'Read fixture result','completion_criteria':'Explain the result'})
            assert g['status']=='active' and g['revision']==1 and 'followup' not in g
            assert call('context',{'section':'followups'})['due_total']==0
            call('goal_update',{'id':'fixture','owner':{'kind':'native_delegation','id':'deleg_fixture'},'next_check_at':time.time()-1})
            _persist_dispatch({'delegation_id':'deleg_fixture','origin_session_id':'parent','dispatched_at':time.time()})
            _persist_completion({'delegation_id':'deleg_fixture','status':'completed'},{'results':[{'status':'completed'}]})
            due=call('context',{'section':'followups'})
            assert due['due_total']==1 and due['items'][0]['owner_status']=='completed'
            assert runtime.store.goal('fixture')[0]['status']=='active'  # Owner completion is not goal completion.
            for state in ('interrupted','failed'):
                _persist_completion({'delegation_id':'deleg_fixture','status':'completed'},{'results':[{'status':state}]})
                assert call('context',{'section':'followups'})['items'][0]['owner_status']==state
                assert runtime.store.goal('fixture')[0]['status']=='active'
            call('goal_update',{'id':'fixture','owner':{'kind':'native_delegation','id':'deleg_missing'}})
            assert call('context',{'section':'followups'})['items'][0]['owner_status']=='unknown'
            assert runtime.store.goal('fixture')[0]['status']=='active'
            invoke_hook('pre_llm_call',session_id='parent',turn_id='new-question',platform='cli',user_message='What is the status?')
            db.append_message('parent','assistant','The fixture result is complete and has been explained.')
            done=call('goal_update',{'id':'fixture','status':'done','note':'Result checked and explained in this conversation'})
            assert done['status']=='done' and call('context',{'section':'followups'})['due_total']==0
            from unittest.mock import patch
            call('goal_create',{'id':'cleanup','title':'Cleanup fixture','description':'Synthetic','completion_criteria':'Explain result'})
            with patch.object(runtime.host,'pause_owner',side_effect=OSError('Synthetic cleanup failure')):
                committed=call('goal_update',{'id':'cleanup','status':'done','note':'Result explained','expected_revision':1})
            assert committed['status']=='done' and 'watch cleanup failed' in committed['warning']
            assert runtime.store.goal('cleanup')[0]['revision']==2
            with delegated_child_context('child'):
                assert call('goal_update',{'id':'fixture','note':'Unauthorized'},False)['code']=='permission_denied'
                assert call('content_write',{'kind':'tools_note','id':'current','body':'Unauthorized','expected_revision':'absent','request_id':'child'},False)['code']=='permission_denied'
            clear_session_vars(tokens)
            tokens=set_session_vars(platform='cron',source='cron',chat_type='private',session_id='cron',cron_session='1')
            assert call('goal_update',{'id':'fixture','status':'active'},False,session='cron',task=None)['code']=='permission_denied'
            assert call('goal_create',{'title':'Wrong','description':'Wrong','completion_criteria':'Wrong'},False,session='cron',task=None)['code']=='permission_denied'
            assert call('content_write',{'kind':'tools_note','id':'current','body':'Unauthorized','expected_revision':'absent','request_id':'cron','signal_id':'u'},False,task=None)['code']=='permission_denied'
            assert not runtime.store.all('followup_receipt') and not runtime.store.all('followup_turn')
            assert len(list_jobs(include_disabled=True))==6
        finally:
            clear_session_vars(tokens);db.close();get_plugin_manager().unload('hermes-muse')
        print(json.dumps({'native_followup':'PASS','ordinary_parent_task_id_accepted':True,'owner_completed_then_parent_done':True,'child_cron_writes_rejected':True,'fixed_jobs':6,'observed_max_response_bytes':max(observed),'model_provider_calls':0,'external_writes':0}))
