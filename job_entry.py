"""Called only by the native Muse job launchers (v2): prints the run's packet as Script Output; empty is silent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hermes_muse.surface import job_output

if __name__ == "__main__":
    text = job_output(MUSE_HOME, MUSE_JOB)  # noqa: F821 - injected by the launcher
    if text:
        print(text)
