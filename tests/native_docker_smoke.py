"""Synthetic model envelopes across real native Docker terminal and host registry.

No plugin/data bind mount or new RPC server. This is the same host-dispatch boundary
used by a Hermes agent whose terminal backend is Docker, not execute_code (whose
public tool allowlist excludes third-party tools).
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

os.environ["HERMES_DATA_DIR_SUFFIX"] = "-muse-isolated-test"
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), os.environ["HERMES_SOURCE"]]
with tempfile.TemporaryDirectory(prefix="muse-docker-") as directory:
    base = Path(directory)
    os.environ["HERMES_HOME"] = str(base / "a")
    from hermes_muse.host_v1 import profile_scope
    from hermes_cli.plugins import discover_plugins, invoke_hook, get_plugin_manager
    from tools.registry import registry
    from model_tools import handle_function_call
    from tools.environments.docker import DockerEnvironment

    records = []
    first_scope = None
    for label in ("a", "b"):
        home = base / label
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: [hermes-muse]\ntimezone: UTC\nterminal:\n  backend: docker\n  credential_files: []\n"
        )
        shutil.copytree(
            root,
            home / "plugins/hermes-muse",
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        with profile_scope(home):
            discover_plugins(force=True)
            rt = registry.get_entry("muse_manage").handler.__self__
            invoke_hook(
                "pre_llm_call",
                session_id="fixture-" + label,
                turn_id="signal",
                platform="cli",
                user_message="Research goal g using the fixture only",
            )

            def dispatch(envelope):
                return json.loads(
                    handle_function_call(
                        "muse_manage",
                        envelope,
                        session_id="fixture-" + label,
                        turn_id="signal",
                        tool_call_id="fixture-" + uuid.uuid4().hex,
                        enabled_toolsets=["muse"],
                    )
                )

            created = dispatch(
                {
                    "action": "goal_create",
                    "data": {
                        "id": "g",
                        "title": label,
                        "description": "Synthetic " + label,
                        "completion_criteria": "Verified artifact",
                        "signal_id": "signal",
                    },
                }
            )
            assert created["ok"], created
            local = dispatch(
                {"action": "content_read", "data": {"kind": "goal", "id": "g"}}
            )
            assert local["result"]["metadata"]["title"] == label
            env = DockerEnvironment(
                image="public.ecr.aws/docker/library/node:22-bookworm-slim",
                cwd="/workspace",
                timeout=30,
                task_id="muse-acceptance-" + uuid.uuid4().hex,
                volumes=[],
                forward_env=[],
                env={},
                network=False,
                persistent_filesystem=False,
                persist_across_processes=False,
                image_pinned=True,
            )
            cid = env._container_id
            try:
                mounts = json.loads(
                    subprocess.check_output(
                        ["docker", "inspect", "--format", "{{json .Mounts}}", cid],
                        text=True,
                    )
                )
                assert all(
                    str(home / "muse") not in m.get("Source", "")
                    and str(home / "plugins") not in m.get("Source", "")
                    for m in mounts
                ), mounts

                def container_request(envelope):
                    source = "console.log(JSON.stringify(" + json.dumps(envelope) + "))"
                    out = env.execute("node -e " + shlex.quote(source))
                    assert out.get("returncode", 0) == 0, out
                    actual = json.loads(out["output"].strip().splitlines()[-1])
                    result = dispatch(actual)
                    check = (
                        "const r="
                        + json.dumps(result)
                        + '; if(typeof r.ok!=="boolean")process.exit(2);console.log("received:"+r.ok)'
                    )
                    received = env.execute("node -e " + shlex.quote(check))
                    assert received.get("returncode", 0) == 0, received
                    return result

                read = container_request(
                    {"action": "content_read", "data": {"kind": "goal", "id": "g"}}
                )
                assert read["result"]["metadata"]["title"] == label
                refused=container_request({'action':'content_read','data':{'kind':'goal','id':'g','home':'foreign'}})
                assert refused['code']=='invalid_argument'
                written = container_request(
                    {
                        "action": "content_write",
                        "data": {
                            "kind": "goal_file",
                            "id": "g",
                            "resource": "files/container.md",
                            "body": "Container fixture " + label,
                            "expected_revision": "absent",
                            "request_id": "container-write",
                            "signal_id": "signal",
                        },
                    }
                )
                assert written["ok"], written
                final = dispatch(
                    {
                        "action": "content_read",
                        "data": {
                            "kind": "goal_file",
                            "id": "g",
                            "resource": "files/container.md",
                        },
                    }
                )
                assert final["result"]["body"] == "Container fixture " + label
                records.append(
                    {
                        "profile": label,
                        "container_id": cid,
                        "mount_destinations": [m["Destination"] for m in mounts],
                        "muse_mounts": 0,
                        "local_read": True,
                        "container_read_write": True,
                    }
                )
            finally:
                env.cleanup(force_remove=True)
                env.wait_for_cleanup(timeout=30)
                remaining = subprocess.run(
                    ["docker", "inspect", cid], capture_output=True
                ).returncode
                assert remaining != 0, "Owned test container was not removed"
                rt.close()
                get_plugin_manager().unload("hermes-muse")
    print(
        json.dumps(
            {
                "native_docker": "PASS",
                "profiles": records,
                "network": False,
                "provider_calls": 0,
                "external_messages": 0,
                "transport": "container-generated synthetic model JSON -> real host tool registry -> container response consumption",
                "execute_code_custom_tool_rpc": "not supported by host static allowlist; not used or patched",
            }
        )
    )
