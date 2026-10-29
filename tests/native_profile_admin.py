"""Actual authenticated host routes in isolated custom homes: profile selection is read-only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import json


def worker(home):
    import shutil
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    repo=Path(__file__).resolve().parents[1]
    sys.path[:0]=[str(repo),os.environ['HERMES_SOURCE']]
    (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\n  isolation: in-process\ntimezone: UTC\n')
    source=home/'plugins/hermes-muse'
    source.mkdir(parents=True)
    files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=repo,text=True).splitlines()
    for name in files:
        if not (repo/name).is_file():continue
        (source/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(repo/name,source/name)
    subprocess.run(['git','init','-q','-b','main'],cwd=source,check=True)
    subprocess.run(['git','add','.'],cwd=source,check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','commit','-qm','synthetic tested source'],cwd=source,check=True)
    from hermes_muse.store import Store
    from hermes_muse.host import HermesHost,profile_scope
    from hermes_muse.service import Companion
    default_store=Store(home); default_host=HermesHost(default_store,source);default_host.initialize()
    with profile_scope(home):
        from cron.jobs import list_jobs
        assert all(j.get("workdir") is None for j in list_jobs(True))
    default_service=Companion(default_store,default_host)
    default_service.signal('fixture','default-session','Maintain synthetic goal default-goal')
    default_service.goal_create(dict(id='default-goal',signal_id='fixture',title='Default private fixture',description='Default only',completion_criteria='Read'))
    target=home/'profiles/cedar';target.mkdir(parents=True)
    (target/'config.yaml').write_text('plugins:\n  enabled: []\ntimezone: UTC\nterminal:\n  backend: docker\n')
    other=home/'profiles/orchid';other.mkdir(parents=True)
    (other/'config.yaml').write_text('plugins:\n  enabled: []\ntimezone: UTC\n')
    from cron.jobs import list_jobs,create_job,pause_job
    with profile_scope(target):
        user_job=create_job(prompt='Synthetic user subscription',schedule='every 2h',name='user-owned',deliver='local')
    def hashes(path):return {str(p.relative_to(path)):hashlib.new('sha256',p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts}
    protected=hashes(other)
    from hermes_cli.web_server import app
    from fastapi.testclient import TestClient
    client=TestClient(app,base_url='http://127.0.0.1')
    headers={'Authorization':'Bearer isolated-profile-admin'}
    base='/api/plugins/hermes-muse'
    def profiles():
        r=client.get(base+'/profiles?profile=default',headers=headers)
        assert r.status_code==200,r.text
        return {x['name']:x for x in r.json()['profiles']}
    def post(name):
        return client.post(base+'/profiles/'+name+'?profile=default',headers=headers,json={'operation':'enable','expected_revision':'x','request_id':'k'})
    assert client.get(base+'/profiles?profile=default').status_code==401
    rows=profiles();assert set(rows)=={'default','cedar','orchid'}
    assert all(r['read_only'] and r['managed_by']=='Hermes' for r in rows.values())
    assert not rows['cedar']['installed'] and not (target/'muse').exists()
    before={name:hashes(path) for name,path in (('default',home),('cedar',target),('orchid',other))}
    # No lifecycle route: the page cannot install, enable or disable Muse in any profile.
    for name in ('default','cedar','orchid'):
        assert post(name).status_code in {404,405},name
    assert not any(r.path.startswith(base+'/profiles/') for r in app.routes if 'POST' in getattr(r,'methods',()))
    assert {name:hashes(path) for name,path in (('default',home),('cedar',target),('orchid',other))}==before
    assert client.get(base+'/goals?profile=default&selected_profile=cedar',headers=headers).status_code==404
    # The profile owner installs Muse with Hermes plugin management; the page then reads it.
    shutil.copytree(source,target/'plugins/hermes-muse',ignore=shutil.ignore_patterns('.git'))
    (target/'config.yaml').write_text('plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\n')
    with profile_scope(target):
        store=Store(target); host=HermesHost(store,target/'plugins/hermes-muse');host.initialize()
        assert all(j.get("workdir") is None for j in list_jobs(True))
        service=Companion(store,host);service.signal('fixture','cedar-session','Maintain synthetic goal default-goal')
        service.goal_create(dict(id='default-goal',signal_id='fixture',title='Cedar private fixture',description='Cedar only',completion_criteria='Read'))
    rows=profiles();assert rows['cedar']['enabled'] and rows['cedar']['read_only']
    # Concurrent profile GETs must not borrow process/global caches or content.
    def get(name):
        r=client.get(base+'/goals/default-goal?profile=default&selected_profile='+name,headers=headers)
        assert r.status_code==200,r.text
        return r.json()
    env_before=dict(os.environ)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(get,['default','cedar']*5))
    assert [v['metadata']['title'] for v in values]==['Default private fixture','Cedar private fixture']*5
    assert os.environ==env_before
    assert all('scope_token' not in value for value in values)
    assert hashes(other)==protected
    print(json.dumps({'profile_admin':'PASS','lifecycle_route_absent':True,'all_profiles_read_only':True,'custom_home':True,'concurrent_profile_scope_no_env_mutation':True,'other_profile_unchanged':True,'no_model_calls':True}))

if __name__=='__main__':
    assert os.environ.get('TMPDIR')
    if len(sys.argv)>1:
        worker(Path(os.environ['HERMES_HOME']))
    else:
        with tempfile.TemporaryDirectory(prefix='muse-admin-fixture-') as directory:
            env={**os.environ,'HERMES_HOME':directory,'TMPDIR':directory,'HERMES_DATA_DIR_SUFFIX':'-muse-isolated-test','HERMES_IGNORE_USER_CONFIG':'1','HERMES_DISABLE_LAZY_INSTALLS':'1','PYTHONDONTWRITEBYTECODE':'1','HERMES_DASHBOARD_SESSION_TOKEN':'isolated-profile-admin'}
            result=subprocess.run([sys.executable,__file__,'--worker'],env=env)
        print(json.dumps({'owned_home_removed_after_process_exit':not Path(directory).exists(),'services_started':0}))
        raise SystemExit(result.returncode)
