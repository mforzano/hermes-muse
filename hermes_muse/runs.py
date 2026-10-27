"""Task outcomes separate from the native scheduler's process exit status."""
from __future__ import annotations

from .operations import reader
from .store import GOVERNANCE_JOBS, bounded, epoch


class Runs:
    def __init__(self, store, host, clock):
        self.store, self.host, self.clock = store, host, clock

    def bind(self, task_id):
        parts = str(task_id or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "cron":
            return None
        _, job_id, execution_id = parts
        job = self.host.manifest()["jobs"].get(job_id, {}).get("key")
        if job not in {"memory-upkeep", "nightly-review", "proactive-watch", "feed-pulse", *GOVERNANCE_JOBS} and not str(job).startswith("watch-"):
            return None
        if not self.host.owned_job(job_id):
            return None
        old = self.store.read("run", execution_id)
        path = self.store.home / "cron/executions.db"
        if not path.exists():
            return None
        with reader(path) as db:
            execution = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if not execution or execution["job_id"] != job_id or execution["status"] != "running":
            return None
        if old:
            return old
        row = {"id": execution_id, "job_id": job_id, "job": job,
               "started": epoch(execution["started_at"] or execution["claimed_at"]),
               "status": "running", "stages": {}}
        with self.store.transaction() as db:
            if not self.store.get(db, "run", execution_id):
                self.store.put(db, "run", execution_id, row)
        return self.store.read("run", execution_id)

    def stage(self, run, name, evidence):
        if not run:
            return
        with self.store.transaction() as db:
            row = self.store.get(db, "run", run["id"])
            row["stages"][name] = {"at": self.clock(), **evidence}
            self.store.put(db, "run", row["id"], row)

    def finish(self, run, data):
        if not run:
            raise ValueError("run_complete requires a host-bound owned Cron execution")
        summary = bounded(data.get("summary"), 2000)
        outcome = data.get("outcome")
        if outcome not in {"complete", "partial"}:
            raise ValueError("outcome must be complete or partial")
        gaps = data.get("gaps", [])
        if not isinstance(gaps, list):
            raise ValueError("gaps must be a list of unresolved work and recovery steps")
        gaps = [bounded(gap, 2000) for gap in gaps]
        if outcome == "partial" and not gaps:
            raise ValueError("Partial work needs a concrete gap and recovery step")
        with self.store.transaction() as db:
            row = self.store.get(db, "run", run["id"])
            stages = row["stages"]
            if outcome == "complete":
                if gaps:
                    raise ValueError("Unfinished work must remain partial")
                if not stages.get("review", {}).get("complete"):
                    raise ValueError("Finish this run's review_complete checkpoint first")
                if row["job"] == "memory-upkeep" and not stages.get("operations", {}).get("complete"):
                    raise ValueError("Operation review is unfinished; preserve its checkpoint and report partial")
                if row["job"] in GOVERNANCE_JOBS:
                    from .content_io import Files, revision
                    raw = Files(self.store.home).read(f"workspace/reviews/{row['id']}.md")
                    if not raw.strip():
                        raise ValueError("Save this governance run's full review_report before completion")
                    stages["report"] = {"id": row["id"], "revision": revision(raw), "at": self.clock()}
                if row["job"] == "nightly-review":
                    day = self.host.local_now(row["started"]).date().isoformat()
                    paths = [f"dreams/{day}.md", "dreams/alignment/derived/ALIGNMENT_SYNTHESIS.md"]
                    for name in paths:
                        path = self.store.path(name)
                        if not path.is_file() or path.stat().st_mtime < row["started"] or not path.read_text(encoding="utf-8").strip():
                            raise ValueError(f"Nightly result missing or unchanged since this run began: {name}")
                    stages["artifacts"] = {"at": self.clock(), "paths": paths}
            row.update(status="ready" if outcome == "complete" else "partial", summary=summary, gaps=gaps)
            self.store.put(db, "run", row["id"], row)
        return {**row, "instruction": "Work recorded, not delivered. Finish notification preparation when applicable, then return the required final output. Partial work remains resumable at saved checkpoints."}

    def finalize(self, run, *, notifications=False):
        with self.store.transaction() as db:
            row = self.store.get(db, "run", run["id"])
            if not row:
                return
            if notifications:
                row["stages"]["notification_output"] = {"at": self.clock(), "complete": True}
            if row["status"] == "ready":
                row["status"] = "complete"
            elif row["status"] == "running":
                row.update(status="partial", gaps=["Execution ended without run_complete; inspect native output and resume unfinished stages."])
            row["finished"] = self.clock()
            self.store.put(db, "run", row["id"], row)

    def recent(self):
        rows = sorted(self.store.all("run"), key=lambda r: r["started"], reverse=True)
        path = self.store.home / "cron/executions.db"
        if path.exists():
            with reader(path) as db:
                for row in rows:
                    if row["status"] not in {"running", "ready"}:
                        continue
                    native = db.execute("SELECT status,error FROM executions WHERE id=?", (row["id"],)).fetchone()
                    if native and native["status"] not in {"running", "claimed"}:
                        row.update(status="partial", gaps=[native["error"] or "Native execution ended without a verified final result; resume unfinished stages."])
                        self.store.write("run", row["id"], row)
        # A newly started attempt must not hide the preceding partial outcome it needs to resume.
        latest = {}
        for row in rows:
            category = "active" if row["status"] in {"running", "ready"} else "ended"
            latest.setdefault((row["job"], category), row)
            if row["status"] == "complete":
                latest.setdefault((row["job"], "last_complete"), row)
        return list({row["id"]: row for row in latest.values()}.values())
