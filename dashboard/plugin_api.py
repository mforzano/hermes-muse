"""Host-authenticated read-only content views; profile lifecycle stays with Hermes plugin management."""

import importlib.util
import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request

# Dashboard imports this file independently of the CLI plugin package.
_package = (
    "_hermes_muse_views_"
    + __import__("hashlib")
    .new("sha256", str(Path(__file__).resolve().parent.parent).encode())
    .hexdigest()[:16]
)
if _package not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _package,
        Path(__file__).resolve().parent.parent / "hermes_muse/__init__.py",
        submodule_search_locations=[
            str(Path(__file__).resolve().parent.parent / "hermes_muse")
        ],
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_package] = _module
    _spec.loader.exec_module(_module)
_views = __import__(_package + ".views", fromlist=["Views"])
_admin = __import__(_package + ".profile_admin", fromlist=["profiles"])
_health = __import__(_package + ".health", fromlist=["load"])
router = APIRouter()


def view(selected_profile=None):
    if selected_profile is not None:
        return _admin.selected_view(selected_profile)
    from hermes_constants import get_hermes_home
    from pm.plugins_state import read_home_selection

    home = Path(get_hermes_home())
    config = (read_home_selection(home) or {}).get("plugins", {})
    if "hermes-muse" not in config.get("enabled", []) or "hermes-muse" in config.get(
        "disabled", []
    ):
        raise HTTPException(404, "Muse is unavailable in this profile")
    if not (home / "plugins/hermes-muse/plugin.yaml").is_file():
        raise HTTPException(404, "Muse is not installed in this profile")
    return _views.Views(home)


def read(data, selected_profile=None):
    try:
        return view(selected_profile).read(data)
    except _views.DomainError as exc:
        raise HTTPException(
            {
                "not_installed": 404,
                "not_found": 404,
                "permission_denied": 403,
                "profile_mismatch": 403,
                "revision_conflict": 409,
                "cursor_expired": 409,
            }.get(exc.code, 400),
            {"code": exc.code, "message": str(exc), **exc.details},
        ) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            503, "Stored Muse data unavailable; no repair performed"
        ) from exc


@router.get("/capabilities")
def capabilities(selected_profile: str | None = None):
    current = view(selected_profile)
    try:
        scope = current.scope()
    except _views.DomainError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "read_only": True,
        "profile": selected_profile or current.home.name,
        "features": ["feed", "ideas", "goals", "topics", "pagination"],
        "host_gaps": [
            "pending_write_resolved",
            "provider_durable_receipt",
            "correlated_process_merge",
            "disable_transaction",
        ],
    }


@router.get("/feed")
def feed(
    topic: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    selected_profile: str | None = None,
):
    return read(
        {
            "kind": "feed",
            "topic": topic,
            "query": query,
            "cursor": cursor,
            "limit": limit,
        }, selected_profile
    )


@router.get("/feed/{object_id}")
def article(
    object_id: str,
    offset: int = 0,
    length: int = 12000,
    revision: str | None = None,
    selected_profile: str | None = None,
):
    return read(
        {
            "kind": "feed",
            "id": object_id,
            "offset": offset,
            "length": length,
            "revision": revision,
        }, selected_profile
    )


@router.get("/ideas")
def ideas(cursor: str | None = None, limit: int = Query(25, ge=1, le=100), selected_profile: str | None = None):
    return read({"kind": "idea", "cursor": cursor, "limit": limit}, selected_profile)


@router.get("/goals")
def goals(
    cursor: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    status: str | None = Query(None, pattern="^(open|closed)$"),
    selected_profile: str | None = None,
):
    return read(
        {"kind": "goal", "cursor": cursor, "limit": limit, "status": status},
        selected_profile,
    )


@router.get("/goals/{object_id}")
def goal(
    object_id: str,
    offset: int = 0,
    length: int = 12000,
    revision: str | None = None,
    selected_profile: str | None = None,
):
    return read(
        {
            "kind": "goal",
            "id": object_id,
            "offset": offset,
            "length": length,
            "revision": revision,
        }, selected_profile
    )


@router.get("/health")
def health(selected_profile: str | None = None):
    """The profile's last muse/health.json as written by the health cron; never runs a check."""
    return _health.load(view(selected_profile).home) or {"available": False}


@router.get("/profiles")
def profiles(request: Request):
    from hermes_cli.web_server import _require_token
    _require_token(request)
    return _admin.profiles()

