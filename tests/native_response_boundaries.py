"""Regression of the independent hidden-coverage and singleton receipt probes."""
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
from tool_client import arguments
discover_plugins(force=True);rt=registry.get_entry('muse_manage').handler.__self__
native=SessionDB(db_path=home/'state.db');native.create_session('origin',source='cli',chat_type='private');native.append_message('origin','user','ok')
invoke_hook('pre_llm_call',session_id='origin',turn_id='user-repair',platform='cli',user_message='ok')
def call(action,data):
    return json.loads(handle_function_call('muse_manage',{'action':action,'data':data},session_id='origin',turn_id='user-repair',enabled_toolsets=['muse']))
data=arguments(rt,'preferences',{'signal_id':'user-repair','blocked_topics':['x'*300000]})
r=call('preferences',data);replay=call('preferences',data)
assert r['ok'] and replay==r and r['result']['result_is_reference']
ref=r['result']['content_ref']['data'];assert ref=={'kind':'preferences','id':'current'}
record=call('content_read',ref)['result'];ref=record['metadata_ref']['data'];parts=[];offset=0
while offset is not None:
    p=call('content_read',{**ref,'offset':offset})['result'];parts.append(p['body']);offset=p['next_offset']
assert json.loads(''.join(parts))['blocked_topics']==['x'*300000]
print(json.dumps({'preferences_committed_success_replay':True,'paged_chars':300000}))
# Seed native private sessions and normal bounded notice; force the unreviewed-conversation gate.
for i in range(1000):
    sid=f'private-{i:04}'
    native.create_session(sid,source='cli',chat_type='private');native.append_message(sid,'user','Private correction')
    rt.store.write('session',sid,{'id':sid,'turn_id':sid,'active':False})
notice=call('notification_add',arguments(rt,'notification_add',{'event_key':'hidden-evidence','message':'Synthetic only','sources':['fixture:source'],'rationale':'Fixture','verified_at':time.time(),'expires':time.time()+3600}))['result']
assert not rt.store.all('conversation_window')
# Reach the persisted gate through the real preparation path; blocked before any dispatch.
assert rt.service.delivery_text(notice['id'],delivery={'job_id':'fixture-patrol','execution_id':'fixture-attempt'})==''
assert rt.store.read('notification',notice['id'])['delivery_gate']['blocked_by']==['conversation_changed_or_unreviewed']
assert not any(w['ranges'] for w in rt.store.all('conversation_window'))
for action in ('notification_prepare','notification_queue','notification_handoff'):
    r=call(action,arguments(rt,action,{'id':notice['id']}))
    assert r['ok'] and len(r['result']['read'])==10 and r['result']['unread_sessions']>=1000,r
    assert len(json.dumps(r).encode())<4096
    assert not any(w['ranges'] for w in rt.store.all('conversation_window'))
descriptors={}
listing=call('context',{'section':'conversations','limit':100})['result']
while True:
    descriptors.update({d['session_id']:d for d in listing['items']})
    if not listing['next_cursor']:break
    listing=call('context',{'section':'conversations','limit':100,'cursor':listing['next_cursor']})['result']
complete=sum(1 for sid,d in descriptors.items() if sid.startswith('private-') and d['complete'])
assert complete==0
revisions=rt.service.conversation_revisions(descriptors)
review_data={'id':notice['id'],'conversation_revisions':revisions,'verdict':'relevant','reason':'Synthetic gate probe'}
review=call('notification_review',arguments(rt,'notification_review',review_data))
assert review['ok'] and review['result']['reviewed'] is False,review
assert not rt.store.read('notification',notice['id']).get('conversation_review')
for sid,d in descriptors.items():
    offset=0
    while offset is not None:
        p=call('conversation_read',{'session_id':sid,'revision':d['revision'],'offset':offset})
        assert p['ok'],p
        assert p['result'].get('messages') is not None
        offset=p['result']['next_offset']
review=call('notification_review',arguments(rt,'notification_review',review_data))
assert review['ok'] and review['result']['reviewed'] is True,review
assert rt.store.read('notification',notice['id'])['conversation_review']['verdict']=='relevant'
print(json.dumps({'native_notification_coverage':'PASS','sessions':len(descriptors),'aliases':3,'descriptor_only_review_rejected':True,'body_pages_then_review':True,'provider_calls':0,'external_sends':0}))
native.close();rt.close()
