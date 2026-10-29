"""A11: find 0.3.x Muse dependencies in external callers (cron prompts, scripts) before the cutover.

Flags, per file and line:
  action    a muse_manage action that v2 removed, on a line that mentions muse_manage, or as "action": "<name>"
  session   cron sessions looked up by the old `cron_<job id>_` id prefix only (v2: source='cron' + title)
  store     direct reads of the 0.3.x muse/state.db `records` table
  import    imports of removed hermes_muse modules
Usage: python scripts/check_callers.py FILE_OR_DIR... ; exit 1 when anything is found.
"""
import re
import sys
from pathlib import Path

REMOVED = ("content_read", "memory_feedback_review", "context", "status", "conversation_read", "notification_add",
           "notification_refresh", "notification_queue", "notification_handoff", "notification_prepare",
           "notification_resolve", "notification_review", "feed_add", "feed_list", "calendar_window", "source_check",
           "review_complete", "run_complete", "delegate", "research_start", "research_status", "operations_read",
           "operations_complete", "operations_batch", "operations_evidence", "operations_commit", "finding_record")
WORD = re.compile(r"(?<![\w.-])(" + "|".join(REMOVED) + r")(?![\w-])")
ACTION = re.compile(r"""["']action["']\s*:\s*["'](""" + "|".join(REMOVED) + r""")["']""")
SESSION = re.compile(r"""cron_%s_|cron_\{|cron_<|LIKE\s+'cron_""", re.I)
SOURCE_CRON = re.compile(r"""source\s*=\s*['"]cron['"]""")
STORE = re.compile(r"""muse/state\.db["')]|from\s+records\b""", re.I)
IMPORT = re.compile(r"\b(?:from|import)\s+hermes_muse\.(store|service|views|operations|context_pages|contracts|guidance|host_v1|"
                    r"mutations|followup|fade|memory_feedback|conversation|runs)\b")


def scan(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    has_source = bool(SOURCE_CRON.search(text))
    for number, line in enumerate(text.splitlines(), 1):
        hits = []
        if "muse_manage" in line:
            hits += [("action", m) for m in WORD.findall(line)]
        hits += [("action", m) for m in ACTION.findall(line)]
        if SESSION.search(line) and not has_source:
            hits.append(("session", "cron_ id prefix without source='cron'"))
        if STORE.search(line):
            hits.append(("store", "0.3.x muse/state.db records"))
        hits += [("import", f"hermes_muse.{m}") for m in IMPORT.findall(line)]
        for rule, what in dict.fromkeys(hits):
            yield f"{path}:{number}: {rule}: {what}"


def files(args):
    for arg in map(Path, args):
        yield from (sorted(p for p in arg.rglob("*") if p.is_file()) if arg.is_dir() else [arg])


def main(args):
    found = [hit for path in files(args) for hit in scan(path)]
    print("\n".join(found) if found else f"callers PASS: no 0.3.x Muse dependency in {len(list(files(args)))} file(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
