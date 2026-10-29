"""Actual registry/hooks in two disposable profile homes; no LLM or transport sends."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

assert os.environ.get('TMPDIR')
os.environ.update(HERMES_DATA_DIR_SUFFIX='-muse-isolated-test', HERMES_IGNORE_USER_CONFIG='1', HERMES_DISABLE_LAZY_INSTALLS='1')
repo = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(repo), os.environ['HERMES_SOURCE']]
operation = sys.argv[1] if len(sys.argv) > 1 else 'disable'
assert operation in {'disable', 'remove'}
with tempfile.TemporaryDirectory(prefix='muse-boundary-') as temporary:
    base = Path(temporary)
    home = base / 'profiles' / ('orchid' if operation == 'disable' else 'cedar')
    home.mkdir(parents=True)
    os.environ['HERMES_HOME'] = str(home)
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    shutil.copytree(repo, home/'plugins/hermes-muse', ignore=shutil.ignore_patterns('.git', '__pycache__'))
    from hermes_cli.plugins import discover_plugins, invoke_hook
    from tools.registry import registry
    from model_tools import handle_function_call
    from hermes_state import SessionDB
    from cron.jobs import list_jobs, pause_job, get_job
    discover_plugins(force=True)
    rt = registry.get_entry('muse_manage').handler.__self__
    package = rt.__class__.__module__.rsplit('.', 1)[0]
    host_module = importlib.import_module(package+'.host_v1')
    Store = importlib.import_module(package+'.store').Store
    other = base/'profiles'/'untouched'
    other.mkdir(parents=True)
    (other/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    with host_module.profile_scope(other):
        other_store = Store(other)
        other_host = host_module.HermesHost(other_store, plugin_root=home/'plugins/hermes-muse')
        other_host.initialize()
    def hashes(path):
        return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in path.rglob('*') if p.is_file()}
    untouched = hashes(other)
    turn = 'initial'
    def user(key, text):
        global turn
        turn = key
        invoke_hook('pre_llm_call', session_id='origin', turn_id=turn, platform='cli', user_message=text)
    def call(action, data=None, ok=True):
        result = json.loads(handle_function_call('muse_manage', {'action': action, 'data': data or {}},
                            session_id='origin', turn_id=turn, tool_call_id=uuid.uuid4().hex, enabled_toolsets=['muse']))
        assert result['ok'] is ok, result
        return result['result'] if ok else result
    def receipt(key, action='add', result=None, source_turn=None):
        invoke_hook('post_tool_call', tool_name='memory', args={'action': action},
                    result={'success': True} if result is None else result, session_id='origin',
                    turn_id=source_turn or turn, tool_call_id=key, status='success')
        return next(r for r in rt.store.all('memory_feedback') if r['tool_call_id'] == key)
    def review(row, request, ok=True):
        return call('memory_feedback_review', dict(receipt_id=row['id'], decision='inform', reason='Synthetic observed receipt',
                    expected_revision=row['revision'], request_id=request), ok=ok)
    try:
        user('remember', 'Remember these two preferences.')
        first, second = receipt('save-one'), receipt('save-two')
        assert {first['id'], second['id']} <= {r['id'] for r in rt.memory_feedback.candidates('origin')}
        review(first, 'inform-original')
        staged = receipt('waiting', result={'success': True, 'staged': True, 'pending_id': 'pending-fixture'})
        user('forget', 'Forget that preference now.')
        receipt('remove', 'remove')
        assert first['id'] not in {r['id'] for r in rt.memory_feedback.candidates('origin')}
        assert review(first, 'inform-original', False)['code'] == 'scope_closed'  # replay also refused
        assert review(second, 'inform-old', False)['code'] == 'scope_closed'
        assert review(staged, 'still-pending')['classified_state'] == 'staged'
        late = receipt('late', source_turn='remember')
        assert late['invalidated'] and late['classified_state'] == 'unknown'
        user('replace', 'Replace the remembered preference.')
        before = receipt('before-replace')
        after = receipt('replacement', 'replace')
        assert review(before, 'same-turn-obsolete', False)['code'] == 'scope_closed'
        assert review(after, 'current-replacement')['classified_state'] == 'saved'
        user('later', 'A later ordinary conversation.')
        assert review(after, 'later-replacement', False)['code'] == 'scope_closed'
        assert staged['id'] in {r['id'] for r in rt.memory_feedback.candidates('origin')}

        native = SessionDB(db_path=home/'state.db')
        try:
            native.create_session('origin', source='cli', chat_type='private')
            native.append_message('origin', 'user', 'Synthetic audit intent: inspect only')
            native.append_message('origin', 'assistant', tool_calls=[{'id': 'read-fixture', 'function': {'name': 'read_file', 'arguments': '{}'}}])
            native.append_message('origin', 'tool', 'x'*26000+'FULL-EVIDENCE-END', tool_name='read_file', tool_call_id='read-fixture')
            batch = call('operations_batch')
            assignment = batch['assignments'][0]
            assert assignment['dispatchable'] is True and assignment['review_mode'] == 'parallel'
            from agent.delegation_context import delegated_child_context
            with delegated_child_context("audit-reader"):
                assert call('operations_evidence', {'receipt': assignment['receipt']})['receipt'] == assignment['receipt']
                assert call('operations_commit', {'batch': batch['batch'], 'reviews': [{'slot':assignment['slot'], 'summary':'child cannot commit'}]}, False)['code'] == 'permission_denied'
            index = call('operations_evidence', {'receipt': assignment['receipt']})
            assert not index['missing']
            texts = []
            for ref in index['expected_refs']:
                offset = 0
                while offset is not None:
                    part = call('operations_evidence', {'receipt': assignment['receipt'], 'ref': ref, 'offset': offset})
                    assert not part['missing']
                    texts.append(part['text']); offset = part['next_offset']
            assert 'FULL-EVIDENCE-END' in ''.join(texts)
            result = call('operations_commit', {'batch': batch['batch'], 'reviews': [{'slot': assignment['slot'], 'summary': 'Parent read full original intent and complete synthetic read result, no mutation or unresolved failure.'}]})
            assert result['newly_reviewed_slots'] == 1 and result['continuous_slots'] == 1
        finally:
            native.close()

        data = dict(source={'connection': 'canonical-tool', 'account': 'account', 'resource': 'mail'},
                    scope='Synthetic mail', started_at=time.time()-1, status='checked', summary='canonical',
                    expected_resources=['mail'], checked_resources=['mail'])
        canonical = call('source_check', {**data, 'expected_revision': 'absent', 'request_id': 'source-new'})
        def revision(): return call('content_read', {'kind': 'source_check', 'id': canonical['id']})['revision']
        rev = revision()
        alias = {**data, 'source': {**data['source'], 'connection': 'alias-tool'}, 'source_id': canonical['id'], 'alias_reason': 'Same observed source', 'summary': 'alias first'}
        first_alias = call('source_check', {**alias, 'expected_revision': rev, 'request_id': 'alias-first'})
        assert first_alias['id'] == canonical['id'] and first_alias['summary'] == 'alias first'
        for stale in ('absent', rev):
            assert call('source_check', {**alias, 'expected_revision': stale, 'request_id': uuid.uuid4().hex}, False)['code'] == 'revision_conflict'
        bound = {k:v for k,v in alias.items() if k not in {'source_id','alias_reason'}}
        rev = revision()
        updated = call('source_check', {**bound, 'summary': 'bound without explicit target', 'expected_revision': rev, 'request_id': 'bound'})
        assert updated['id'] == canonical['id']
        with rt.store.transaction() as db:
            alias_key = db.execute("SELECT id FROM records WHERE kind='source_alias'").fetchone()[0]
        chained = {**alias, 'source': {**alias['source'], 'connection': 'second-alias'}, 'source_id': alias_key}
        assert call('source_check', {**chained, 'expected_revision': revision(), 'request_id': 'via-existing-alias'})['id'] == canonical['id']
        # Two native registry writers hold the same actual canonical revision.
        rev = revision()
        def writer(n):
            return json.loads(handle_function_call('muse_manage', {'action':'source_check', 'data':{**bound, 'summary':str(n), 'expected_revision':rev,'request_id':'race-'+str(n)}}, session_id='origin', turn_id=turn, tool_call_id='race-'+str(n), enabled_toolsets=['muse']))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, range(2)))
        assert sum(r['ok'] for r in results) == 1, results
        assert [r['code'] for r in results if not r['ok']] == ['revision_conflict']
        for field in ('account', 'resource'):
            bad = {**alias, 'source': {**alias['source'], field: 'other'}, 'expected_revision': revision(), 'request_id': 'wrong-'+field}
            assert not call('source_check', bad, False)['ok']

        jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 6 and all(j['enabled'] for j in jobs)
        invalid = ['Please remove the obsolete draft file from this project.', 'Do not disable Muse.',
                   'Please disable Muse tomorrow.', 'How do I disable Muse?', '"Please disable Muse now."',
                   '请停用Muse，但现在不执行', '明天再停用Muse', '怎么停用Muse', '请不要卸载Muse']
        for n, text in enumerate(invalid):
            user('invalid-'+str(n), text)
            rev = call('content_read', {'kind':'installation','id':'current'})['revision']
            result = call('installation_prepare', dict(operation=operation, signal_id=turn, expected_revision=rev, request_id='invalid-'+str(n)), False)
            assert result['code'] == 'permission_denied'
            assert all(j['enabled'] for j in list_jobs(include_disabled=True))
        user('mismatch', 'Please uninstall Muse now.' if operation=='disable' else '请停用Muse插件')
        rev = call('content_read', {'kind':'installation','id':'current'})['revision']
        assert call('installation_prepare', dict(operation=operation, signal_id=turn, expected_revision=rev, request_id='mismatch'), False)['code']=='permission_denied'
        pause_job(jobs[0]['id'], reason='Preexisting synthetic user pause')
        user('authorized', 'Please disable Muse now.' if operation=='disable' else '请卸载Muse插件')
        rev = call('content_read', {'kind':'installation','id':'current'})['revision']
        prep = dict(operation=operation, signal_id=turn, expected_revision=rev, request_id='authorized')
        failed = [False]
        def fail_once(key, **kwargs):
            if not failed[0]:
                failed[0] = True
                raise OSError('Synthetic interruption')
            return pause_job(key, **kwargs)
        with patch('cron.jobs.pause_job', side_effect=fail_once):
            partial = call('installation_prepare', prep)
        assert partial['partial'] and len(partial['remaining_ids']) == 1
        complete = call('installation_prepare', prep)
        assert not complete['partial'] and len(complete['paused_owned_ids']) == 5
        assert jobs[0]['id'] in complete['unchanged_ids']
        assert call('installation_prepare', prep)['paused_owned_ids'] == complete['paused_owned_ids']
        assert all(not get_job(j['id'])['enabled'] for j in jobs)
        assert hashes(other) == untouched
        print(json.dumps({'boundary_regressions':'PASS', 'profile':home.name, 'operation':operation,
                          'memory_obsolete_replay_refused':True, 'staged_preserved':True, 'parent_audit_full_read':True,
                          'source_alias_canonical_cas_and_concurrency':True, 'other_profile_unchanged':True,
                          'prepare_partial_retry':True, 'model_calls':0, 'external_sends':0}))
    finally:
        rt.close()
print(json.dumps({'temporary_profiles_removed':not base.exists(), 'owned_services_started':0}))
