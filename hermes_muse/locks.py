"""Reentrant process/thread locks for the profile's shared mutation boundary."""

from contextlib import contextmanager
import os
import threading
if os.name != "nt":
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
def _windows_lock(path):
    import pywintypes
    import win32con
    import win32file
    from .content_io import Files, _windows_child, parts

    if path.parent.name.lower() != "muse":
        raise ValueError("Lock must belong to the profile Muse root")
    parts(path.name)
    with Files(path.parent.parent).directory(create=True) as parent:
        handle = _windows_child(parent, path.name, create=True, writable=True)
        overlapped = pywintypes.OVERLAPPED()
        try:
            win32file.LockFileEx(handle, win32con.LOCKFILE_EXCLUSIVE_LOCK, 0, 1, overlapped)
            try:
                yield
            finally:
                win32file.UnlockFileEx(handle, 0, 1, overlapped)
        finally:
            handle.Close()


def _lock_key(path):
    path = path.absolute()
    if os.name == "nt":
        import pywintypes
        import win32api
        missing = []
        existing = path
        while True:
            try:
                long_path = win32api.GetLongPathName(str(existing))
                break
            except pywintypes.error as exc:
                if exc.winerror not in {2, 3} or existing == existing.parent:
                    raise
                missing.append(existing.name)
                existing = existing.parent
        path = type(path)(long_path).joinpath(*reversed(missing))
    # This normalizes spelling only. Secure native traversal still validates
    # every component when opening the lock; aliases do not authorize a path.
    return os.path.normcase(str(path))


@contextmanager
def _file_lock(path):
    key = _lock_key(path)
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", {})
        _local.held = held
        if key in held:
            yield
            return
        if os.name == "nt":
            with _windows_lock(path):
                held[key] = True
                try:
                    yield
                finally:
                    held.pop(key, None)
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
