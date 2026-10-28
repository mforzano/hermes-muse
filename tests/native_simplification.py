"""Real registry regression for selected maintenance scopes, no providers or sends."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo))
if os.environ.get('HERMES_SOURCE'):sys.path.insert(1,os.environ['HERMES_SOURCE'])
os.environ['HERMES_DISABLE_LAZY_INSTALLS']='1'
with tempfile.TemporaryDirectory(prefix='muse-native-scopes-') as folder:
    home=Path(folder);os.environ['HERMES_HOME']=folder
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    shutil.copytree(repo,home/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git','.serena','__pycache__'))
    from hermes_state import SessionDB
    native=SessionDB(db_path=home/'state.db')
    native.create_session('human',source='cli',chat_type='private')
    native.append_message('human','user','Old real user authority')
    from hermes_muse.store import Store
    before=Store(home)
    legacy={'message_id':0,'through':900,'since':time.time()-86400}
    before.write('meta','operation_cursor',legacy)
    before.write('meta','operation_wave',{'number':339,'done':False,'pages':[{'receipt':'old','reviewed':False}]})
    from hermes_cli.plugins import discover_plugins,invoke_hook
    from tools.registry import registry
    from model_tools import handle_function_call
    from cron.executions import create_execution,mark_execution_running
    from gateway.session_context import set_session_vars,clear_session_vars
    from agent.turn_finalizer import apply_llm_output_transform
    discover_plugins(force=True)
    rt=registry.get_entry('muse_manage').handler.__self__
    assert rt.store.read('meta','operation_cursor')==legacy
    assert rt.service.operations.policy()['legacy']['reviewed'] is False
    from hermes_muse.host import cron_run
    assert cron_run(home,'memory-upkeep')['wakeAgent'] is False
    # Unknown responsibility alone must wake; inspecting it does not change/replay it.
    for kind,record in [('mutation_request',{'id':'interrupted-write','state':'pending'}),
                        ('notification',{'id':'uncertain-delivery','status':'unknown'})]:
        rt.store.write(kind,record['id'],record)
        assert cron_run(home,'memory-upkeep')['wakeAgent'] is True
        assert rt.store.read(kind,record['id'])==record
        with rt.store.transaction() as db:rt.store.delete(db,kind,record['id'])
    assert cron_run(home,'memory-upkeep')['wakeAgent'] is False
    jid=rt.host.manifest()['fixed']['memory-upkeep']
    attempt=create_execution(jid,source='test');mark_execution_running(attempt['id'])
    task='cron:'+jid+':'+attempt['id'];sid='cron-fixture'
    tokens=set_session_vars(platform='cron',session_id=sid,chat_type='private')
    def call(action,data=None,ok=True):
        v=json.loads(handle_function_call('muse_manage',{'action':action,'data':data or {}},session_id=sid,task_id=task,enabled_toolsets=['muse']))
        assert v['ok'] is ok,v
        return v['result'] if ok else v
    try:
        scope=call('context',{'section':'signals','job':'memory-upkeep'})
        assert scope['total']==0
        assert call('review_complete',{'job':'memory-upkeep','window':scope['window'],'ids':[],'summary':'No new user signals'})['complete']
        assert call('run_complete',{'outcome':'complete','summary':'Local scope finished; old raw history remains unreviewed'})['status']=='ready'
        apply_llm_output_transform(SimpleNamespace(session_id=sid,model='fixture',platform='cron'),'[SILENT]',turn_id='finish')
        assert rt.store.read('run',attempt['id'])['status']=='complete'
        assert rt.store.read('meta','operation_cursor')==legacy
    finally:clear_session_vars(tokens)
    # Nightly uses confirmed understanding, never consumes another raw-signal cursor.
    before_night=rt.store.read('cursor','nightly-review',0)
    assert rt.service.review_complete({'job':'nightly-review','summary':'Confirmed memory unchanged; no new facts'})['complete']
    assert rt.store.read('cursor','nightly-review',0)==before_night
    # Old long input can remain unresolved while a new explicit cancellation is saved.
    rt.service.signal('long','human','x'*30000)
    rt.service.signal('cancel','human','Cancel only the second item; keep the first authorized')
    from hermes_muse.context_pages import context
    scope=context(rt.service,{'section':'signals','job':'memory-upkeep'})
    batch=context(rt.service,{'section':'signals','job':'memory-upkeep','window':scope['window'],'ids':['long','cancel']})
    assert batch['items'][0].get('gap') and batch['items'][1]['body'].startswith('Cancel')
    reviewed=rt.service.review_complete({'job':'memory-upkeep','window':scope['window'],'ids':['cancel'],'summary':'Saved exact partial cancellation; old authority remains'},require_window=True)
    assert not reviewed['complete'] and rt.store.read('signal','cancel')['upkeep']
    assert rt.store.read('signal','long') and not rt.store.read('signal','long').get('upkeep')
    # The 05:00 missing-scope counterexample: calendars are derivable, mail is not.
    window=call('calendar_window')
    args={'source':{'connection':'fixture','account':'fixture','resource':'calendars:rolling-30d'},
          'query_window':window,'status':'partial','summary':'Synthetic coverage, no real source queried',
          'expected_resources':['calendar'],'checked_resources':[], 'expected_revision':'absent','request_id':'scope-calendar'}
    saved=call('source_check',args)
    assert saved['scope'] and saved['query_window']==window
    missing=call('source_check',{**args,'source':{'connection':'fixture','account':'fixture','resource':'mail:inbox'},
        'query_window':None,'request_id':'scope-mail'},ok=False)
    assert 'scope' in missing['error']
    # Accepted work follows the already-known private parent lineage, without agents.
    tokens=set_session_vars(platform='cli',session_id='human',chat_type='private')
    try:
        invoke_hook('pre_llm_call',session_id='human',turn_id='accepted-parent',platform='cli',user_message='Please finish this accepted fixture work')
        goal=json.loads(handle_function_call('muse_manage',{'action':'goal_create','data':{
            'id':'accepted','title':'Accepted fixture','description':'Synthetic parent work','completion_criteria':'Verify result','signal_id':'accepted-parent'}},
            session_id='human',turn_id='accepted-parent',enabled_toolsets=['muse']))
        assert goal['ok'],goal
        native.create_session('worker',source='subagent',parent_session_id='human',chat_type='private')
        native.create_session('ordinary',source='cli',chat_type='private')
        invoke_hook('pre_llm_call',session_id='ordinary',turn_id='ordinary',platform='cli',user_message='Unrelated ordinary fixture')
        native.create_session('group',source='cli',parent_session_id='human',chat_type='group')
        native.create_session('group-child',source='subagent',parent_session_id='group',chat_type='private')
        native.create_session('foreign',source='cli',chat_type='private')
        refs={}
        for session in ('ordinary','worker','group-child','foreign'):
            refs[session]=[]
            for i in range(4):
                mid=native.append_message(session,'assistant',tool_calls=[{'id':session+str(i),'function':{
                    'name':'terminal','arguments':json.dumps({'command':'opaque fixture '+str(i)})}}])
                refs[session].append('message:'+str(mid))
        page=json.loads(handle_function_call('muse_manage',{'action':'operations_read'},session_id='human',enabled_toolsets=['muse']))['result']
        accepted_cases=[c for c in page['events'] if c['session_id']=='worker']
        assert set(refs['worker']) <= {ref for c in accepted_cases for ref in c['evidence_refs']}
        assert all('opaque_accepted_work' in c['reasons'] for c in accepted_cases)
        assert len({ref for c in page['events'] if c['session_id']=='ordinary' for ref in c['evidence_refs']})==2
        assert not any(c['session_id'] in {'group-child','foreign'} for c in page['events'])
    finally:clear_session_vars(tokens)
    print(json.dumps({'native_simplification':'PASS','unknown_responsibility_wakes_without_change':True,'accepted_private_child_not_sampled':True,'real_registry_empty_scope_with_legacy_backlog':True,'nightly_no_second_signal_cursor':True,'old_long_signal_does_not_block_new_cancellation':True,'old_authority_preserved':True,'provider_calls':0,'external_sends':0}))
    native.close();rt.close()
