"""Real authenticated Hermes routes and two isolated profiles; no model/network writes."""

import os
import sys
import tempfile
from pathlib import Path
import shutil
import hashlib
import json

os.environ["HERMES_DATA_DIR_SUFFIX"] = "-muse-isolated-test"
assert os.environ.get("TMPDIR"), "Set TMPDIR to an isolated scratch directory"
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, os.environ["HERMES_SOURCE"])


def snapshot(home):
    return {
        str(p.relative_to(home)): hashlib.new("sha256", p.read_bytes()).hexdigest()
        for folder in ("muse", "cron", "plugins")
        for p in (home / folder).rglob("*")
        if p.is_file()
    }


with tempfile.TemporaryDirectory(prefix="muse-native-views-") as temporary:
    home = Path(temporary)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_DASHBOARD_SESSION_TOKEN"] = "isolated-fixture-token"
    from hermes_muse.host import HermesHost, profile_scope
    from hermes_muse.store import Store
    from hermes_muse.service import Companion

    for target, title in [(home, "ALPHA"), (home / "profiles/beta", "BETA")]:
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.yaml").write_text(
            "plugins:\n  enabled: [hermes-muse]\n  isolation: in-process\ntimezone: UTC\n"
        )
        shutil.copytree(
            root,
            target / "plugins/hermes-muse",
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        with profile_scope(target):
            store = Store(target)
            host = HermesHost(store, plugin_root=target / "plugins/hermes-muse")
            host.initialize()
            service = Companion(store, host)
            service.signal("signal", "fixture-session", "Create goal fixture")
            service.goal_create(
                {
                    "id": "same-id",
                    "signal_id": "signal",
                    "title": title,
                    "description": "Synthetic",
                    "completion_criteria": "Synthetic fixture",
                }
            )
            if title == "ALPHA":
                service.goal_create({"id": "only-alpha", "signal_id": "signal", "title": title,
                                     "description": "Synthetic", "completion_criteria": "Profile isolation"})
            for n in range(103):
                service.feed_add(
                    {
                        "event_key": str(n),
                        "title": title + " " + str(n),
                        "content": "Synthetic body " + str(n),
                        "sources": ["fixture:source"],
                        "why": "Fixture",
                        "goal_id": "same-id",
                    }
                )
    from hermes_cli.web_server import app
    from fastapi.testclient import TestClient

    # No lifespan startup: the real middleware/router runs, no gateway/health workers.
    client = TestClient(app, base_url="http://127.0.0.1")
    prefix = "/api/plugins/hermes-muse"
    assert client.get(prefix + "/feed").status_code == 401
    headers = {"Authorization": "Bearer isolated-fixture-token"}
    before = [snapshot(home), snapshot(home / "profiles/beta")]
    a = client.get(prefix + "/goals/same-id", headers=headers)
    assert a.status_code == 200, a.text
    b = client.get(prefix + "/goals/same-id?profile=beta", headers=headers)
    assert b.status_code == 200, b.text
    assert (
        a.json()["metadata"]["title"] == "ALPHA"
        and b.json()["metadata"]["title"] == "BETA"
    )
    assert 'scope_token' not in a.json() and 'scope_token' not in b.json()
    assert client.get(prefix+'/goals/only-alpha',params={'profile':'beta'},headers=headers).status_code==404
    ids = []
    cursor = None
    while True:
        response = client.get(
            prefix + "/feed",
            params={"limit": 25, **({"cursor": cursor} if cursor else {})},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()
        ids.extend(x["id"] for x in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == 103
    assert before == [snapshot(home), snapshot(home / "profiles/beta")]
    (home / "profiles/beta/config.yaml").write_text(
        "plugins:\n  enabled: []\n  disabled: [hermes-muse]\n"
    )
    assert client.get(prefix + "/feed?profile=beta", headers=headers).status_code == 404
    assert (
        client.get(prefix + "/feed?profile=nonexistent", headers=headers).status_code
        == 404
    )
    assert client.post(
        prefix + "/feed", json={"action": "forget"}, headers=headers
    ).status_code in {404, 405}
    print(
        json.dumps(
            {
                "native_api": "PASS",
                "unauthenticated": 401,
                "profiles": ["serving", "beta"],
                "same_id_isolated": True,
                "articles": 103,
                "pure_read_hashes_unchanged": True,
                "disabled": 404,
                "missing_profile": 404,
                "writes_exposed": False,
            }
        )
    )
