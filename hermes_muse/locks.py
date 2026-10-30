"""Reentrant process/thread locks for the profile's shared mutation boundary."""

from contextlib import contextmanager
import os
import threading
import fcntl

_guard = threading.Lock()
_locks = {}
_local = threading.local()


@contextmanager
def file_lock(path):
    # One lock order for install/audit/content and record transactions.
    if path.name != "mutations.lock":
        with file_lock(path.parent / "mutations.lock"):
            with _file_lock(path):
                yield
    else:
        with _file_lock(path):
            yield


@contextmanager
def _file_lock(path):
    key = str(path.absolute())
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", {})
        _local.held = held
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        from .content_io import Files

        if path.parent.name != "muse":
            raise ValueError("Lock must belong to the profile Muse root")
        with Files(path.parent.parent).directory() as parent:
            fd = os.open(
                path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[key] = fd
            try:
                yield
            finally:
                held.pop(key, None)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
