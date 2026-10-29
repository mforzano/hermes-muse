"""Native substrate and reload checks for authored scope plans; no model or connectors."""
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
# Plans below are authored fixtures, not inferred by a model or a new plugin policy engine.
from cron.jobs import list_jobs, update_job, pause_job, get_job
from hermes_muse.host_v1 import profile_scope
from native_calendar_smoke import run as calendar_checks
assert not rt.store.goals(), 'Empty goals must not prevent the calendar tool path'
calendar_checks(registry.get_entry('muse_manage'),rt)
assert not rt.store.all('notification') and not rt.store.all('source_check')
source={'connection':'fixture-market','account':'research','resource':'events'}
started=time.time()-30
base={'source':source,'scope':'Declared market events','started_at':started,'status':'checked',
      'summary':'Synthetic market source checked','expected_resources':['events'],'checked_resources':['events'],'cursor':'good'}
def check(data):
    result=call('source_check',arguments(rt,'source_check',data))
    assert result['ok'],result
    return result['result']
market=check(base)
# Specialist fixture executes its authored market-only plan. There is no connector call
# for excluded personal calendars/messages, no corresponding source row or gap notice.
assert [r['source']['connection'] for r in rt.store.all('source_check')]==['fixture-market']
assert not rt.store.all('notification')
missing=check({**base,'source_id':market['id'],'started_at':started+1,'status':'unavailable',
               'checked_resources':[],'summary':'Restore the existing authorized connector before protected reads','cursor':'not-success'})
assert missing['last_success']==market['last_success'] and missing['cursor']=='good'
from cron.executions import create_execution, mark_execution_running
job_id=rt.host.manifest()['fixed']['proactive-watch']
attempt=create_execution(job_id,source='test');mark_execution_running(attempt['id'])
partial=json.loads(handle_function_call('muse_manage',{'action':'run_complete','data':{'outcome':'partial',
    'summary':'In-scope connection unavailable', 'gaps':['Restore the existing authorized connector before protected reads']}},
    session_id='origin',turn_id='user-repair',task_id='cron:'+job_id+':'+attempt['id'],enabled_toolsets=['muse']))
assert partial['ok'] and partial['result']['status']=='partial'
assert 'Restore' in partial['result']['gaps'][0]

failure=check({**base,'source_id':market['id'],'started_at':started+2,'status':'error',
               'checked_resources':[],'summary':'Transient timeout; retry this run after cooldown','cursor':'not-success'})
assert failure['last_success']==market['last_success'] and failure['cursor']=='good'
recovered=check({**base,'source_id':market['id'],'started_at':started+3,'summary':'Recovered within this run','cursor':'recovered'})
assert recovered['last_success']==started+3 and not rt.store.all('notification')
# Explicit one-item exception has a distinct collection scope, not a personal sweep.
one=check({**base,'source':{'connection':'fixture-requested-item','account':'private','resource':'accepted-event'},
           'scope':'Only the explicitly accepted event','expected_resources':['event-1'],'checked_resources':['event-1']})
assert one['last_success_resources']==['event-1']
assert len(rt.store.all('source_check'))==2
prefs=rt.store.path('PROACTIVE_PREFERENCES.md')
text=prefs.read_text().split('## Coverage scope')[0]+'## Coverage scope\n\nMarket research only. Exclude personal calendars and personal messages; explicit accepted goals cover only their specific item.\n'
prefs.write_text(text);saved=prefs.read_bytes()
with profile_scope(rt.home):
    jobs=list_jobs(include_disabled=True)
    assert len(jobs)==6
    original=rt.host.manifest()
    patrol=original['fixed']['proactive-watch']
    marker=original['jobs'][patrol]['marker']
    update_job(patrol,{'prompt':marker+'\nTemporary fixture prompt patch'})
    pause_job(original['fixed']['feed-pulse'],reason='User fixture pause')
    before={j['id']:{k:j.get(k) for k in ('schedule','enabled','deliver')} for j in list_jobs(include_disabled=True)}
    rt.host.initialize();rt.host.initialize()
    after={j['id']:{k:j.get(k) for k in ('schedule','enabled','deliver')} for j in list_jobs(include_disabled=True)}
    assert before==after and rt.host.manifest()['installation_id']==original['installation_id']
    assert 'Temporary fixture prompt patch' not in get_job(patrol)['prompt']
    assert 'coverage plan' in get_job(patrol)['prompt']
assert prefs.read_bytes()==saved
print(json.dumps({'coverage_fixture':'PASS','model_policy_execution':False,'general_30_day_day8_day30_calendar_path':True,
 'specialist_authored_plan_no_personal_probe_or_source_check_or_gap_notice':True,'missing_and_error_preserve_watermark':True,
 'same_run_recovery_without_notice':True,'accepted_item_only':True,'empty_goals_still_calendar':True,
 'reload_preserves_preferences_bytes_ids_schedules_pauses_targets_install_id':True,'fixed_jobs':6,'template_prompt_refreshed':True,
 'real_connector_calls':0,'provider_calls':0,'external_sends':0}))
native.close();rt.close()
