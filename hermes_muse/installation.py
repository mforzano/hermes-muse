"""Explicit preparation only; ordinary unload never pauses host jobs."""

import re

from .content_io import DomainError, revision
from .host_v1 import file_lock, profile_scope
from .views import Views, canonical


def explicit_request(text, operation):
    """A deliberately narrow whole-message imperative, not inferred NL intent."""
    verbs = {"disable": ("disable", "停用|禁用"),
             "remove": ("remove|uninstall", "卸载|移除")}
    english, chinese = verbs[operation]
    target = r"(?:hermes-muse|muse)(?:\s+plugin)?"
    en = rf"(?:please\s+)?(?:{english})\s+(?:the\s+)?{target}(?:\s+now)?[.!]?"
    zh = rf"(?:现在)?(?:请)?(?:帮我)?(?:{chinese})\s*(?:hermes-muse|muse)\s*(?:插件)?(?:现在|立即)?[。！!]?"
    return bool(re.fullmatch(en, text.strip(), re.I) or re.fullmatch(zh, text.strip(), re.I))


def prepare(runtime, data, session_id, turn_id):
    if set(data) - {"operation", "signal_id", "expected_revision", "request_id"}:
        raise DomainError("invalid_argument", "Unknown installation fields")
    if data.get("operation") not in {"disable", "remove"}:
        raise DomainError("invalid_argument", "operation must be disable or remove")
    request = data.get("request_id")
    if not isinstance(request, str) or not 1 <= len(request) <= 128:
        raise DomainError("invalid_argument", "request_id required")
    store, host = runtime.store, runtime.host
    with store.transaction() as db:
        signal = runtime.service.require_signal(db, data.get("signal_id"))
    current = store.read("session", session_id, {})
    if signal["id"] != turn_id or current.get("turn_id") != signal["id"]:
        raise DomainError(
            "permission_denied", "Preparation requires this current user turn"
        )
    if signal["session_id"] != session_id or not explicit_request(signal["text"], data["operation"]):
        raise DomainError(
            "permission_denied",
            "Use an explicit current-profile Muse command matching operation: "
            "'Please disable Muse now.' / 'Please uninstall Muse now.' / '请停用Muse插件' / '请卸载Muse插件'. "
            "Quoted, conditional, deferred and instructional questions do not authorize preparation.",
        )
    return prepare_owned(runtime.home, store, host, data)


def prepare_owned(home, store, host, data, *, origin="tool"):
    """Shared pause lifecycle; caller has already authenticated its own entrypoint."""
    request = data['request_id']
    signature = revision(canonical(data))
    with profile_scope(home), file_lock(store.path("install.lock")):
        manifest = host.manifest()
        pending = manifest.get("preparation")
        if pending:
            if pending["request_id"] != request or pending["signature"] != signature:
                raise DomainError(
                    "idempotency_conflict", "An installation preparation already exists"
                )
        else:
            actual = Views(home).object("installation", "current")[2]
            if data.get("expected_revision") != actual:
                raise DomainError(
                    "revision_conflict", "Reread installation", current_revision=actual
                )
            pending = {
                "request_id": request,
                "signature": signature,
                "operation": data["operation"],
                "origin": origin,
                "expected_revision": data["expected_revision"],
                "jobs": {},
                "pending_evidence": [],
            }
            manifest["preparation"] = pending
            host.save_manifest(manifest)
        from cron.jobs import pause_job

        for key in manifest["jobs"]:
            entry = pending["jobs"].get(key)
            if entry and entry["state"] in {"paused", "unchanged"}:
                continue
            job = host.owned_job(key)
            if not job:
                pending["jobs"][key] = {
                    "state": "unchanged",
                    "reason": "ownership mismatch or absent",
                }
                host.save_manifest(manifest)
                continue
            if entry is None:
                entry = {"state": "pending", "was_enabled": bool(job.get("enabled"))}
                pending["jobs"][key] = entry
                host.save_manifest(manifest)
            try:
                if entry["was_enabled"]:
                    pause_job(
                        key,
                        reason="Muse explicit " + data["operation"] + " preparation",
                    )
                    entry.update(state="paused", paused_by_prepare=True)
                else:
                    entry.update(state="unchanged", paused_by_prepare=False)
            except Exception as exc:
                entry.update(state="pending", error=type(exc).__name__)
            host.save_manifest(manifest)
        pending["pending_evidence"] = [
            {"kind": kind, "id": r["id"]}
            for kind in ("run",)
            for r in store.all(kind)
            if r.get("state", r.get("status"))
            in {"pending", "dispatching", "unknown", "running", "partial"}
        ]
        host.save_manifest(manifest)
        states = pending["jobs"]
        return {
            "paused_owned_ids": [
                k for k, r in states.items() if r["state"] == "paused"
            ],
            "unchanged_ids": [
                k for k, r in states.items() if r["state"] == "unchanged"
            ],
            "remaining_ids": [k for k, r in states.items() if r["state"] == "pending"],
            "partial": any(r["state"] == "pending" for r in states.values()),
            "pending_evidence": pending["pending_evidence"],
            "requires_host_action": True,
            "revision": Views(home).object("installation", "current")[2],
        }
