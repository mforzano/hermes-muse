"""Hermes dashboard host calls: serving profile, profile listing and selection, request token. Read-only."""
from __future__ import annotations

from pathlib import Path

from . import PLUGIN


class Unavailable(LookupError):
    """The selected profile is unknown, or Muse is not installed and enabled there."""


def selection_enabled(home):
    # PM's public selection reader neither seeds backups nor mutates process globals; load_config does.
    from pm.plugins_state import read_home_selection
    selection = (read_home_selection(home) or {}).get("plugins", {})
    return PLUGIN in selection.get("enabled", []) and PLUGIN not in selection.get("disabled", [])


def version(home):
    from hermes_cli.plugins import parse_manifest_file
    path = Path(home) / "plugins" / PLUGIN
    manifest = parse_manifest_file(path / "plugin.yaml", path, "user", "") if (path / "plugin.yaml").is_file() else None
    return manifest.version if manifest else None


def resolve(name):
    from hermes_cli.profiles import get_profile_dir, list_profile_names, normalize_profile_name
    from hermes_constants import get_default_hermes_root, get_hermes_home
    if not isinstance(name, str) or normalize_profile_name(name) != name or name not in list_profile_names():
        raise Unavailable("Select a registered profile on this Hermes host")
    home = get_profile_dir(name).resolve()
    if name == "default" and home != get_default_hermes_root(home=get_hermes_home()).resolve():
        raise Unavailable("Default root resolution changed")
    return home


def state(name):
    home = resolve(name)
    installed = version(home)
    return {"name": name, "installed": installed is not None, "enabled": bool(installed and selection_enabled(home)),
            "version": installed, "managed_by": "Hermes", "read_only": True,
            "initialized": (home / "muse" / "state-v2.db").is_file()}


def profiles():
    from hermes_cli.profiles import list_profile_names
    return {"profiles": [state(name) for name in list_profile_names()],
            "host_scope": "This Hermes instance only; select a profile before viewing its content"}


def home_for(selected_profile=None):
    """Profile home whose Muse content the dashboard may show; raises Unavailable otherwise."""
    if selected_profile is not None:
        if not state(selected_profile)["enabled"]:
            raise Unavailable("Muse is not installed and enabled in this profile")
        return resolve(selected_profile)
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    if not selection_enabled(home) or version(home) is None:
        raise Unavailable("Muse is not installed and enabled in this profile")
    return home


def require_token(request):
    from hermes_cli.web_server import _require_token  # private: probe `symbols` reports when it moves
    _require_token(request)
