"""Called only by the native per-profile Muse cron launchers (MUSE_HOME and MUSE_JOB are injected)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hermes_muse.entry import run
from hermes_muse.host import profile_scope

if __name__ == "__main__":
    with profile_scope(MUSE_HOME):  # noqa: F821 - injected by the launcher
        print(json.dumps(run(MUSE_HOME, MUSE_JOB), ensure_ascii=False))  # noqa: F821
