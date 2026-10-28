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
    try:
        from tools.interrupt import is_interrupted
    except ImportError:
        return
    if is_interrupted():
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
    if os.name == "nt":
        for component in result:
            _windows_name(component)
    return result


def _windows_name(name):
    stem = name.split(".", 1)[0].upper()
    if (not name or name in {".", ".."} or name.endswith((".", " "))
            or any(ord(c) < 32 or c in '\\\\/:<>"|?*' for c in name)
            or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)):
        raise DomainError("invalid_argument", "Unsafe Windows path component")


@contextmanager
def _windows_directory(root, relative="", *, create=False):
    """Retain a handle-relative directory chain; never reopen an absolute child.

    Ancestors deny delete sharing; write sharing permits native publication.
    FILE_WRITE_ATTRIBUTES can change an empty directory into a junction even
    without write sharing. Confinement therefore comes from
    NtCreateFile's RootDirectory, not from pinning and absolute child paths.
    Incompatible occupants fail closed.
    """
    import win32api
    import win32con
    import win32file

    root = root.absolute()
    if not re.fullmatch(r"[A-Za-z]:\\", root.anchor):
        raise DomainError("permission_denied", "Windows Muse requires a local drive")
    if (win32file.GetDriveType(root.anchor) != win32con.DRIVE_FIXED
            or win32api.GetVolumeInformation(root.anchor)[4] != "NTFS"):
        raise DomainError("permission_denied", "Windows Muse requires local NTFS")
    handles = []
    try:
        # A volume root has no user-controlled parent component to traverse.
        handle = win32file.CreateFile(
            root.anchor, 0x100081, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None, win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000,  # OPEN_REPARSE_POINT
            None,
        )
        handles.append(handle)
        for component in list(root.parts[1:]) + (parts(relative) if relative else []):
            handle = _windows_child(handle, component, directory=True, create=create)
            handles.append(handle)
        yield handle
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise DomainError("permission_denied", "Unsafe or occupied Windows content directory") from exc
    finally:
        for handle in reversed(handles):
            handle.Close()


def _windows_child(parent, name, *, directory=False, create=False, writable=False,
                   exclusive=False, delete=False):
    """Open one literal child against a retained native parent handle."""
    import ctypes
    from ctypes import wintypes
    import pywintypes
    import win32file

    _windows_name(name)

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", wintypes.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ctypes.POINTER(UnicodeString)),
                    ("Attributes", wintypes.ULONG), ("SecurityDescriptor", ctypes.c_void_p),
                    ("SecurityQualityOfService", ctypes.c_void_p)]

    class IoStatus(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    buffer = ctypes.create_unicode_buffer(name)
    encoded = name.encode("utf-16-le")
    string = UnicodeString(len(encoded), len(encoded) + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), int(parent), ctypes.pointer(string),
                                  0x40, None, None)  # OBJ_CASE_INSENSITIVE
    security_buffer = None
    if create or exclusive:
        import win32api
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), 8)
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        sid = win32security.ConvertSidToStringSid(user)
        inheritance = "OICI" if directory else ""
        sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"D:P(A;{inheritance};FA;;;{sid})(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;BA)", 1)
        security_buffer = ctypes.create_string_buffer(bytes(sd))
        attributes.SecurityDescriptor = ctypes.addressof(security_buffer)
    status, handle = IoStatus(), wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    open_child = ntdll.NtCreateFile
    open_child.restype = wintypes.LONG
    open_child.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
                          ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatus),
                          ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
                          wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    result = open_child(
        ctypes.byref(handle), (0x100081 if directory else (0x12019F if writable else 0x120089)) | (0x10000 if delete else 0),
        ctypes.byref(attributes), ctypes.byref(status), None, 0,
        3,  # deny DELETE; WRITE sharing is required by NT rename's parent open
        2 if exclusive else (3 if create else 1),  # CREATE / OPEN_IF / OPEN
        0x200020 | (1 if directory else 0x40),  # REPARSE_POINT, synchronous, type
        None, 0,
    )
    if result < 0:
        convert = ntdll.RtlNtStatusToDosError
        convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
        raise ctypes.WinError(convert(result))
    opened = pywintypes.HANDLE(handle.value)
    try:
        info = win32file.GetFileInformationByHandle(opened)
        if info[0] & 0x400 or (not directory and info[7] != 1):
            raise DomainError("permission_denied", "Reparse points and linked content are not permitted")
        return opened
    except BaseException:
        opened.Close()
        raise


def _windows_set_information(handle, information, kind):
    """Use NT handle-relative information (Win32 rename reopens the parent)."""
    import ctypes
    from ctypes import wintypes

    class IoStatus(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    native = ctypes.WinDLL("ntdll")
    call = native.NtSetInformationFile
    call.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoStatus), ctypes.c_void_p,
                     wintypes.ULONG, wintypes.ULONG]
    call.restype = wintypes.LONG
    result = call(int(handle), ctypes.byref(IoStatus()), ctypes.byref(information),
                  ctypes.sizeof(information), kind)
    if result < 0:
        convert = native.RtlNtStatusToDosError
        convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
        raise ctypes.WinError(convert(result))


def _windows_rename(handle, parent, name):
    import ctypes
    from ctypes import wintypes
    encoded = name.encode("utf-16-le")

    class Rename(ctypes.Structure):
        _fields_ = [("ReplaceIfExists", wintypes.BOOLEAN), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.ULONG), ("FileName", ctypes.c_byte * len(encoded))]

    info = Rename(True, int(parent), len(encoded))
    ctypes.memmove(ctypes.addressof(info) + Rename.FileName.offset, encoded, len(encoded))
    _windows_set_information(handle, info, 10)  # FileRenameInformation


def _windows_delete(handle):
    import ctypes
    _windows_set_information(handle, ctypes.c_ubyte(1), 13)  # FileDispositionInformation


def _windows_names(handle):
    """Enumerate the retained directory itself, including multi-buffer results."""
    import ctypes
    from ctypes import wintypes
    import struct

    class IoStatus(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    ntdll = ctypes.WinDLL("ntdll")
    query = ntdll.NtQueryDirectoryFile
    query.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.POINTER(IoStatus), ctypes.c_void_p, wintypes.ULONG,
                      wintypes.ULONG, wintypes.BOOLEAN, ctypes.c_void_p, wintypes.BOOLEAN]
    query.restype = wintypes.LONG
    names, restart = [], True
    while True:
        buffer, status = ctypes.create_string_buffer(65536), IoStatus()
        result = query(int(handle), None, None, None, ctypes.byref(status), buffer,
                       ctypes.sizeof(buffer), 12, False, None, restart)  # FileNamesInformation
        restart = False
        if result == -2147483642:  # STATUS_NO_MORE_FILES
            return sorted(names)
        if result < 0:
            convert = ntdll.RtlNtStatusToDosError
            convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
            raise ctypes.WinError(convert(result))
        raw, offset = buffer.raw[:status.Information], 0
        while True:
            following, _, length = struct.unpack_from("<III", raw, offset)
            name = raw[offset + 12:offset + 12 + length].decode("utf-16-le")
            if name not in {".", ".."}:
                names.append(name)
            if not following:
                break
            offset += following


@contextmanager
def readonly_database(path, *, timeout=2):
    """Read-only connection with deterministic close, protected on Windows."""
    if os.name == "nt":
        with windows_database(path, timeout=timeout) as db:
            yield db
    else:
        import sqlite3
        db = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=timeout)
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()


@contextmanager
def windows_database(path, *, writable=False, create=False, timeout=15):
    """Pin the entire NTFS chain and a non-deletable leaf for SQLite's lifetime.

    A retained child makes its parent nonempty: NTFS cannot turn that directory
    into a junction. All ancestors retain a non-deletable child too. Inspect the
    final parent *after* acquiring the leaf, closing the empty-directory reparse
    race before SQLite performs its ordinary filename open. WRITE sharing stays
    enabled for SQLite's normal concurrent writer and rollback-journal behavior.
    """
    import sqlite3
    import win32file

    path = Path(path).absolute()
    with _windows_directory(path.parent, create=create) as parent:
        leaf = _windows_child(parent, path.name, create=create, writable=writable)
        db = None
        try:
            if win32file.GetFileInformationByHandle(parent)[0] & 0x400:
                raise DomainError("permission_denied", "Database parent changed into a reparse point")
            if win32file.GetFileType(leaf) != 1:
                raise DomainError("permission_denied", "Unsafe database file")
            db = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                 uri=True, timeout=timeout, isolation_level=None)
            if not writable:
                db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            if db is not None:
                db.close()
            leaf.Close()


class Files:
    def __init__(self, home, *, workspace="muse"):
        self.root = Path(home) / workspace

    @contextmanager
    def directory(self, relative="", *, create=False):
        if os.name == "nt":
            with _windows_directory(self.root, relative, create=create) as handle:
                yield handle
            return
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
            if os.name == "nt":
                import win32file

                handle = _windows_child(parent, components[-1])
                try:
                    if win32file.GetFileType(handle) != 1:
                        raise DomainError("permission_denied", "Content must be a regular file")
                    raw = win32file.ReadFile(handle, 1024 * 1024 + 1)[1]
                    if len(raw) > 1024 * 1024:
                        raise DomainError("size_limit", "Stored content exceeds safe read limit")
                    return raw
                finally:
                    handle.Close()
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
                if os.name == "nt":
                    return _windows_names(fd)
                return sorted(os.listdir(fd))
        except FileNotFoundError:
            return []

    def _windows_replace(self, relative, raw, expected):
        import win32file
        components = parts(relative)
        with self.directory("/".join(components[:-1]), create=True) as parent:
            def current():
                try:
                    return self.read(relative)
                except FileNotFoundError:
                    return None
            before = current()
            actual = revision(before) if before is not None else "absent"
            if actual != expected:
                raise DomainError("revision_conflict", "File changed concurrently; reread and merge current content",
                                  current_revision=actual)
            if before == raw:
                return False
            temporary = _windows_child(parent, ".muse-" + uuid.uuid4().hex,
                                       exclusive=True, writable=True, delete=True)
            try:
                win32file.WriteFile(temporary, raw)
                win32file.FlushFileBuffers(temporary)
                if current() != before:
                    raise DomainError("revision_conflict", "Concurrent external edit; proposal retained; reread before retry")
                check_cancelled()
                _windows_rename(temporary, parent, components[-1])
            except DomainError:
                raise
            except BaseException:
                _windows_delete(temporary)
                raise
            finally:
                temporary.Close()
            return True

    def replace(self, relative, raw, expected):
        if os.name == "nt":
            return self._windows_replace(relative, raw, expected)
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
        if os.name == "nt":
            import win32file
            components = parts(relative)
            with self.directory("/".join(components[:-1])) as parent:
                handle = _windows_child(parent, components[-1], delete=True)
                try:
                    raw = win32file.ReadFile(handle, 1024 * 1024 + 1)[1]
                    if len(raw) > 1024 * 1024:
                        raise DomainError("size_limit", "Stored content exceeds safe read limit")
                    if revision(raw) != expected:
                        raise DomainError("revision_conflict", "Content changed before deletion",
                                          current_revision=revision(raw))
                    check_cancelled()
                    _windows_delete(handle)
                finally:
                    handle.Close()
            return
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
