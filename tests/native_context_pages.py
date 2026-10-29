"""Real host registry/hooks, large synthetic profile, no provider calls or sends."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def worker(home):
    repo=Path(__file__).resolve().parents[1]
    sys.path[:0]=[str(repo),str(repo/'tests'),os.environ['HERMES_SOURCE']]
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    shutil.copytree(repo,home/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git','__pycache__'))
    from hermes_cli.plugins import discover_plugins,invoke_hook
    from tools.registry import registry
    from model_tools import handle_function_call
    from hermes_state import SessionDB
    from cron.executions import create_execution,mark_execution_running
    from agent.delegation_context import delegated_child_context
    from tool_client import arguments,conversations
    discover_plugins(force=True)
    rt=registry.get_entry('muse_manage').handler.__self__
    native=SessionDB(db_path=home/'state.db');native.create_session('origin',source='cli',chat_type='private')
    native.append_message('origin','user','Correct the synthetic Feed arithmetic; maintain notes.')
    invoke_hook('pre_llm_call',session_id='origin',turn_id='user-repair',platform='cli',user_message='Correct the synthetic Feed arithmetic; maintain notes.')
    long_user = 'Long native user message: ' + 'Evidence ' * 5000
    native.append_message('origin','user',long_user)
    invoke_hook('pre_llm_call',session_id='origin',turn_id='long-user',platform='cli',user_message=long_user)
    now=time.time();rt.service.clock=lambda:now
    def call(action,data=None,ok=True,task=None):
        raw=handle_function_call('muse_manage',{'action':action,'data':data or {}},session_id='origin',turn_id='user-repair',task_id=task,enabled_toolsets=['muse'])
        assert len(raw.encode())<=256*1024,(action,len(raw.encode()))
        value=json.loads(raw);assert value['ok'] is ok,value
        return value.get('result') if ok else value
    with rt.store.transaction() as db:
        for i in range(40):
            rt.store.put(db,'signal',f'large-{i:04}',{'id':f'large-{i:04}','session_id':'origin','created':now-100+i//275,'text':'Synthetic long signal '+('证据'*1500)})
        for i in range(40):
            rt.store.put(db,'source_check',f'source-{i}',{'id':f'source-{i}','status':'partial','summary':'x'*6000})
    overview=call('context');assert 'signal_reviews' in overview and 'source_checks' not in overview
    state=call('status');assert state['sections']==overview['sections']
    first=call('context',{'section':'source_check','limit':100});ids=[r['id'] for r in first['items']]
    while first['next_cursor']:
        first=call('context',{'section':'source_check','limit':100,'cursor':first['next_cursor']});ids.extend(r['id'] for r in first['items'])
    assert len(ids)==len(set(ids))==40
    assert call('context',{'section':'feed','home':'foreign'},ok=False)['code']=='invalid_argument'
    with delegated_child_context('native-reader'):
        assert call('context',{'section':'source_check','limit':3})['total']==40
        assert not call('context',{'section':'signals','job':'memory-upkeep'},ok=False)['ok']
        assert not call('feed_update',{},ok=False)['ok']
    attempt=create_execution(rt.host.manifest()['fixed']['memory-upkeep'],source='test');mark_execution_running(attempt['id'])
    task='cron:'+attempt['job_id']+':'+attempt['id']
    first=call('context',{'section':'signals','job':'memory-upkeep','limit':41},task=task)
    window=first['window'];through=first['through'];seen=[];page=first
    cp={'job':'memory-upkeep','window':window,'through':through,'up_to':through,'summary':'Reviewed all synthetic evidence'}
    assert call('review_complete',cp,ok=False,task=task)['code']=='revision_required'
    while True:
        for row in page['items']:
            offset=0;body=''
            while offset is not None:
                text=call('context',{'section':'signals','job':'memory-upkeep','window':window,'id':row['id'],'offset':offset},task=task)
                body+=text['body'];offset=text['next_offset']
            if row['id']=='long-user':
                assert row['requires_original'] and body==long_user and text['message_id']
            seen.append(row['id'])
        if not page['next_cursor']:break
        page=call('context',{'section':'signals','job':'memory-upkeep','window':window,'limit':41,'cursor':page['next_cursor']},task=task)
    assert len(seen)==len(set(seen))==30
    now+=1;rt.service.signal('later','origin','Concurrent later signal')
    assert call('review_complete',cp,task=task)['complete']
    assert call('context',{'section':'signals','job':'memory-upkeep'},task=task)['total']==13
    # Native history pages survive an append, but never count the appended text as read.
    for i in range(9):native.append_message('origin','assistant','Original evidence '+str(i)+'x'*18000)
    notice=call('notification_add',arguments(rt,'notification_add',{'event_key':'long-history-notice','message':'Synthetic result','sources':['fixture:source'],'rationale':'Synthetic relevance','verified_at':now,'expires':now+3600}))
    blocked=call('notification_review',arguments(rt,'notification_review',{'id':notice['id'],'conversation_revisions':{},'verdict':'relevant','reason':'Evidence not yet read'}))
    from hermes_muse.conversation import REVIEW_CHARS
    assert not blocked['reviewed'] and [r['data']['session_id'] for r in blocked['read']]==['origin']
    assert blocked['read'][0]['data']['offset']>0  # Only the newest text, not the whole day.
    d=call('conversation_read',{'session_id':'origin'})
    one=call('conversation_read',{'session_id':'origin','revision':d['revision'],'offset':0})
    native.append_message('origin','user','New correction must also be read')
    changed=call('conversation_read',{'session_id':'origin','revision':d['revision'],'offset':one['next_offset']})
    assert not changed['available'] and changed['next_offset']==changed['total_text_chars']-REVIEW_CHARS
    states=conversations(call);assert states['origin']['complete']
    reviewed=call('notification_review',arguments(rt,'notification_review',{'id':notice['id'],'conversation_revisions':{'origin':states['origin']['revision']},'verdict':'relevant','reason':'Read all original and appended evidence'}))
    assert reviewed['reviewed']
    # Existing semantic Feed edit is exposed in docs; preserve sources and use exact CAS.
    feed=call('feed_add',arguments(rt,'feed_add',{'event_key':'ratio','title':'Synthetic ratio','content':'368.7 / 3.687 is tenfold','sources':['fixture:source'],'why':'Correction fixture'}))
    data=arguments(rt,'feed_update',{'id':feed['id'],'signal_id':'user-repair','content':'368.7 / 3.687 is 100-fold'})
    corrected=call('feed_update',data);assert corrected['sources']==['fixture:source']
    body=call('content_read',{'kind':'feed','id':feed['id']});assert '100-fold' in body['body']
    stale={**data,'request_id':'stale-correction','content':'Old conflicting correction'}
    assert call('feed_update',stale,ok=False)['code']=='revision_conflict'
    assert call('feed_update',data)==corrected
    # A large successful mutation returns a durable reference, not an error after committing.
    row=rt.store.read('feed',feed['id']);row['why']='x'*300000;rt.store.write('feed',feed['id'],row)
    data=arguments(rt,'feed_update',{'id':feed['id'],'signal_id':'user-repair','feedback':'Reviewed correction'})
    receipt=call('feed_update',data);assert receipt['result_is_reference'];assert call('feed_update',data)==receipt
    record=call('content_read',{'kind':'feed','id':feed['id']});assert not record['metadata_complete']
    # The documented absent creation actually works with a native evidence receipt.
    native.append_message('origin','assistant','I completed the synthetic correction; verify its actual state')
    audit=call('operations_read',{},task=task)
    assert audit['events']
    case=audit['events'][0];ref=case['evidence_refs'][0];receipt=case['receipt'];off=0
    while off is not None:
        original=call('operations_evidence',{'receipt':receipt,'ref':ref,'offset':off})
        assert not original['missing'];off=original['next_offset']
    finding=call('finding_record',{'expected_revision':'absent','request_id':'finding-create','evidence':[ref],
                    'summary':'Synthetic fixture finding','impact':'Fixture only'},task=task)
    assert call('content_read',{'kind':'finding','id':finding['id']})['metadata']['id']==finding['id']
    # Live goal-ID pages remain consumable while the parent closes reviewed work.
    for i in range(60):
        call('goal_create',{'id':f'goal-{i:03}','title':'Fixture','description':'Synthetic work','completion_criteria':'Explain result'})
        call('goal_update',{'id':f'goal-{i:03}','next_check_at':now-1})
    seen=[];cursor=''
    while True:
        due=call('context',{'section':'followups','cursor':cursor,'limit':7})
        with delegated_child_context('native-reader'):
            assert call('context',{'section':'followups','cursor':cursor,'limit':7})['items']==due['items']
        for item in due['items']:
            seen.append(item['id'])
            call('goal_update',{'id':item['id'],'status':'done','note':'Fixture checked and explained'})
        if not due['next_cursor']:break
        cursor=due['next_cursor']
    assert len(seen)==len(set(seen))==60
    assert call('context',{'section':'followups'})['due_total']==0
    print(json.dumps({'native_large_context':'PASS','selected_signals':30,'global_unprocessed':13,'native_truncated_signal_full_body':True,'source_checks':40,'due_goals_read_and_done':60,'real_registry_hooks':True,'child_write_denied':True,'caller_home_rejected':True,'append_only_history_progress':True,'feed_correction_CAS_replay':True,'finding_absent_creation':True,'notification_partial_then_reviewed':True,'oversized_mutation_receipt':'reference, replay preserved','provider_calls':0,'external_sends':0}))
    native.close();rt.close()


if __name__=='__main__':
    if len(sys.argv)>1:worker(Path(sys.argv[1]))
    else:
        assert os.environ.get('TMPDIR')
        with tempfile.TemporaryDirectory(prefix='muse-native-pages-') as tmp:
            env={**os.environ,'HERMES_HOME':tmp,'HERMES_DATA_DIR_SUFFIX':'-muse-isolated-test','PYTHONDONTWRITEBYTECODE':'1','HERMES_DISABLE_LAZY_INSTALLS':'1'}
            r=subprocess.run([sys.executable,__file__,tmp],env=env)
        print(json.dumps({'owned_home_removed_after_worker_exit':not Path(tmp).exists()}));raise SystemExit(r.returncode)
