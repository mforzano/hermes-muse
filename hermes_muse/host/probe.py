"""Capability and semantic probes; no version ranges. Each result is {name, status, detail, fix}.

status: ok | degraded (works, but evidence is off) | missing (the feature cannot work as is).
A probe never raises; a host fault during a probe downgrades an `ok` to `degraded`.
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
import time
from collections import Counter
from pathlib import Path

from . import PLUGIN
from .cron import KNOWN_OUTCOMES
from .sessions import USER_SOURCES, classify

DAY = 86400
ODD_NAME = re.compile(r"[<>]|bound method|object at 0x")
CONTRACT_TEST = "run tests/native_host_contract.py with this Hermes and update hermes_muse/host"


def result(name, status, detail="", fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix if status != "ok" else ""}


def host_imports():
    """(module, name) for every Hermes import in hermes_muse/host; name None for a plain import."""
    found = set()
    for path in Path(__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                found |= {(node.module, alias.name) for alias in node.names}
            elif isinstance(node, ast.Import):
                found |= {(alias.name, None) for alias in node.names}
    return sorted((m, n) for m, n in found if m.split(".")[0] not in sys.stdlib_module_names)


def symbols(adapter, **_):
    gaps = []
    imports = host_imports()
    for module, name in imports:
        try:
            if name and not hasattr(importlib.import_module(module), name):
                gaps.append(f"{module}.{name}")
            elif not name:
                importlib.import_module(module)
        except Exception as exc:
            gaps.append(f"{module} ({type(exc).__name__})")
    gaps += adapter.sessions.schema_gaps()
    return result("symbols", "missing" if gaps else "ok",
                  "absent: " + ", ".join(gaps) if gaps else f"{len(imports)} Hermes symbols and state.db columns present",
                  CONTRACT_TEST)


def cron_sessions(adapter, now, **_):
    """Woken Muse agent runs in 24h (a cron session started inside the run) whose session job_of attributes."""
    jobs = adapter.cron.jobs()
    runs = [e for e in adapter.cron.executions(now - DAY) if not jobs.get(e["job_id"], {}).get("no_agent")]
    sessions = adapter.sessions.cron_sessions(now - DAY)

    def inside(s, e):
        return e["start"] - 5 <= s["started_at"] <= e["end"] + 5

    woken = [e for e in runs if any(inside(s, e) for s in sessions)]
    seen = [e for e in woken if any(s["job_id"] == e["job_id"] and inside(s, e) for s in sessions)]
    if not woken:
        return result("cron_sessions", "ok", f"{len(runs)} Muse agent runs in 24h, none woke the model")
    ratio = len(seen) / len(woken)
    return result("cron_sessions", "ok" if ratio >= 0.8 else "degraded" if seen else "missing",
                  f"{len(seen)}/{len(woken)} woken runs attributed ({ratio:.2f}, threshold 0.8)",
                  "Hermes changed cron session naming or titles; update match_job in hermes_muse/host/sessions.py; "
                  + CONTRACT_TEST)


def delivery_semantics(adapter, now, **_):
    """Outcome values of the last 20 delivering Muse runs (7 days) and Bot Chat receipt readability."""
    jobs = adapter.cron.jobs()
    runs = sorted((e for e in adapter.cron.executions(now - 7 * DAY)
                   if (jobs.get(e["job_id"], {}).get("deliver") or "local") != "local"
                   and e["status"] not in ("claimed", "running")), key=lambda e: e["start"])[-20:]
    counts = Counter(e["delivery_outcome"] for e in runs)
    new = sorted(map(str, set(counts) - KNOWN_OUTCOMES))
    unreadable = sum(1 for e in runs if e["delivery_outcome"] == "queued"
                     and str(jobs[e["job_id"]].get("deliver")).startswith("bot-chat")
                     and adapter.deliver.bot_receipt(jobs[e["job_id"]]["deliver"], e["job_id"], e["execution_id"]) is None)
    failed = counts["failed"] + counts["not_configured"]
    detail = f"last {len(runs)} outcomes {dict(counts)}; unreadable Bot Chat receipts {unreadable}"
    if new:
        return result("delivery_semantics", "missing", f"new outcome values {new}; {detail}",
                      "Hermes changed delivery_outcome; map the new values in hermes_muse/host/cron.py Deliver.classify")
    return result("delivery_semantics", "degraded" if failed or unreadable else "ok", detail,
                  "Check the failing runs' last_delivery_error and the delivery_target probe; "
                  "unreadable receipts mean the native receipt key or location changed")


def delivery_target(adapter, **_):
    """Every Bot Chat row's cwd must exist on this host or Hermes refuses delivery (invalid_params field=cwd)."""
    rows = adapter.sessions.bot_chats()
    bad = [r for r in rows if r["cwd"] and not (Path(r["cwd"]).is_absolute() and Path(r["cwd"]).is_dir())]
    if not bad:
        return result("delivery_target", "ok", f"{len(rows)} Bot Chat session(s) with a valid host cwd")
    names = ", ".join(f"{r['id']} cwd={r['cwd']!r}" for r in bad)
    return result("delivery_target", "missing", f"Bot Chat session cwd does not exist on this host: {names}",
                  f"Set that session's cwd to an existing host directory such as {adapter.cwd()} via Hermes session "
                  "settings (or one backed-up UPDATE while the gateway is idle); do not change terminal.cwd. "
                  "Durable fix: drop non-existent cwd in Hermes legacy_launch_params.")


def signal_purity(adapter, now, captured_sources=None, **_):
    """Signals only from user surfaces: kernel-captured sources (if given), Hermes' own machine set, unknown sources."""
    leaked = sorted({s for s in captured_sources or () if classify(s) != "user"})
    try:
        from agent.prompt_caching import MACHINE_PACED_SOURCES
        overlap = sorted(USER_SOURCES & set(MACHINE_PACED_SOURCES))
    except ImportError:
        overlap = []
    unknown = sorted(s for s in adapter.sessions.sources(now - DAY) if classify(s) == "unclassified")
    detail = (f"captured non-user sources {leaked}; user sources Hermes calls machine-paced {overlap}; "
              f"unclassified sources (ignored) {unknown}"
              + ("" if captured_sources is not None else "; kernel signal sources not supplied"))
    status = "missing" if leaked or overlap else "degraded" if unknown else "ok"
    return result("signal_purity", status, detail,
                  "Classify each listed source in USER_SOURCES or MACHINE_SOURCES (hermes_muse/host/sessions.py); "
                  "purge signals captured from non-user sources")


def config_location(adapter, **_):
    plugins = adapter.config.raw().get("plugins") or {}
    legacy = plugins.get(PLUGIN)
    entry = (plugins.get("entries") or {}).get(PLUGIN) or {}
    current = {**(entry.get("config") or {}), **(entry.get("settings") or {})}
    if isinstance(legacy, dict) and legacy:
        clash = sorted(k for k in legacy if k in current and legacy[k] != current[k])
        detail = (f"plugins.{PLUGIN} and plugins.entries differ on {clash}; entries wins" if clash
                  else f"settings under legacy plugins.{PLUGIN} {sorted(legacy)}; read as fallback")
        return result("config_location", "degraded", detail,
                      f"Move them to plugins.entries.{PLUGIN}.settings and delete plugins.{PLUGIN}")
    return result("config_location", "ok", f"plugins.entries.{PLUGIN} keys {sorted(current)}")


def home(adapter, **_):
    root = adapter.home / "muse"
    if not adapter.home.is_dir():
        return result("home", "missing", f"profile home {adapter.home} is not a directory",
                      "Start Muse from Hermes so the active profile home is passed explicitly")
    odd = sorted(p.name for p in root.iterdir() if p.is_dir() and ODD_NAME.search(p.name)) if root.is_dir() else []
    return result("home", "degraded" if odd else "ok",
                  f"abnormal directories under {root}: {odd}" if odd else f"home {adapter.home}",
                  "A non-path (e.g. an uncalled Path.home) was used as a home; check the directory holds no "
                  "user data, then remove it")


PROBES = (symbols, cron_sessions, delivery_semantics, delivery_target, signal_purity, config_location, home)


def run(adapter, *, captured_sources=None, now=None):
    """All 7 probes; for Health at startup and daily. `captured_sources`: sources of signals Muse stored in 24h."""
    now = time.time() if now is None else now
    out = []
    for probe in PROBES:
        faults = adapter.fault_count
        try:
            found = probe(adapter, now=now, captured_sources=captured_sources)
        except Exception as exc:
            adapter.fault(f"probe.{probe.__name__}", exc)
            found = result(probe.__name__, "missing", f"probe failed: {type(exc).__name__}: {exc}", CONTRACT_TEST)
        if found["status"] == "ok" and adapter.fault_count > faults:
            errors = [f["error"] for f in list(adapter.faults)[-(adapter.fault_count - faults):]]
            found = result(found["name"], "degraded", f"{found['detail']}; host errors {errors}", CONTRACT_TEST)
        out.append(found)
    return out
