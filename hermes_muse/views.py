"""Shared, side-effect-free domain projections for tools and authenticated UI."""

from contextlib import contextmanager
import base64
import difflib
import json
import re
import sqlite3
import time
from pathlib import Path

from .content_io import DomainError, Files, revision
from .store import identifier

RECORDS = {
    "feed": "feed",
    "interest": "interest",
    "idea": "interest",
    "notice": "notification",
    "run": "run",
    "finding": "operation_finding",
    "research": "research",
    "source_check": "source_check",
}
COLLECTIONS = {*RECORDS, "goal", "goal_file", "note", "dream", "review_report"}
SINGLETONS = {
    "preferences": "PROACTIVE_PREFERENCES.md",
    "tools_note": "TOOLS.md",
    "working_lessons": "AGENTS.md",
    "alignment": "dreams/alignment/derived/ALIGNMENT_SYNTHESIS.md",
    "installation": "install.json",
}


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def date_id(value):
    from datetime import date

    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DomainError("invalid_argument", "Use YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def mapping(kind, key, resource=None):
    if kind in SINGLETONS:
        if key != "current" or resource is not None:
            raise DomainError(
                "invalid_argument", "Singleton ID must be current, without resource"
            )
        return SINGLETONS[kind]
    if kind == "note":
        if resource is not None or not isinstance(key, str) or ":" not in key:
            raise DomainError(
                "invalid_argument", "Use daily:date, person:id or group:id"
            )
        category, name = key.split(":", 1)
        if category == "daily":
            return "memory/" + date_id(name) + ".md"
        if category in {"person", "group"} and identifier(name).upper() != "INDEX":
            return (
                "memory/"
                + ("people" if category == "person" else "groups")
                + "/"
                + name
                + ".md"
            )
        raise DomainError("invalid_argument", "Invalid note ID")
    if kind == "note_index":
        if key not in {"people", "groups"} or resource is not None:
            raise DomainError("invalid_argument", "Index ID must be people or groups")
        return f"memory/{key}/INDEX.md"
    if kind == "dream":
        if resource is not None:
            raise DomainError("invalid_argument", "Dream does not accept resource")
        return "dreams/" + date_id(key) + ".md"
    if kind == "goal_file":
        if (
            not isinstance(resource, str)
            or len(resource) > 200
            or not re.fullmatch(
                r"(files|hidden_files)/[A-Za-z0-9_-][A-Za-z0-9_-]*\.md", resource
            )
        ):
            raise DomainError(
                "invalid_argument",
                "Use files/name.md or authorized hidden_files/name.md",
            )
        return f"workspace/goals/{identifier(key)}/{resource}"
    if resource is not None:
        raise DomainError("invalid_argument", "This kind does not accept resource")
    if kind == "goal":
        return f"workspace/goals/{identifier(key)}/GOAL.md"
    if kind == "review_report":
        return f"workspace/reviews/{identifier(key)}.md"
    raise DomainError("invalid_argument", "Unsupported content kind")


def validate_tools_note(body):
    from urllib.parse import urlsplit
    import ipaddress

    if re.search(
        r"(?i)(password|api[_ -]?key|secret|authorization(?=\s*[:=]\s*(?:bearer|basic|token)\b)|access[_ -]?token)\s*[:=]\s*\S+",
        body,
    ):
        raise DomainError("invalid_argument", "Tools notes cannot contain credentials")
    for url in re.findall(r"https?://[^\s<>]+", body):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        private = host in {"localhost"} or host.endswith(
            (".local", ".internal", ".lan")
        )
        try:
            private |= ipaddress.ip_address(host).is_private
        except ValueError:
            pass
        if (
            parsed.username
            or parsed.password
            or private
            or re.search(r"(?i)(token|key|secret|password)=", parsed.query)
        ):
            raise DomainError(
                "invalid_argument",
                "Tools notes cannot contain private connection URLs",
            )


class Views:
    def __init__(self, home, *, allow_hidden=False, session_id=None, now=None):
        self.home = Path(home).resolve()
        self.files = Files(self.home)
        self.allow_hidden = allow_hidden
        self.session_id = session_id
        self.now = now

    def manifest(self):
        try:
            value = json.loads(self.files.read("install.json"))
        except FileNotFoundError as exc:
            raise DomainError(
                "not_installed", "Muse is not installed in this profile"
            ) from exc
        if value.get("plugin") != "hermes-muse" or not value.get("installation_id"):
            raise DomainError(
                "source_unavailable",
                "Invalid installation metadata; explicit repair required",
            )
        return value

    def scope(self):
        return revision(
            canonical([str(self.home.absolute()), self.manifest()["installation_id"]])
        )

    @contextmanager
    def database(self):
        # SQLite opens a pinned, no-follow file descriptor, not a re-resolved path.
        # Muse owns a rollback-journal DB; unknown WAL layouts fail closed.
        import os, stat

        with self.files.directory() as parent:
            fd = os.open(
                "state.db", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            db = None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise DomainError("permission_denied", "Unsafe state database")
                db = sqlite3.connect(f"file:/dev/fd/{fd}?mode=ro", uri=True)
                db.execute("PRAGMA query_only=ON")
                if db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    raise DomainError(
                        "source_unavailable",
                        "Unexpected WAL state; explicit offline repair required",
                    )
                db.execute("BEGIN")
                yield db
            finally:
                if db is not None:
                    db.close()
                os.close(fd)

    def rows(self, kind):
        try:
            with self.database() as db:
                return [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM records WHERE kind=? ORDER BY id", (kind,)
                    )
                ]
        except (FileNotFoundError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise DomainError(
                "source_unavailable", "Stored records unavailable; no repair performed"
            ) from exc

    def record(self, kind, key):
        try:
            with self.database() as db:
                row = db.execute(
                    "SELECT data FROM records WHERE kind=? AND id=?", (kind, key)
                ).fetchone()
                return json.loads(row[0]) if row else None
        except (FileNotFoundError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise DomainError(
                "source_unavailable", "Stored object unavailable; no repair performed"
            ) from exc

    def object(self, kind, key, resource=None):
        if resource and resource.startswith("hidden_files/") and not self.allow_hidden:
            raise DomainError(
                "permission_denied",
                "Hidden artifacts require a host-bound maintenance parent",
            )
        if kind == 'tool_result':
            row = self.record('tool_result', identifier(key))
            if not row or self.session_id is None or row.get('session_id') != self.session_id:
                raise DomainError('permission_denied', 'Tool result is outside this originating session')
            return row['body'], {'id': key, 'action': row['action']}, revision(row['body'].encode())
        if kind in RECORDS:
            row = self.record(RECORDS[kind], identifier(key))
            if (
                not row
                or kind in {"idea", "interest"}
                and row.get("kind", "interest") != kind
            ):
                raise DomainError("not_found", f"content_read: there is no {kind} with id {key!r}. Fix: list {kind} ids first with context(section={kind}) "
                                  f"or content_read(kind={kind}) without data.id, then read one of the returned ids.", field="data.id")
            metadata = {
                k: v
                for k, v in row.items()
                if k not in {"path", "raw", "credentials", "api_key"}
            }
            if kind == "feed":
                if row.get("goal_id"):
                    _, owner, _ = self.object("goal", row["goal_id"])
                    metadata["session_id"] = owner.get("session_id")
                # The stored path must match the fixed owned mapping too.
                expected = f"workspace/your_files/feed/{key}.md"
                if row.get("path") != expected:
                    raise DomainError(
                        "permission_denied", "Invalid Feed file ownership"
                    )
                raw = self.files.read(expected)
                return raw.decode(), metadata, revision(canonical(row) + raw)
            return None, metadata, revision(canonical(row))
        if kind == "goal_file":
            self.object("goal", key)
        relative = mapping(kind, key, resource)
        if kind == "review_report" and not self.record("run", key):
            raise DomainError("not_found", "Report has no recorded run")
        try:
            raw = self.files.read(relative)
        except FileNotFoundError as exc:
            raise DomainError("not_found", "Content unavailable") from exc
        body, metadata = raw.decode("utf-8"), {}
        if kind in {"goal", "preferences"}:
            first, _, body = body.partition("\n")
            if not first.startswith("<!-- hermes-muse ") or not first.endswith(" -->"):
                raise DomainError("source_unavailable", "Invalid managed metadata")
            metadata = json.loads(first[17:-4])
            if kind == "goal" and metadata.get("id") != key:
                raise DomainError(
                    "source_unavailable", "Goal metadata identity mismatch"
                )
        elif kind == "installation":
            manifest = json.loads(body)
            metadata = {
                k: manifest[k]
                for k in (
                    "plugin",
                    "version",
                    "installation_id",
                    "fixed",
                    "jobs",
                    "preparation",
                )
                if k in manifest
            }
            body = None
        elif kind == "tools_note":
            validate_tools_note(body)
        elif kind == "working_lessons":
            match = re.search(
                r"(?m)^## Working lessons\s*\n(.*?)(?=^## |\Z)", body, re.S
            )
            body = match[1] if match else ""
        return body, metadata, revision(raw)

    def keys(self, kind, goal_id=None):
        if kind in RECORDS:
            return [
                r["id"]
                for r in self.rows(RECORDS[kind])
                if kind not in {"idea", "interest"} or r.get("kind", "interest") == kind
            ]
        if kind == "goal":
            return [
                n
                for n in self.files.names("workspace/goals")
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", n)
                and "GOAL.md" in self.files.names("workspace/goals/" + n)
            ]
        if kind == "goal_file":
            self.object("goal", goal_id)
            return [
                "files/" + n
                for n in self.files.names(
                    f"workspace/goals/{identifier(goal_id)}/files"
                )
                if re.fullmatch(r"[A-Za-z0-9_-]+\.md", n)
            ]
        if kind == "note":
            result = [
                "daily:" + n[:-3]
                for n in self.files.names("memory")
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", n)
            ]
            for group, prefix in [("people", "person:"), ("groups", "group:")]:
                result += [
                    prefix + n[:-3]
                    for n in self.files.names("memory/" + group)
                    if re.fullmatch(r"[A-Za-z0-9_-]+\.md", n)
                    and n.upper() != "INDEX.MD"
                ]
            return result
        folder = {"dream": "dreams", "review_report": "workspace/reviews"}.get(kind)
        if folder:
            return [
                n[:-3]
                for n in self.files.names(folder)
                if re.fullmatch(r"[A-Za-z0-9_-]+\.md", n)
            ]
        raise DomainError("invalid_argument", "This kind has no collection")

    def read(self, data):
        allowed = {
            "kind",
            "id",
            "resource",
            "offset",
            "length",
            "revision",
            "part",
            "cursor",
            "limit",
            "topic",
            "query",
            "status",
        }
        if set(data) - allowed:
            raise DomainError(
                "invalid_argument",
                "Unknown content fields: " + ", ".join(sorted(set(data) - allowed)),
            )
        scope = self.scope()
        kind, key = data.get("kind"), data.get("id")
        valid = sorted(COLLECTIONS | set(SINGLETONS) | {"note_index", "tool_result"})
        if kind not in valid:
            raise DomainError("invalid_argument", "content_read: data.kind " + repr(kind) + " is not supported. Valid kinds: "
                              + ", ".join(valid)
                              + ". Notifications are kind=notice."
                              + (f" Did you mean {near[0]!r}?" if (near := difflib.get_close_matches(str(kind), valid, n=1, cutoff=0.75)) else "")
                              + " Fix: set data.kind to one of the valid kinds.", field="data.kind")
        base = {"kind": kind, "observed_at": time.time()}
        if key is None or kind == "goal_file" and not data.get("resource"):
            limit = data.get("limit", 25)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise DomainError("invalid_argument", "limit must be 1..100")
            status = data.get("status")
            if status is not None and (kind != "goal" or status not in {"open", "closed", "dormant"}):
                raise DomainError(
                    "invalid_argument", "status=open|closed|dormant applies only to goals"
                )
            items, goals = [], {}
            for item in self.keys(kind, key):
                body, metadata, rev = self.object(
                    kind,
                    key if kind == "goal_file" else item,
                    item if kind == "goal_file" else None,
                )
                if status and (metadata.get("status") == "active") != (status != "closed"):
                    continue
                if kind == "goal":
                    goals[item] = metadata
                if data.get("topic") and data["topic"] not in {
                    metadata.get("goal_id"),
                    metadata.get("interest_id"),
                }:
                    continue
                if (
                    data.get("query")
                    and data["query"].casefold()
                    not in (
                        (body or "") + " " + json.dumps(metadata, ensure_ascii=False)
                    ).casefold()
                ):
                    continue
                summary = {
                    k: metadata[k]
                    for k in (
                        "title",
                        "status",
                        "created",
                        "position",
                        "goal_id",
                        "interest_id",
                        "why",
                        "rationale",
                        "kind",
                        "session_id",
                        "updated",
                        "deadline",
                        "next_check_at",
                        "waiting_user",
                        "blocked",
                        "source",
                    )
                    if k in metadata
                }
                if isinstance(metadata.get("sources"), list):
                    summary["source_count"] = len(metadata["sources"])
                if kind == "goal" and metadata.get("progress"):
                    summary["latest_progress"] = dict(metadata["progress"][-1])
                truncated = any(isinstance(v, str) and len(v) > 512 for v in summary.values())
                summary = {k: v[:512] if isinstance(v, str) else v for k, v in summary.items()}
                if "latest_progress" in summary:
                    text = summary["latest_progress"].get("text")
                    truncated |= isinstance(text, str) and len(text) > 512
                    summary["latest_progress"]["text"] = str(text)[:512]
                items.append(
                    {
                        "summary_truncated": truncated,
                        "id": key if kind == "goal_file" else item,
                        **({"resource": item} if kind == "goal_file" else {}),
                        "revision": rev,
                        **summary,
                    }
                )
            if kind == "goal":
                from .fade import Fade
                fade = Fade(self.home, list(goals.values()), self.now)
                for row in items:
                    state = fade.of(goals[row["id"]])
                    if state and state["state"] == "dormant":
                        row.update(dormant_since=state["dormant_since"], fade_at=state["fade_at"], fade_due=state["fade_due"])
                if status == "dormant":
                    items = [row for row in items if "dormant_since" in row]
                # Needs-you, blocked, in progress; closed goals last, newest first.
                def goal_order(r):
                    if r.get("status") != "active":
                        return (3, -(r.get("updated") or 0), r["id"])
                    rank = 0 if r.get("waiting_user") else 1 if r.get("blocked") else 2
                    return (rank, r.get("next_check_at") or 0, r["id"])

                items.sort(key=goal_order)
            else:
                items.sort(
                    key=lambda r: (-r.get("position", 0), r["id"], r.get("resource", ""))
                )
            snapshot = revision(
                canonical(
                    [scope, kind, key, data.get("topic"), data.get("query"), items]
                )
            )
            offset = 0
            if data.get("cursor"):
                try:
                    cursor = json.loads(base64.urlsafe_b64decode(data["cursor"]))
                    if (
                        cursor["snapshot"] != snapshot
                        or type(cursor["offset"]) is not int
                        or cursor["offset"] < 0
                    ):
                        raise ValueError()
                    offset = cursor["offset"]
                except Exception as exc:
                    raise DomainError(
                        "cursor_expired", "data.cursor is no longer valid: the list changed or the cursor was edited. Fix: resend without data.cursor to restart "
                        "from the first page, then pass each next_cursor back exactly as returned.",
                        field="data.cursor", hint="drop data.cursor and restart", retryable=True
                    ) from exc
            end = min(len(items), offset + limit)
            while end > offset + 1 and len(canonical(items[offset:end])) > 128 * 1024:
                end -= 1
            cursor = (
                base64.urlsafe_b64encode(
                    canonical({"snapshot": snapshot, "offset": end})
                ).decode()
                if end < len(items)
                else None
            )
            extra = {}
            if kind == "idea":
                # Lets an empty Ideas page say when the generating review last ran.
                reviews = [
                    r.get("started") or 0
                    for r in self.rows("run")
                    if r.get("job") == "nightly-review"
                ]
                extra["last_review_at"] = max(reviews, default=None)
            return {
                **base,
                **extra,
                "items": items[offset:end],
                "next_cursor": cursor,
                "snapshot_revision": snapshot,
                "complete": cursor is None,
                "total": len(items),
            }
        body, metadata, rev = self.object(kind, key, data.get("resource"))
        offset, length = data.get("offset", 0), data.get("length", 12000)
        if (
            type(offset) is not int
            or offset < 0
            or type(length) is not int
            or not 1 <= length <= 24000
        ):
            raise DomainError("invalid_argument", "Use offset >= 0 and length 1..24000")
        if offset and not data.get("revision"):
            raise DomainError(
                "revision_required", "Continuation requires first-page revision"
            )
        if data.get("revision") and data["revision"] != rev:
            raise DomainError(
                "revision_conflict",
                "Content changed; restart reading",
                current_revision=rev,
            )
        part = data.get('part', 'body')
        if part not in {'body', 'metadata'}:
            raise DomainError('invalid_argument', 'part must be body or metadata')
        metadata_ref = None
        if part == 'metadata':
            body, metadata = json.dumps(metadata, ensure_ascii=False, sort_keys=True), {}
        elif len(canonical(metadata)) > 32768:
            metadata_ref = {'action': 'content_read', 'data': {'kind': kind, 'id': key, 'part': 'metadata',
                            'revision': rev, **({'resource': data['resource']} if data.get('resource') else {})}}
            metadata = {k: v for k, v in metadata.items() if (k in {'id', 'status', 'title'} and len(str(v)) <= 1000)
                        }
        end = offset + length
        return {
            **base,
            "id": key,
            "revision": rev,
            "metadata": metadata,
            "metadata_complete": metadata_ref is None,
            "metadata_ref": metadata_ref,
            "body_format": "json_metadata_text" if part == "metadata" else "text",
            "body": body[offset:end] if body is not None else None,
            "total_chars": len(body or ""),
            "next_offset": end if end < len(body or "") else None,
            "complete": end >= len(body or ""),
        }
