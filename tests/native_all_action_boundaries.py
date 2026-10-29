"""All-action input/transport boundaries plus real native-dispatch routing with fake outputs."""
import os,sys,json,shutil,time
from pathlib import Path
home=Path(os.environ['HERMES_HOME']);assert home.name.startswith('muse-independent-pages-')
repo=Path(__file__).resolve().parents[1];sys.path[:0]=[str(repo),str(repo/'tests'),os.environ['HERMES_SOURCE']]
(home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
shutil.copytree(repo,home/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git','__pycache__'))
from hermes_cli.plugins import discover_plugins,invoke_hook
from tools.registry import registry
from model_tools import handle_function_call
from hermes_state import SessionDB
discover_plugins(force=True);rt=registry.get_entry('muse_manage').handler.__self__
native=SessionDB(db_path=home/'state.db');native.create_session('origin',source='cli',chat_type='private');native.append_message('origin','user','ok')
invoke_hook('pre_llm_call',session_id='origin',turn_id='user-repair',platform='cli',user_message='ok')
def call(action,data):
    return json.loads(handle_function_call('muse_manage',{'action':action,'data':data},session_id='origin',turn_id='user-repair',enabled_toolsets=['muse']))
from unittest.mock import patch
from types import SimpleNamespace
import hashlib
from hermes_muse.runtime import ACTIONS, Runtime
from hermes_muse.contracts import FIELDS
from hermes_muse.responses import ARCHIVE, archive
from agent.delegation_context import delegated_child_context

assert set(ACTIONS)==set(FIELDS) and len(ACTIONS)==40
cap=256*1024
def raw_call(action,data,session='origin'):
    raw=handle_function_call('muse_manage',{'action':action,'data':data},session_id=session,turn_id='user-repair',enabled_toolsets=['muse'])
    assert len(raw.encode())<=cap,(action,len(raw.encode()))
    return json.loads(raw)
def hashes():
    result = {str(p.relative_to(rt.store.root)): hashlib.new('sha256', p.read_bytes()).hexdigest()
              for p in rt.store.root.rglob('*') if p.is_file()}
    assert result, 'Fixture must contain actual initialized Muse files'
    return result
before=hashes()
# Every real registry action rejects before journal/track/bind/dispatch, not a serializer stub.
with patch.object(rt.service.operations,'track',side_effect=AssertionError('preflight must precede track')), patch.object(rt.ctx,'dispatch_tool',side_effect=AssertionError('no dispatch')):
    for action in ACTIONS:
        reply=raw_call(action,{'body':'x'*600000})
        assert not reply['ok'] and reply['code']=='size_limit',(action,reply)
after=hashes()
assert after==before, {'changed_files': [k for k in set(before)|set(after) if before.get(k)!=after.get(k)]}
# All action success/error shapes share the final serializer. Synthetic envelopes,
# separate from the real business-path assertions below.
for action in ACTIONS:
    token=ARCHIVE.set(lambda value: archive(rt.store,'origin',action,value))
    try:
        for success in (True,False):
            envelope={'ok':success,'result':{'payload':'x'*300000}} if success else {'ok':False,'error':'Synthetic failure','native':{'details':'x'*300000}}
            raw=Runtime._receipt(envelope,action=action)
            reply=json.loads(raw)
            assert len(raw.encode())<=cap and reply['ok']==success
    finally:
        ARCHIVE.reset(token)

def full(ref,session='origin'):
    offset=0;parts=[];revision=None
    while offset is not None:
        data={**ref['data'],'offset':offset}
        if revision:data['revision']=revision
        page=raw_call(ref['action'],data,session)['result']
        # Existing object refs may first point to pageable metadata.
        if page.get('metadata_ref'):
            return full(page['metadata_ref'],session)
        parts.append(page.get('body') or '')
        offset=page['next_offset'];revision=page['revision']
    return json.loads(''.join(parts))
# A nested pure read must not inherit an enclosing write's archive callback.
prior=ARCHIVE.set(lambda value: (_ for _ in ()).throw(AssertionError('read attempted archive')))
try:
    before=hashes()
    with patch.object(rt, '_handle', return_value=json.dumps({'ok': True, 'result': {'payload': 'x'*300000}})):
        denied=raw_call('feed_list',{})
    assert not denied['ok'] and denied['code']=='size_limit'
    assert hashes()==before
finally:
    ARCHIVE.reset(prior)
parent=SimpleNamespace(valid_tool_names={'delegate_task'})
calls=[]
def dispatch(name,args,**kw):
    calls.append(name)
    return json.dumps(native_result)
with patch('agent.subagent_lifecycle.get_active_subagent_parent',return_value=parent), patch.object(rt.ctx,'dispatch_tool',side_effect=dispatch):
    native_result={'error':'Synthetic failure','details':'x'*300000}
    error_data={'goal':'Synthetic','request_id':'error-replay'}
    error=raw_call('delegate',error_data)
    assert not error['ok'] and full(error['error_ref'])['native']['details']=='x'*300000
    assert raw_call('delegate',error_data)==error and len(calls)==1
    denied=raw_call('content_read',error['error_ref']['data'],session='other')
    assert not denied['ok'] and denied['code']=='permission_denied'
    denied=raw_call('content_read',{**error['error_ref']['data'],'home':'foreign'})
    assert not denied['ok'] and denied['code']=='invalid_argument'
    native_result={'status':'completed','results':[{'status':'completed','result':'x'*300000}]}
    data={'goal':'Synthetic','request_id':'success-replay'}
    result=raw_call('delegate',data)
    assert result['ok'] and full(result['result']['content_ref'])['result']==native_result
    assert raw_call('delegate',data)==result and len(calls)==2
    assert raw_call('delegate',{**data,'goal':'Changed'})['code']=='idempotency_conflict'
    research_data={'items':['Synthetic'],'request_id':'research-replay'}
    research=raw_call('research_start',research_data)
    assert research['ok'] and raw_call('research_start',research_data)==research and len(calls)==3
    observed=full(research['result']['content_ref'])
    assert observed['dispatch']==native_result
    status=raw_call('research_status',{'id':research['result']['id']})
    assert full(status['result']['content_ref'])['result']['native']==native_result
    assert len(calls)==3
    # Crash after native completion but before final replay receipt: do not redispatch.
    original=rt.store.write
    def crash(kind,key,value):
        if kind=='mutation_request' and value.get('state')=='complete':
            raise RuntimeError('Synthetic final journal interruption')
        return original(kind,key,value)
    with patch.object(rt.store,'write',side_effect=crash):
        assert not raw_call('delegate',{'goal':'Synthetic','request_id':'interrupted'})['ok']
    assert len(calls)==4
    assert raw_call('delegate',{'goal':'Synthetic','request_id':'interrupted'})['code']=='outcome_unknown'
    assert len(calls)==4
with delegated_child_context('fixture-child'):
    rejected=raw_call('delegate',{'goal':'Synthetic','request_id':'child'})
    assert not rejected['ok'] and rejected['code']=='permission_denied'
# Invalid checkpoint is refused without cursor/last_review mutation.
before=rt.store.read('cursor','feed-pulse',0)
assert not raw_call('review_complete',{'job':'feed-pulse','up_to':time.time(),'through':time.time(),'summary':'Synthetic','window':'x'*300000})['ok']
assert rt.store.read('cursor','feed-pulse',0)==before
print(json.dumps({'actions':list(ACTIONS),'all_40_real_oversized_input_no_changes':True,'all_40_final_success_error_envelopes_bounded':True,'native_errors_readable':True,'delegate_replay_no_duplicate':True,'pending_unknown_never_redispatched':True,'research_status_paged':True,'cross_session_scope_child_denied':True,'synthetic_dispatch_calls':len(calls),'real_delegate_calls':0,'external_sends':0}))
native.close();rt.close()
