"""Health: an independent, read-only check of one profile (design 2.8).

Runs as a no_agent cron (`health_entry.py`, every 30 minutes). It opens Store v2 read-only, runs the
HostAdapter probes and the invariants, and writes `<home>/muse/health.json`. Alert exits, none of them
through the Muse Notifier:
  1. main-session injection: `injection(home)` returns one line while any high finding is open;
  2. Dashboard: `load(home)` behind the plugin's `/health` route;
  3. cron stdout: `run()` returns a message only when the alert set changes (empty stdout is silent);
  4. optional `health_deliver` setting: the same message sent to a second Hermes route.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import invariants as inv

SCHEMA = 1
INTERVAL = 30 * 60
STALE = 4 * INTERVAL
ALERT = ("medium", "high")


def path(home):
    return Path(home) / "muse" / "health.json"


def load(home):
    """The last health.json, or None when Health has not run in this profile."""
    try:
        return json.loads(path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary(home):
    """health.json without its passing checks: level, time and `items` (what is wrong and its fix)."""
    report = load(home)
    if not report:
        return None
    return {"level": report.get("level"), "generated_at": report.get("generated_at"),
            "items": [{f: x.get(f) for f in ("id", "level", "detail", "fix", "first_seen")}
                      for x in report.get("invariants", []) if x.get("level") != "ok"]}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def check(adapter, now=None):
    """All findings for one profile; never writes the Store and never raises on host errors."""
    from ..kernel.store import Store
    now = time.time() if now is None else now
    findings, store = [], None
    db = adapter.home / "muse" / "state-v2.db"
    try:
        if not db.is_file():
            raise FileNotFoundError(db)
        store = Store(db, readonly=True)
        store.meta("migration")  # opens the file once so an unreadable db fails here
        findings.append(inv.finding("store", "ok", "readable", "readable", str(db)))
    except Exception as exc:
        store = None
        findings.append(inv.finding("store", "high", "unreadable", "readable", f"{type(exc).__name__}: {exc}",
                                    fix="Run the v2 migration, or restore muse/state-v2.db from backup"))
    captured = None
    if store:
        for checker in inv.STORE_CHECKS:
            try:
                found = checker(store, now)
            except Exception as exc:  # one broken invariant must not hide the others
                found = inv.finding(checker.__name__, "medium", "error", "computable", f"{type(exc).__name__}: {exc}")
            findings += found if isinstance(found, list) else [found]
        captured = {s["payload"].get("source") for s in store.query("signal", since=now - inv.D)} - {None}
    findings += inv.probes(adapter.probe(captured_sources=captured, now=now))
    owned = adapter.cron.jobs()
    findings += inv.deliveries(store, adapter.cron.executions(now - inv.D), owned, now)
    findings += inv.tools(adapter.sessions.tool_results(now - inv.D))
    findings += inv.jobs(owned, now)
    findings.append(inv.host_faults(list(adapter.faults)))
    return findings


def fingerprint(findings):
    return hashlib.sha256(json.dumps(sorted((f["id"], f["level"]) for f in findings if f["level"] in ALERT))
                          .encode()).hexdigest()[:16]


def message(report):
    alerts = [f for f in report["invariants"] if f["level"] in ALERT]
    if not alerts:
        return f"Muse health ({report['profile']}) recovered: no medium or high findings."
    lines = [f"Muse health ({report['profile']}): {report['level']}"]
    lines += [f"- {f['level']} {f['id']}: {f['detail'][:240]}" + (f" Fix: {f['fix'][:200]}" if f["fix"] else "")
              for f in sorted(alerts, key=lambda f: -inv.LEVELS.index(f["level"]))]
    return "\n".join(lines)


def write(home, report):
    target = path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".health.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, target)


def run(home=None, *, now=None, adapter=None):
    """One health pass: write health.json; return the alert message (empty when nothing changed)."""
    from ..host import HostAdapter
    adapter = adapter or HostAdapter(home)
    now = time.time() if now is None else now
    old = load(adapter.home) or {}
    first = {f["id"]: f.get("first_seen") for f in old.get("invariants", []) if f["level"] != "ok"}
    findings = check(adapter, now)
    for f in findings:
        f["first_seen"] = (first.get(f["id"]) or iso(now)) if f["level"] != "ok" else None
    report = {"schema": SCHEMA, "profile": adapter.home.name, "generated_at": iso(now), "generated_ts": now,
              "level": inv.worst(*(f["level"] for f in findings)), "alert_fingerprint": fingerprint(findings),
              "invariants": findings, "faults": list(adapter.faults)}
    changed = report["alert_fingerprint"] != old.get("alert_fingerprint", fingerprint([]))
    text = message(report) if changed else ""
    target = adapter.config.get().get("health_deliver")
    if text and target:  # the slot keeps a recurring alert set from hitting the one-shot's replay guard
        key = f"health-{report['alert_fingerprint']}-{int(now // INTERVAL)}"
        report["backup"] = adapter.deliver.send(str(target), text, key)
    write(adapter.home, report)
    return text


def injection(home, now=None):
    """One main-session line while a high finding is open or Health itself stopped; None otherwise."""
    report, now = load(home), time.time() if now is None else now
    if not report:
        return None
    if now - report.get("generated_ts", 0) > STALE:
        return f"[Muse health] The health check has not run since {report.get('generated_at')}; Muse may be failing silently."
    high = [f for f in report.get("invariants", []) if f["level"] == "high"]
    if not high:
        return None
    return "[Muse health] high: " + "; ".join(f"{f['id']} ({f['detail'][:120]})" for f in high) + \
        ". Tell the user once if relevant; details in muse/health.json."


def ensure_cron(adapter, deliver):
    """Create or refresh the health no_agent job on `deliver` (the profile's main route)."""
    entry = Path(__file__).resolve().parents[2] / "health_entry.py"
    return adapter.cron.ensure("health", schedule="every 30m", entry=entry, deliver=deliver, no_agent=True)
