"""Descriptor-relative content access; no directory creation during reads."""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

WINDOWS = os.name == "nt"
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


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
    if WINDOWS and any(
        p.endswith(".") or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?", p)
        for p in result
    ):
        # Windows maps these to devices or strips the trailing dot (an alias of another name).
        raise DomainError("invalid_argument", "Reserved Windows resource name")
    return result


def at(parent, name):
    """Address a child of a Files.directory handle for os calls: (path, dir_fd)."""
    return (os.path.join(parent, name), None) if WINDOWS else (name, parent)


def open_at(parent, name, flags, mode=0o777):
    """os.open a child without following a link at the leaf."""
    if not WINDOWS:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=parent)
    path = os.path.join(parent, name)
    try:
        _refuse_reparse(path)
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
    # O_BINARY: no newline translation, so revisions hash the bytes on disk.
    return os.open(path, flags | os.O_BINARY, mode)


def _refuse_reparse(path):
    """Windows has no O_NOFOLLOW or dir_fd: refuse symlinks and junctions by attribute."""
    if os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(errno.ELOOP, "Reparse point refused", path)


def _windows_directory(root, components, create):
    # shortcut: each component is checked and then used by absolute path, so a junction swapped in
    # between is not caught; move to handle-relative NtCreateFile if profiles get untrusted writers.
    path = root.anchor
    for component in components:
        path = os.path.join(path, component)
        try:
            _refuse_reparse(path)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(path, 0o700)
    return path


class Files:
    def __init__(self, home):
        self.root = Path(home) / "muse"

    @contextmanager
    def directory(self, relative="", *, create=False):
        # Opening each component with O_NOFOLLOW closes the check/use symlink gap.
        components = list(self.root.absolute().parts[1:]) + (
            parts(relative) if relative else []
        )
        fd = None if WINDOWS else os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            if WINDOWS:
                yield _windows_directory(self.root.absolute(), components, create)
                return
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
            if fd is not None:
                os.close(fd)

    def read(self, relative):
        components = parts(relative)
        with self.directory("/".join(components[:-1])) as parent:
            try:
                fd = open_at(parent, components[-1], os.O_RDONLY | NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
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
            fd = open_at(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            (source, dir_fd), (target, _) = at(parent, temporary), at(parent, name)
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
                os.replace(source, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                if not WINDOWS:  # Windows cannot fsync a directory
                    os.fsync(parent)
            except DomainError:
                raise
            except BaseException:
                os.unlink(source, dir_fd=dir_fd)
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
            path, dir_fd = at(parent, components[-1])
            os.unlink(path, dir_fd=dir_fd)
            if not WINDOWS:
                os.fsync(parent)
