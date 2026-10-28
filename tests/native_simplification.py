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
    print(json.dumps({'native_simplification':'PASS','real_registry_empty_scope_with_legacy_backlog':True,'nightly_no_second_signal_cursor':True,'old_long_signal_does_not_block_new_cancellation':True,'old_authority_preserved':True,'provider_calls':0,'external_sends':0}))
    native.close();rt.close()
