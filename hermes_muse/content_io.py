"""Descriptor-relative content access; no directory creation during reads."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import uuid


class DomainError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


def check_cancelled():
    from .host import interrupted
    try:
        stop = interrupted()
    except ImportError:
        return
    if stop:
        raise DomainError(
            "outcome_unknown",
            "Host interrupted this operation; inspect the original request before retry",
        )


def parts(relative):
    if (
        not isinstance(relative, str)
        or len(relative) > 240
        or any(c in relative for c in ("\\", "\x00", "%", ":"))
    ):
        raise DomainError("invalid_argument", "Invalid resource")
    result = relative.split("/")
    if any(
        not p
        or p.startswith(".")
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", p)
        for p in result
    ):
        raise DomainError("invalid_argument", "Invalid resource segments")
    return result


class Files:
    def __init__(self, home):
        self.root = Path(home) / "muse"

    @contextmanager
    def directory(self, relative="", *, create=False):
        # Opening each component with O_NOFOLLOW closes the check/use symlink gap.
        components = list(self.root.absolute().parts[1:]) + (
            parts(relative) if relative else []
        )
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in components:
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o700, dir_fd=fd)
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                os.close(fd)
                fd = child
            yield fd
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise DomainError(
                "permission_denied", "Unsafe or unavailable content directory"
            ) from exc
        finally:
            os.close(fd)

    def read(self, relative):
        components = parts(relative)
        with self.directory("/".join(components[:-1])) as parent:
            try:
                fd = os.open(
                    components[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "rb") as stream:
                    import stat

                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise DomainError(
                            "permission_denied", "Content must be a regular file"
                        )
                    raw = stream.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise DomainError(
                            "size_limit", "Stored content exceeds safe read limit"
                        )
                    return raw
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise
                raise DomainError("permission_denied", "Unsafe content file") from exc

    def names(self, relative):
        try:
            with self.directory(relative) as fd:
                return sorted(os.listdir(fd))
        except FileNotFoundError:
            return []

    def replace(self, relative, raw, expected):
        components = parts(relative)
        with self.directory("/".join(components[:-1]), create=True) as parent:
            name, temporary = components[-1], ".muse-" + uuid.uuid4().hex

            def current():
                try:
                    return self.read(relative)
                except FileNotFoundError:
                    return None

            before = current()
            actual = revision(before) if before is not None else "absent"
            if expected != actual:
                raise DomainError(
                    "revision_conflict",
                    "File changed concurrently; reread and merge current content",
                    current_revision=actual,
                )
            if raw == before:
                return False
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if current() != before:
                    # Retain the proposed version for authorized conflict recovery.
                    raise DomainError(
                        "revision_conflict",
                        "Concurrent external edit; proposal retained; reread before retry",
                    )
                check_cancelled()
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            except DomainError:
                raise
            except BaseException:
                os.unlink(temporary, dir_fd=parent)
                raise
            return True

    def unlink(self, relative, expected):
        components = parts(relative)
        with self.directory("/".join(components[:-1])) as parent:
            raw = self.read(relative)
            if revision(raw) != expected:
                raise DomainError(
                    "revision_conflict",
                    "Content changed before deletion",
                    current_revision=revision(raw),
                )
            check_cancelled()
            os.unlink(components[-1], dir_fd=parent)
            os.fsync(parent)
