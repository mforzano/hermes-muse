"""Boundary check (design 2.3): only hermes_muse/host/ may import Hermes; no 0.3.x module comes back.

Every other shipped Python file may import only the standard library, the plugin's own package and
fastapi (the dashboard router). Imports inside functions count. Exit 1 with one line per violation.
Usage: python scripts/check_boundaries.py [repo]
"""
import ast
import sys
from pathlib import Path

ALLOWED = {"hermes_muse", "fastapi"}
V1_MODULES = {"audit", "content", "context_pages", "contracts", "conversation", "delegation", "fade", "followup",
              "guidance", "handoff", "host_v1", "installation", "memory_feedback", "migration", "mutations",
              "operations", "profile_admin", "request_context", "research", "responses", "runs", "scheduling",
              "service", "store", "wake_gate"}
SKIP = {".git", "tests", "node_modules", "__pycache__"}


def violations(repo):
    host = repo / "hermes_muse" / "host"
    for path in sorted(repo.rglob("*.py")):
        relative = path.relative_to(repo)
        if SKIP & set(relative.parts) or host in path.parents:
            continue
        if path.parent == repo / "hermes_muse" and path.stem in V1_MODULES:
            yield f"{relative}: 0.3.x module {path.stem!r} is back"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(relative))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and top not in ALLOWED:
                    yield f"{relative}:{node.lineno}: imports {name!r}; only hermes_muse/host may import Hermes"


def main():
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]).resolve()
    found = list(violations(repo))
    print("\n".join(found) if found else f"boundaries PASS: only hermes_muse/host imports Hermes ({repo.name})")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
