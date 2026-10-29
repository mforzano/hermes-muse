"""Install / upgrade on plugin load (design 7.4 step 2), under muse/install.lock.

1. Write the migration marker muse/migrating.json: every Muse cron launcher and the background tick stay
   silent while it exists (a failed install leaves it, with the error, and the main session is told).
2. Migrate 0.3.1/0.3.16 muse/state.db into muse/state-v2.db once (idempotent; v1 stays read-only).
3. Create missing content files from templates (never overwrite) and the content folders.
4. Refresh the Muse cron jobs in place. Keys keep their 0.3.x names, so job ids, user schedules, routes
   and pauses survive and external callers keep their job references; each gets `prompts/jobs/<job>.md`
   and runs cron_entry.py. The monthly audit is paused (merged into the weekly digest). Watch jobs get
   the current watch prompt (ensure_watch is also how muse_manage watch_create makes them).
5. Create or refresh the Health no_agent cron. 6. Remove the marker.
"""
import json
import os
import time
from pathlib import Path

from . import health
from .content_io import Files
from .host import PLUGIN
from .kernel.common import preferences
from .kernel.store import Store
from .locks import file_lock

MARKER = "muse/migrating.json"
PROMPTS_BACKUP = "muse/backup/cron-prompts-0.3.json"
# cron key (0.3.x name) -> (job, default schedule for a new install, delivers to the user); prompt prompts/jobs/<job>.md
JOBS = {
    "memory-upkeep": ("intake", "0 * * * *", False),
    "proactive-watch": ("patrol", "*/30 * * * *", True),
    "nightly-review": ("nightly", "20 3 * * *", False),
    "feed-pulse": ("feed", "0 8 * * *", False),
    "weekly-governance-review": ("weekly", "15 21 * * 0", True),
}
RETIRED = {"monthly-system-audit": "merged into the weekly digest"}
SKILLS = [f"{PLUGIN}:companion"]
WATCH_PROMPT = """You run one Hermes Muse watch for the user's goal (its state is in Script Output).
Check: {check}
Stop condition: {stop}
Use only read-only tools. If the stop condition is met, or something the user must act on changed, answer in one or two
short sentences in the user's language with the evidence link. Otherwise answer exactly [SILENT]."""
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


def watch_check(prompt):
    """What a watch checks, from its current prompt: v2 (`Check: …`) or 0.3.x (after the generated instructions)."""
    prompt = str(prompt or "")
    head, found, _ = prompt.rpartition("\nStop condition: ")
    if not found:
        return None
    for marker in ("\nCheck: ", "With nothing approved return [SILENT].\n"):
        if marker in head:
            return head.rpartition(marker)[2].strip() or None
    return None


def ensure_watch(adapter, root, key, *, schedule, check, stop_condition, deliver=None):
    """Create or refresh the owned job of one watch; a refresh keeps its schedule, route and pause."""
    prompt = WATCH_PROMPT.format(check=check.strip(), stop=str(stop_condition or "").strip())
    return adapter.cron.ensure(key, schedule=schedule, entry=Path(root) / "cron_entry.py", prompt=prompt,
                               skills=SKILLS, deliver=deliver or route(adapter))


def jobs(adapter, root, store):
    entry, out = root / "cron_entry.py", {}
    for key, (job, schedule, delivered) in JOBS.items():
        found = adapter.cron.ensure(key, schedule=schedule, entry=entry, skills=SKILLS,
                                    prompt=(root / "prompts" / "jobs" / f"{job}.md").read_text(encoding="utf-8"),
                                    deliver=route(adapter) if delivered else "local")
        out[key] = found["id"] if found else None
    for key, reason in RETIRED.items():
        job = owned(adapter, key)
        out[key] = "paused" if job and adapter.cron.pause(job["id"], reason) else None
    by_id = adapter.cron.jobs()
    for w in store.query("watch", state="active"):
        job = by_id.get(w["payload"].get("job_id"))
        check = job and (w["payload"].get("check") or watch_check(job.get("prompt")))
        if check:  # shortcut: a watch whose prompt Muse cannot parse keeps it; the user recreates it
            key = job["script"][len("hermes-muse-"):-3]
            out[key] = (ensure_watch(adapter, root, key, schedule=job.get("schedule"), check=check,
                                     stop_condition=w["payload"].get("stop_condition")) or {}).get("id")
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
