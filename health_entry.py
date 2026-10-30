"""Called only by the native `muse-health` no_agent Cron launcher; stdout is delivered, empty is silent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hermes_muse.health import run

if __name__ == "__main__":
    text = run(MUSE_HOME)  # noqa: F821 - injected by the launcher
    if text:
        print(text)
