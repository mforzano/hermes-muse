"""Real native delegate child lifecycle and registry reads; scripted model body, no network."""
import os
import json
from pathlib import Path
import sys
import tempfile
import subprocess


def run(home):
    import hashlib
    import shutil
    import threading
    import time
    import uuid
    from types import SimpleNamespace
    from unittest.mock import patch
    repo = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repo), os.environ['HERMES_SOURCE']]
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\ndelegation:\n  max_concurrent_children: 2\n')
    shutil.copytree(repo, home/'plugins/hermes-muse', ignore=shutil.ignore_patterns('.git', '.serena', '__pycache__'))
    from hermes_cli.plugins import discover_plugins, invoke_hook
    from tools.registry import registry
    from model_tools import handle_function_call
    from hermes_state import SessionDB
    from agent.delegation_context import is_delegated_child_context
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_constants import get_hermes_home
    discover_plugins(force=True)
    rt = registry.get_entry('muse_manage').handler.__self__
    def call(action, data=None, sid='parent', task=None, ok=True):
        result = json.loads(handle_function_call('muse_manage', {'action':action,'data':data or {}},
                            session_id=sid, turn_id='user', task_id=task, tool_call_id='fixture-'+uuid.uuid4().hex, enabled_toolsets=['muse']))
        assert result.get('ok') is ok, (action, result)
        return result['result'] if ok else result
    def snapshot():
        return {str(p.relative_to(home)): hashlib.new('sha256',p.read_bytes()).hexdigest()
                for folder in ('muse','cron') for p in (home/folder).rglob('*') if p.is_file()}
    # A second actual temporary profile, with the same object ID but different data.
    from importlib import import_module
    package = rt.__class__.__module__.rsplit('.',1)[0]
    host_module = import_module(package+'.host')
    views_module = import_module(package+'.views')
    other = home/'profiles/other'
    other.mkdir(parents=True)
    with host_module.profile_scope(other):
        store = import_module(package+'.store').Store(other)
        host = host_module.HermesHost(store, plugin_root=home/'plugins/hermes-muse')
        host.initialize()
        service = import_module(package+'.service').Companion(store,host)
        service.signal('other-user','other-session','Create synthetic goal g')
        service.goal_create(dict(id='g',title='OTHER-PROFILE-PRIVATE',description='Other scope',completion_criteria='Other result',signal_id='other-user'))
        foreign_scope = views_module.Views(other).scope()
    def other_hashes():
        return {str(p.relative_to(other)):hashlib.new('sha256',p.read_bytes()).hexdigest() for p in other.rglob('*') if p.is_file()}
    other_before = other_hashes()
    native = SessionDB(db_path=home/'state.db')
    children = []
    try:
        invoke_hook('pre_llm_call', session_id='parent', turn_id='user', platform='cli', user_message='Review synthetic task evidence and goal g.')
        call('goal_create', dict(id='g',title='Synthetic goal',description='Inspect only',completion_criteria='Actual evidence reviewed',signal_id='user'))
        native.create_session('parent',source='cli',chat_type='private')
        native.append_message('parent','user','Original synthetic intent: read and compare, no external writes.')
        native.append_message('parent','assistant','History '+ 'h'*26000+'HISTORY-END')
        for n in range(70):
            native.append_message('parent','assistant', content='HIDDEN-REASONING-MUST-NOT-LEAK',tool_calls=[{'id':str(n),'function':{'name':'read_file','arguments':json.dumps({'file':'fixture-'+str(n)})}}])
            native.append_message('parent','tool','Fixture '+str(n)+' '+ 'x'*26000+' RESULT-END-'+str(n),tool_name='read_file',tool_call_id=str(n))
        native.create_session('group', source='telegram',chat_type='group')
        native.append_message('group','user','GROUP-SECRET')
        batch = call('operations_batch')
        assert len(batch['assignments']) == 2, batch
        assert all(a['dispatchable'] and a['review_mode']=='parallel' for a in batch['assignments'])
        assignments = {a['receipt']:a for a in batch['assignments']}
        barrier = threading.Barrier(2)
        results_lock = threading.Lock()
        read_counts = []
        class ScriptedChild:
            def __init__(self, n):
                self.session_id='worker-'+str(n)
                self.model='scripted-no-provider'
                self._delegate_depth=1
                self.closed=False
            def close(self): self.closed=True
            def interrupt(self,*args,**kwargs): pass
            def run_conversation(self, user_message, task_id=None, **kwargs):
                assert is_delegated_child_context(), 'Must be set by the actual native child runner'
                assert Path(get_hermes_home()) == home
                assignment=next(a for receipt,a in assignments.items() if receipt in user_message)
                before=snapshot()
                barrier.wait(timeout=15)
                def read(action,data=None,ok=True): return call(action,data,self.session_id,task_id,ok)
                for action in ('context','status'):
                    assert read(action)['read_semantics'].startswith('pure read')
                goal=read('content_read',{'kind':'goal','id':'g'})
                assert goal['metadata']['title']=='Synthetic goal'
                assert read('content_read',{'kind':'goal','id':'g','home':'foreign'},False)['code']=='invalid_argument'
                assert read('operations_evidence',{'receipt':assignment['receipt'],'profile':'other'},False)['code']=='invalid_argument'
                assert read('content_read',{'kind':'goal','id':'g','home':str(home/'profiles/other')},False)['code']=='invalid_argument'
                assert read('conversation_read',{'session_id':'group'},False)['code']=='permission_denied'
                for action,data in [('operations_commit',{'batch':batch['batch'],'reviews':[{'slot':assignment['slot'],'summary':'not permitted'}]}),
                                    ('content_write',{'kind':'note','id':'daily:2026-01-01','body':'Denied child write','expected_revision':'absent','request_id':'child-write-probe'}),('operations_read',{}),('operations_batch',{}),('research_status',{'id':'unknown'}),
                                    ('run_complete',{'outcome':'complete','summary':'not permitted'})]:
                    assert read(action,data,False)['code']=='permission_denied'
                history=read('conversation_read',{'session_id':'parent','since':0,'until':time.time()})
                full=''.join(m['text'] for m in history['messages'])
                while history.get('next_offset') is not None:
                    history=read('conversation_read',{'session_id':'parent','since':history['since'],'until':history['until'],'revision':history['revision'],'offset':history['next_offset']})
                    full+=''.join(m['text'] for m in history['messages'])
                assert 'HISTORY-END' in full and 'HIDDEN-REASONING' not in full and not history['coverage_recorded']
                receipt=assignment['receipt']; offset=0; refs=[]; pages=0
                while offset is not None:
                    index=read('operations_evidence',{'receipt':receipt,'offset':offset,'limit':3,'session_id':'parent','since':0,'until':time.time()})
                    assert not index['missing']
                    refs.extend(index['returned_refs']); offset=index['next_offset']; pages+=1
                assert pages>1 and refs
                assert read('operations_evidence',{'receipt':receipt,'session_id':'absent'})['total']==0
                assert read('operations_evidence',{'receipt':receipt,'since':0,'until':1})['total']==0
                bodies=[]; body_pages=0
                for ref in refs:
                    offset=0; chunks=[]
                    while offset is not None:
                        part=read('operations_evidence',{'receipt':receipt,'ref':ref,'offset':offset})
                        assert not part['missing']
                        chunks.append(part['text']); offset=part['next_offset']; body_pages+=1
                    bodies.append(json.loads(''.join(chunks)))
                assert body_pages>len(refs)
                assert all('RESULT-END-' in r['content'] for r in bodies if r['role']=='tool')
                assert 'HIDDEN-REASONING' not in json.dumps(bodies)
                assert before==snapshot(), 'Child reads/writes rejection changed Muse/Cron files'
                with results_lock: read_counts.append({'slot':assignment['slot'],'index_pages':pages,'body_pages':body_pages,'refs':len(refs)})
                summary={'batch':batch['batch'],'slot':assignment['slot'],'complete':True,'refs':refs,
                         'summary':'Read original intent and complete tool results through RESULT-END; synthetic read-only actions, no demonstrated mutation or unresolved failure.'}
                return {'final_response':json.dumps(summary),'completed':True,'api_calls':0,'messages':[]}
        def build(tasks, schemas, creds, **kwargs):
            for n in range(len(tasks)): children.append(ScriptedChild(n))
            return [(i,t,children[i]) for i,t in enumerate(tasks)],None
        parent=SimpleNamespace(valid_tool_names={'delegate_task'},session_id='parent',_delegate_depth=0,
                               model='fixture-parent',provider='custom',base_url='https://fixture.invalid/v1',api_key='synthetic',
                               api_mode='chat_completions',request_overrides=None)
        # Only construction/model work is scripted. Native dispatch, parallel executor,
        # _run_single_child, child context, registry, storage and cleanup are unmodified.
        from gateway.session_context import set_session_vars, clear_session_vars
        tokens = set_session_vars(async_delivery=False)
        try:
            with bind_subagent_parent(parent), patch('tools.delegate_tool._build_children',side_effect=build):
                result=call('delegate',{'tasks':[a['task'] for a in batch['assignments']]})
        finally:
            clear_session_vars(tokens)
        assert len(result.get('results',[]))==2 and all(r['status']=='completed' for r in result['results']),result
        conclusions=[json.loads(r['summary']) for r in result['results']]
        assert len(read_counts)==2 and all(c.closed for c in children)
        # Parent reads the referenced original evidence again before accepting conclusions.
        for conclusion in conclusions:
            assignment=next(a for a in assignments.values() if a['slot']==conclusion['slot'])
            index=call('operations_evidence',{'receipt':assignment['receipt'],'limit':100})
            assert set(conclusion['refs'])==set(index['returned_refs']) and not index['missing']
            for ref in conclusion['refs']:
                offset=0
                while offset is not None:
                    part=call('operations_evidence',{'receipt':assignment['receipt'],'ref':ref,'offset':offset})
                    assert not part['missing'];offset=part['next_offset']
        by_slot=sorted(conclusions,key=lambda r:r['slot'])
        later=call('operations_commit',{'batch':batch['batch'],'reviews':[{'slot':by_slot[1]['slot'],'summary':by_slot[1]['summary']}]})
        assert later['continuous_slots']==0
        first=call('operations_commit',{'batch':batch['batch'],'reviews':[{'slot':by_slot[0]['slot'],'summary':by_slot[0]['summary']}]})
        assert first['continuous_slots']==2 and first['reviewed_slots']==2
        assert first['remaining_slots']==1 and not first['batch_complete'], first
        with bind_subagent_parent(parent):
            rejected=call('delegate',{'tasks':[batch['assignments'][0]['task']]},ok=False)
            assert 'original dispatch' in rejected['error']
        assert other_hashes() == other_before
        print(json.dumps({'native_parallel_review':'PASS','native_children':2,'actual_parallel_barrier':True,'reads':read_counts,
                          'child_read_side_effects':False,'child_writes_denied':True,'cross_profile_inputs_denied':True,'actual_second_profile_unchanged':True,
                          'parent_contiguous_slots':first['continuous_slots'],'remaining_unreviewed_slots':first['remaining_slots'],'model_calls':0,'external_sends':0,'native_children_closed':True}))
    finally:
        native.close(); rt.close()


if __name__=='__main__':
    assert os.environ.get('TMPDIR')
    if len(sys.argv)>1 and sys.argv[1]=='--worker':
        run(Path(os.environ['HERMES_HOME']))
    else:
        with tempfile.TemporaryDirectory(prefix='muse-parallel-review-') as directory:
            env={**os.environ,'HERMES_HOME':directory,'HERMES_DATA_DIR_SUFFIX':'-muse-isolated-test',
                 'HERMES_IGNORE_USER_CONFIG':'1','HERMES_DISABLE_LAZY_INSTALLS':'1','PYTHONDONTWRITEBYTECODE':'1'}
            result=subprocess.run([sys.executable,__file__,'--worker'],env=env)
        # Parent cleans only its exact home, after native worker atexit finishes.
        print(json.dumps({'temporary_home_removed_after_worker_exit':not Path(directory).exists(),'services_started':0}))
        raise SystemExit(result.returncode)
