"""Install / upgrade on plugin load (design 7.4 step 2), under muse/install.lock.

1. Write the migration marker muse/migrating.json: every Muse cron launcher and the background tick stay
   silent while it exists (a failed install leaves it, with the error, and the main session is told).
2. Migrate 0.3.1/0.3.16 muse/state.db into muse/state-v2.db once (idempotent; v1 stays read-only).
3. Create missing content files from templates (never overwrite) and the content folders.
4. Refresh the Muse cron jobs in place. Keys keep their 0.3.x names, so job ids, user schedules, routes
   and pauses survive and external callers keep their job references; the monthly audit is paused
   (merged into the weekly digest). Watch jobs get the current watch prompt when one ships.
5. Create or refresh the Health no_agent cron. 6. Remove the marker.
"""
import json
import os
import time
from pathlib import Path

from . import health
from .content_io import Files
from .host import PLUGIN
from .kernel import goals
from .kernel.common import preferences
from .kernel.store import Store
from .locks import file_lock

MARKER = "muse/migrating.json"
PROMPTS_BACKUP = "muse/backup/cron-prompts-0.3.json"
# cron key -> (kernel job, default schedule for a new install, delivers to the user)
JOBS = {
    "memory-upkeep": ("intake", "0 * * * *", False),
    "proactive-watch": ("patrol", "*/30 * * * *", True),
    "nightly-review": ("nightly", "20 3 * * *", False),
    "feed-pulse": ("feed", "0 8 * * *", False),
    "weekly-governance-review": ("weekly", "15 21 * * 0", True),
}
RETIRED = {"monthly-system-audit": "merged into the weekly digest"}
FOLDERS = ("memory/people", "memory/groups", "dreams/alignment/derived", "workspace/goals", "workspace/your_files/feed")


def migrating(home):
    """The marker's content while an install is running or failed, else None."""
    try:
        return json.loads((Path(home) / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        return {"at": 0, "error": "unreadable migration marker"}


def route(adapter):
    """The profile's main delivery route: preference, plugin setting, then the profile's Bot Chat."""
    try:
        deliver = preferences(adapter.home).get("deliver")
    except ValueError:
        deliver = None
    return deliver or adapter.config.get().get("deliver") or "bot-chat"


def templates(home, root):
    files, created = Files(home), []
    for path in sorted((root / "templates").rglob("*.md")):
        relative = path.relative_to(root / "templates").as_posix()
        try:
            files.read(relative)
            continue
        except FileNotFoundError:
            pass
        body = path.read_text(encoding="utf-8").replace("{{MUSE_HOME}}", str(home / "muse")).replace("{{HERMES_HOME}}", str(home))
        files.replace(relative, body.encode(), "absent")
        created.append(relative)
    for folder in FOLDERS:
        (home / "muse" / folder).mkdir(parents=True, exist_ok=True)
    return created


def owned(adapter, key):
    return next((j for j in adapter.cron.jobs().values() if j.get("script") == f"hermes-muse-{key}.py"), None)


def backup_prompts(adapter):
    """Keep the 0.3.x prompts of owned jobs once, before the first refresh: they carry the 0.3.x
    installation markers, so a rollback that restores them is not answered with a duplicate job set."""
    path = adapter.home / PROMPTS_BACKUP
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    jobs = {i: {"name": j.get("name"), "prompt": j.get("prompt"), "enabled": j.get("enabled")}
            for i, j in adapter.cron.owned_jobs().items()}
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    path.chmod(0o600)
    return True


def jobs(adapter, root, store):
    entry, skills, out = root / "cron_entry.py", [f"{PLUGIN}:companion"], {}
    for key, (_, schedule, delivered) in JOBS.items():
        job = adapter.cron.ensure(key, schedule=schedule, entry=entry, skills=skills,
                                  prompt=(root / "prompts" / f"{key}.md").read_text(encoding="utf-8"),
                                  deliver=route(adapter) if delivered else "local")
        out[key] = job["id"] if job else None
    for key, reason in RETIRED.items():
        job = owned(adapter, key)
        out[key] = "paused" if job and adapter.cron.pause(job["id"], reason) else None
    template = root / "prompts" / "watch.md"
    if template.is_file():
        by_id = adapter.cron.jobs()
        for w in store.query("watch", state="active"):
            job = by_id.get(w["payload"].get("job_id"))
            if not job:
                continue
            p, key = w["payload"], job["script"][len("hermes-muse-"):-3]
            found = goals.read(adapter.home, p.get("goal_id"))
            prompt = (template.read_text(encoding="utf-8") + f"\nGoal: {p.get('goal_id')}"
                      + (f" ({found[0].get('title')})" if found else "") + f"\nStop condition: {p.get('stop_condition') or ''}\n")
            out[key] = (adapter.cron.ensure(key, schedule=job.get("schedule"), entry=entry, prompt=prompt, skills=skills) or {}).get("id")
    return out


def install(adapter, root, now=None):
    """Idempotent; returns what it did. Raises after leaving the marker (with the error) on failure."""
    from .kernel.store.migrate import migrate
    home, root = adapter.home, Path(root).resolve()
    now = time.time() if now is None else now
    marker = home / MARKER
    with file_lock(home / "muse" / "install.lock"):
        mark = {"at": now, "pid": os.getpid()}
        marker.write_text(json.dumps(mark), encoding="utf-8")
        try:
            report = {"migration": migrate(home, now=now) if (home / "muse" / "state.db").is_file() else None}
            store = Store.for_home(home)
            report["templates"] = templates(home, root)
            report["prompts_backed_up"] = backup_prompts(adapter)
            report["jobs"] = jobs(adapter, root, store)
            report["health_cron"] = (health.ensure_cron(adapter, route(adapter)) or {}).get("id")
        except Exception as exc:
            marker.write_text(json.dumps({**mark, "error": f"{type(exc).__name__}: {exc}"[:500]}), encoding="utf-8")
            raise
        marker.unlink()
    return report
