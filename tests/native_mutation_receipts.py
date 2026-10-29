"""Real registry CAS envelopes, interruption recovery and legacy receipt replay."""
import os,sys,json,shutil
from pathlib import Path
home=Path(os.environ['HERMES_HOME']);assert home.name.startswith('muse-independent-pages-')
repo=Path(__file__).resolve().parents[1];sys.path[:0]=[str(repo),str(repo/'tests'),os.environ['HERMES_SOURCE']]
(home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
shutil.copytree(repo,home/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git','__pycache__'))
from hermes_cli.plugins import discover_plugins,invoke_hook
from tools.registry import registry
from model_tools import handle_function_call
from hermes_state import SessionDB
from tool_client import arguments
discover_plugins(force=True);rt=registry.get_entry('muse_manage').handler.__self__
native=SessionDB(db_path=home/'state.db');native.create_session('origin',source='cli',chat_type='private');native.append_message('origin','user','ok')
invoke_hook('pre_llm_call',session_id='origin',turn_id='user-repair',platform='cli',user_message='ok')
def call(action,data):
    return json.loads(handle_function_call('muse_manage',{'action':action,'data':data},session_id='origin',turn_id='user-repair',enabled_toolsets=['muse']))
from unittest.mock import patch
from hermes_muse.views import Views, canonical
from hermes_muse.content_io import revision

cap=256*1024
expected={**rt.service.preferences(),'blocked_topics':[''],'changed':True}
n=cap-len(json.dumps({'ok':True,'result':expected},ensure_ascii=False).encode())-10
data=arguments(rt,'preferences',{'signal_id':'user-repair','blocked_topics':['x'*n]})
def bounded_call(action, data):
    raw=handle_function_call('muse_manage',{'action':action,'data':data},session_id='origin',turn_id='user-repair',enabled_toolsets=['muse'])
    assert len(raw.encode())<=cap
    result=json.loads(raw)
    assert result['ok'],result
    return result

def read_reference(result, field, expected):
    assert result['result']['result_is_reference']
    obj=bounded_call('content_read',result['result']['content_ref']['data'])['result']
    ref=obj['metadata_ref']['data'];parts=[];offset=0
    while offset is not None:
        page=bounded_call('content_read',{**ref,'offset':offset})['result']
        parts.append(page['body']);offset=page['next_offset']
    assert json.loads(''.join(parts))[field]==expected

normal=bounded_call('preferences',data)
assert 'revision' in normal and bounded_call('preferences',data)==normal
read_reference(normal,'blocked_topics',['x'*n])
feed_data=arguments(rt,'feed_add',{'event_key':'recover-big','title':'Fixture','content':'Synthetic only','sources':['x'*300000],'why':'Fixture'})
original_write=rt.store.write
def crash(kind,key,value):
    if kind=='mutation_request' and value.get('state')=='complete':
        raise RuntimeError('Synthetic interruption before journal commit')
    return original_write(kind,key,value)
with patch.object(rt.store,'write',side_effect=crash):
    assert not call('feed_add',feed_data)['ok']
recovered=bounded_call('feed_add',feed_data)
assert recovered['recovered'] and 'revision' in recovered
assert bounded_call('feed_add',feed_data)==recovered
read_reference(recovered,'sources',['x'*300000])
# A receipt persisted by a prior version must also be bounded on its first replay.
journal=revision(canonical([Views(rt.home).scope(),'feed_add',feed_data['request_id']]))
old=rt.store.read('mutation_request',journal)
row=rt.store.all('feed')[0]
legacy={**recovered,'result':row}
assert len(json.dumps(legacy).encode())>cap
rt.store.write('mutation_request',journal,{**old,'result':legacy})
replayed=bounded_call('feed_add',feed_data)
assert replayed==recovered and bounded_call('feed_add',feed_data)==replayed
assert rt.store.read('mutation_request',journal)['result']==replayed
assert len(rt.store.all('feed'))==1
read_reference(replayed,'sources',['x'*300000])
print(json.dumps({'final_envelope':'PASS','pending_recovery':'PASS','legacy_completed_replay':'PASS','references_paged_and_reconstructed':True,'one_feed_only':True,'provider_calls':0,'external_sends':0}))
native.close();rt.close()
