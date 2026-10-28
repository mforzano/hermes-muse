"""Ordinary-user native Windows compatibility tests; no installed host required."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest

SCRATCH = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsImports(unittest.TestCase):
    def test_manifest_declares_native_and_timezone_dependencies(self):
        import ast
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
        section = manifest.split("python_dependencies:\n", 1)[1].split("\ntags:", 1)[0]
        requirements = [ast.literal_eval(line.strip()[2:]) for line in section.splitlines() if line.startswith("  - ")]
        self.assertEqual({r.split(">=", 1)[0] for r in requirements}, {"pywin32", "tzdata"})
        self.assertTrue(all(r.endswith('; sys_platform == "win32"') for r in requirements))

    def test_plugin_modules_import_without_posix_modules(self):
        for name in ("locks", "service", "host", "runtime", "migration", "views"):
            with self.subTest(module=name):
                importlib.import_module("hermes_muse." + name)


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsLocks(unittest.TestCase):
    def test_short_path_alias_reentrancy(self):
        import win32api
        from hermes_muse.locks import _lock_key, file_lock
        with tempfile.TemporaryDirectory(prefix="muse long lock alias ", dir=SCRATCH) as tmp:
            long_path = Path(tmp) / "muse" / "install.lock"
            short_path = Path(win32api.GetShortPathName(tmp)) / "muse" / "install.lock"
            self.assertEqual(_lock_key(long_path), _lock_key(short_path))
            with file_lock(long_path), file_lock(short_path):
                self.assertTrue(long_path.exists())

    def test_case_alias_reentrancy(self):
        from hermes_muse.locks import file_lock
        with tempfile.TemporaryDirectory(prefix="muse-case-", dir=SCRATCH) as tmp:
            path = Path(tmp) / "muse" / "install.lock"
            with file_lock(path), file_lock(Path(str(path).upper())):
                self.assertTrue(path.exists())

    def test_process_contention_and_profile_isolation(self):
        import subprocess
        import sys
        from hermes_muse.locks import file_lock
        code = ("from pathlib import Path; import sys; from hermes_muse.locks import file_lock; "
                "print('waiting', flush=True)\nwith file_lock(Path(sys.argv[1])): print('acquired', flush=True)")
        with tempfile.TemporaryDirectory(prefix="muse-process-", dir=SCRATCH) as tmp:
            path = Path(tmp) / "a" / "muse" / "install.lock"
            with file_lock(path):
                child = subprocess.Popen([sys.executable, "-c", code, str(path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "waiting")
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=0.3)
                    sibling = subprocess.run([sys.executable, "-c", code,
                                              str(Path(tmp) / "b" / "muse" / "install.lock")],
                                             capture_output=True, text=True, timeout=15)
                    self.assertEqual(sibling.returncode, 0, sibling.stderr)
                    self.assertIn("acquired", sibling.stdout)
                except BaseException:
                    child.kill()
                    child.communicate()
                    raise
            out, err = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("acquired", out)

    def test_nested_profile_locks_release_after_exception(self):
        from hermes_muse.locks import file_lock
        with tempfile.TemporaryDirectory(prefix="muse-lock-", dir=SCRATCH) as tmp:
            path = Path(tmp) / "muse" / "install.lock"
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with file_lock(path), file_lock(path), file_lock(path.parent / "content.lock"):
                    raise RuntimeError("fixture")
            with file_lock(path):
                self.assertTrue(path.is_file())


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsFiles(unittest.TestCase):
    def test_reject_windows_aliases_without_normalizing(self):
        from hermes_muse.content_io import DomainError, parts
        for name in ("CON", "con.md", "AUX.txt", "NUL", "COM1.md", "LPT9", "notes."):
            with self.subTest(name=name), self.assertRaises(DomainError):
                parts("workspace/" + name)
        self.assertEqual(parts("workspace/console.md"), ["workspace", "console.md"])

    def test_missing_read_does_not_create_profile(self):
        from hermes_muse.content_io import Files
        with tempfile.TemporaryDirectory(prefix="muse-missing-", dir=SCRATCH) as tmp:
            home = Path(tmp) / "missing"
            with self.assertRaises(FileNotFoundError):
                Files(home).read("TOOLS.md")
            self.assertFalse(home.exists())

    def test_read_rejects_hardlinked_external_file(self):
        from hermes_muse.content_io import DomainError, Files
        with tempfile.TemporaryDirectory(prefix="muse-linked-", dir=SCRATCH) as tmp:
            root = Path(tmp) / "muse"
            root.mkdir()
            outside = Path(tmp) / "outside.md"
            outside.write_bytes(b"outside sentinel")
            os.link(outside, root / "TOOLS.md")
            with self.assertRaises(DomainError):
                Files(tmp).read("TOOLS.md")
            self.assertEqual(outside.read_bytes(), b"outside sentinel")

    def test_read_limit_and_empty_file(self):
        from hermes_muse.content_io import DomainError, Files
        with tempfile.TemporaryDirectory(prefix="muse-limits-", dir=SCRATCH) as tmp:
            root = Path(tmp) / "muse"
            root.mkdir()
            target = root / "TOOLS.md"
            target.write_bytes(b"")
            self.assertEqual(Files(tmp).read("TOOLS.md"), b"")
            target.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaises(DomainError) as caught:
                Files(tmp).read("TOOLS.md")
            self.assertEqual(caught.exception.code, "size_limit")
            from hermes_muse.content_io import revision
            with self.assertRaises(DomainError) as caught:
                Files(tmp).unlink("TOOLS.md", revision(b"x" * (1024 * 1024 + 1)))
            self.assertEqual(caught.exception.code, "size_limit")
            self.assertTrue(target.exists())

    def test_junction_ancestor_rejected_before_read_or_lock_creation(self):
        import subprocess
        from unittest.mock import patch
        from hermes_muse.content_io import DomainError, Files
        from hermes_muse.locks import file_lock
        with tempfile.TemporaryDirectory(prefix="muse-junction-", dir=SCRATCH) as tmp:
            base = Path(tmp)
            outside = base / "outside"
            (outside / "muse").mkdir(parents=True)
            sentinel = outside / "muse" / "TOOLS.md"
            sentinel.write_bytes(b"out of profile")
            junction = base / "profile"
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            try:
                with patch("win32file.ReadFile", side_effect=AssertionError("unexpected read")):
                    with self.assertRaises(DomainError):
                        Files(junction).read("TOOLS.md")
                with self.assertRaises(DomainError):
                    with file_lock(junction / "muse" / "install.lock"):
                        self.fail("junction accepted")
                from hermes_muse.store import Store
                from hermes_muse.views import Views
                from hermes_muse.operations import reader
                from hermes_muse.runtime import Runtime
                for action in (lambda: Runtime(None, junction), lambda: Store(junction),
                               lambda: Files(junction).replace("TOOLS.md", b"escape", "absent"),
                               lambda: Files(junction).unlink("TOOLS.md", "absent")):
                    with self.assertRaises(DomainError):
                        action()
                for context in (Views(junction).database(), reader(junction / "muse/state.db")):
                    with self.assertRaises(DomainError):
                        with context:
                            self.fail("junction database accepted")
                self.assertEqual(sorted(p.name for p in (outside / "muse").iterdir()), ["TOOLS.md"])
                self.assertEqual(sentinel.read_bytes(), b"out of profile")
            finally:
                junction.rmdir()

    def test_retained_directory_blocks_replacement(self):
        from hermes_muse.content_io import Files
        with tempfile.TemporaryDirectory(prefix="muse-pinned-", dir=SCRATCH) as tmp:
            root = Path(tmp) / "muse"
            root.mkdir()
            (root / "TOOLS.md").write_bytes(b"inside")
            with Files(tmp).directory():
                with self.assertRaises(OSError):
                    root.rename(root.with_name("moved"))
                self.assertEqual(Files(tmp).read("TOOLS.md"), b"inside")

    def test_new_directories_have_private_protected_acl(self):
        import win32security
        from hermes_muse.content_io import Files
        with tempfile.TemporaryDirectory(prefix="muse-acl-", dir=SCRATCH) as tmp:
            Files(tmp).replace("workspace/TOOLS.md", b"private", "absent")
            for relative in ("muse", "muse/workspace", "muse/workspace/TOOLS.md"):
                sd = win32security.GetFileSecurity(str(Path(tmp) / relative),
                                                  win32security.DACL_SECURITY_INFORMATION)
                self.assertTrue(sd.GetSecurityDescriptorControl()[0] & 0x1000)
                acl = sd.GetSecurityDescriptorDacl()
                allowed = {win32security.ConvertSidToStringSid(acl.GetAce(i)[2])
                           for i in range(acl.GetAceCount())}
                self.assertNotIn("S-1-1-0", allowed)
                self.assertNotIn("S-1-5-32-545", allowed)
                self.assertNotIn("S-1-5-11", allowed)

    def test_atomic_replace_revision_and_unlink(self):
        from hermes_muse.content_io import DomainError, Files, revision
        with tempfile.TemporaryDirectory(prefix="muse-write-", dir=SCRATCH) as tmp:
            files = Files(tmp)
            self.assertTrue(files.replace("workspace/TOOLS.md", b"first", "absent"))
            self.assertEqual(files.read("workspace/TOOLS.md"), b"first")
            self.assertFalse(files.replace("workspace/TOOLS.md", b"first", revision(b"first")))
            with self.assertRaises(DomainError) as caught:
                files.replace("workspace/TOOLS.md", b"wrong", "absent")
            self.assertEqual(caught.exception.code, "revision_conflict")
            self.assertTrue(files.replace("workspace/TOOLS.md", b"second", revision(b"first")))
            files.unlink("workspace/TOOLS.md", revision(b"second"))
            self.assertEqual(files.names("workspace"), [])

    def test_atomic_write_retains_proposal_on_external_edit_and_cancellation(self):
        from unittest.mock import patch
        import win32file
        from hermes_muse.content_io import DomainError, Files, revision
        with tempfile.TemporaryDirectory(prefix="muse-cas-", dir=SCRATCH) as tmp:
            files = Files(tmp)
            files.replace("TOOLS.md", b"before", "absent")
            flush = win32file.FlushFileBuffers
            def external_edit(handle):
                flush(handle)
                (Path(tmp) / "muse" / "TOOLS.md").write_bytes(b"external edit")
            with patch("win32file.FlushFileBuffers", side_effect=external_edit):
                with self.assertRaises(DomainError) as caught:
                    files.replace("TOOLS.md", b"proposal", revision(b"before"))
            self.assertEqual(caught.exception.code, "revision_conflict")
            self.assertEqual(files.read("TOOLS.md"), b"external edit")
            proposals = [n for n in files.names("") if n.startswith(".muse-")]
            self.assertEqual(len(proposals), 1)
            self.assertEqual((Path(tmp) / "muse" / proposals[0]).read_bytes(), b"proposal")
            with patch("hermes_muse.content_io.check_cancelled",
                       side_effect=DomainError("outcome_unknown", "interrupted")):
                with self.assertRaises(DomainError) as caught:
                    files.replace("TOOLS.md", b"cancelled proposal", revision(b"external edit"))
            self.assertEqual(caught.exception.code, "outcome_unknown")
            self.assertEqual(files.read("TOOLS.md"), b"external edit")
            self.assertEqual(len([n for n in files.names("") if n.startswith(".muse-")]), 2)

    def test_occupied_target_refuses_publication_without_copy_fallback(self):
        import win32file
        from hermes_muse.content_io import DomainError, Files, revision
        with tempfile.TemporaryDirectory(prefix="muse-occupied-", dir=SCRATCH) as tmp:
            files = Files(tmp)
            files.replace("TOOLS.md", b"before", "absent")
            target = Path(tmp) / "muse" / "TOOLS.md"
            occupied = win32file.CreateFile(str(target), 0x80000000, 3, None, 3, 0, None)
            try:
                with self.assertRaises(DomainError) as caught:
                    files.replace("TOOLS.md", b"proposal", revision(b"before"))
                self.assertEqual(caught.exception.code, "permission_denied")
                self.assertEqual(target.read_bytes(), b"before")
                self.assertEqual(files.names(""), ["TOOLS.md"])
            finally:
                occupied.Close()
            self.assertTrue(files.replace("TOOLS.md", b"after", revision(b"before")))

    def test_names_are_sorted_and_missing_directory_stays_absent(self):
        from hermes_muse.content_io import Files
        with tempfile.TemporaryDirectory(prefix="muse-names-", dir=SCRATCH) as tmp:
            files = Files(tmp)
            self.assertEqual(files.names("workspace"), [])
            self.assertFalse((Path(tmp) / "muse").exists())
            root = Path(tmp) / "muse" / "workspace"
            root.mkdir(parents=True)
            expected = [f"entry-{n:04d}.md" for n in range(1500)] + ["空间.md"]
            for name in expected:
                (root / name).touch()
            self.assertEqual(files.names("workspace"), sorted(expected))

    def test_in_place_junction_does_not_redirect_handle_relative_read(self):
        import struct
        import win32file
        from unittest.mock import patch
        from hermes_muse import content_io
        with tempfile.TemporaryDirectory(prefix="muse-inplace-", dir=SCRATCH) as tmp:
            root = Path(tmp) / "muse"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "TOOLS.md").write_bytes(b"outside sentinel")
            substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
            display = str(outside).encode("utf-16-le")
            paths = substitute + b"\x00\x00" + display + b"\x00\x00"
            payload = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0,
                                  0, len(substitute), len(substitute) + 2, len(display)) + paths
            original = content_io._windows_child
            attacked = []

            def open_child(parent, name, **options):
                handle = original(parent, name, **options)
                if options.get("directory") and name == "muse":
                    attacker = win32file.CreateFile(str(root), 0x100, 7, None, 3, 0x02200000, None)
                    try:
                        win32file.DeviceIoControl(attacker, 0x900A4, payload, None)
                        attacked.append(True)
                    finally:
                        attacker.Close()
                return handle

            try:
                with patch.object(content_io, "_windows_child", side_effect=open_child), \
                     patch("win32file.ReadFile", side_effect=AssertionError("outside read")):
                    with self.assertRaises((content_io.DomainError, FileNotFoundError)):
                        content_io.Files(tmp).read("TOOLS.md")
                self.assertEqual(attacked, [True], "attack must actually execute")
                self.assertEqual((outside / "TOOLS.md").read_bytes(), b"outside sentinel")
            finally:
                root.rmdir()

    def test_in_place_junction_does_not_redirect_handle_relative_lock(self):
        import struct
        import win32file
        from unittest.mock import patch
        from hermes_muse import content_io
        from hermes_muse.locks import file_lock
        with tempfile.TemporaryDirectory(prefix="muse-lock-inplace-", dir=SCRATCH) as tmp:
            root = Path(tmp) / "muse"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
            display = str(outside).encode("utf-16-le")
            paths = substitute + b"\x00\x00" + display + b"\x00\x00"
            payload = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0,
                                  0, len(substitute), len(substitute) + 2, len(display)) + paths
            original = content_io._windows_child
            attacked = []

            def open_child(parent, name, **options):
                handle = original(parent, name, **options)
                if options.get("directory") and name == "muse":
                    attacker = win32file.CreateFile(str(root), 0x100, 7, None, 3, 0x02200000, None)
                    try:
                        win32file.DeviceIoControl(attacker, 0x900A4, payload, None)
                        attacked.append(True)
                    finally:
                        attacker.Close()
                return handle

            try:
                with patch.object(content_io, "_windows_child", side_effect=open_child):
                    try:
                        with file_lock(root / "install.lock"):
                            pass  # A lock against the original retained object is safe too.
                    except content_io.DomainError:
                        pass
                self.assertEqual(attacked, [True])
                self.assertEqual(list(outside.iterdir()), [], "no lock may be created outside the profile")
            finally:
                root.rmdir()

    def test_read_utf8_file_in_non_ascii_profile(self):
        from hermes_muse.content_io import Files
        with tempfile.TemporaryDirectory(prefix="muse-空间 -", dir=SCRATCH) as tmp:
            root = Path(tmp) / "muse"
            root.mkdir()
            raw = "Notes: café — 中文\r\n".encode("utf-8")
            (root / "TOOLS.md").write_bytes(raw)
            self.assertEqual(Files(tmp).read("TOOLS.md"), raw)


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsIntegration(unittest.TestCase):
    def test_store_initializes_and_reopens_transactionally(self):
        from hermes_muse.store import Store
        from hermes_muse.views import Views
        with tempfile.TemporaryDirectory(prefix="muse-init-空间 -", dir=SCRATCH) as tmp:
            home = Path(tmp) / "new profile"
            store = Store(home)
            store.write("meta", "fixture", {"value": 1})
            self.assertEqual(Views(home).record("meta", "fixture"), {"value": 1})
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with store.transaction() as db:
                    store.put(db, "meta", "fixture", {"value": 2})
                    raise RuntimeError("rollback")
            self.assertEqual(Store(home).read("meta", "fixture"), {"value": 1})

    def test_native_launcher_is_atomic_owned_and_executable(self):
        import subprocess
        import sys
        from types import SimpleNamespace
        from hermes_muse.host import HermesHost
        with tempfile.TemporaryDirectory(prefix="muse-launch-空间 -", dir=SCRATCH) as tmp:
            home = Path(tmp) / "new profile"
            plugin = Path(tmp) / "missing plugin"
            store = SimpleNamespace(home=home, root=home / "muse")
            host = HermesHost(store, plugin_root=plugin)
            record = {"scripts": []}
            script = host._launcher("fixture", record)
            self.assertEqual(script, "hermes-muse-fixture.py")
            result = subprocess.run([sys.executable, str(home / "scripts" / script)],
                                    cwd=SCRATCH, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '{"wakeAgent": false}')
            host._launcher("fixture", record)
            self.assertEqual(record["scripts"], ["scripts/" + script])
            (home / "scripts" / script).write_text("unrelated", encoding="utf-8")
            with self.assertRaises(ValueError):
                host._launcher("fixture", {"scripts": []})

    def test_protected_readonly_sqlite_coexists_with_normal_writer(self):
        from contextlib import closing
        import sqlite3
        from hermes_muse.views import Views
        from hermes_muse.operations import reader
        with tempfile.TemporaryDirectory(prefix="muse-db-空间 -", dir=SCRATCH) as tmp:
            path = Path(tmp) / "muse" / "state.db"
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as seed, seed:
                seed.execute("CREATE TABLE records(kind,id,data)")
                seed.execute("INSERT INTO records VALUES('meta','one','1')")
            for context in (Views(tmp).database(), reader(path)):
                with context as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute("DELETE FROM records")
                    with closing(sqlite3.connect(path, timeout=0.1)) as writer, writer:
                        writer.execute("BEGIN IMMEDIATE")
                        writer.rollback()
                    with self.assertRaises(OSError):
                        path.rename(path.with_suffix(".moved"))
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute("INSERT INTO records VALUES('meta','two','2')")
            self.assertEqual(len(Views(tmp).rows("meta")), 2)

    def test_sqlite_pins_block_in_place_junction_and_leaf_replacement(self):
        import struct
        import win32file
        import pywintypes
        from hermes_muse.store import Store
        from hermes_muse.views import Views
        with tempfile.TemporaryDirectory(prefix="muse-db-pin-", dir=SCRATCH) as tmp:
            home = Path(tmp) / "profile"
            store = Store(home)
            store.write("meta", "sentinel", {"inside": True})
            outside = Path(tmp) / "outside"
            outside.mkdir()
            sentinel = outside / "state.db"
            sentinel.write_bytes(b"outside sentinel")
            substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
            display = str(outside).encode("utf-16-le")
            paths = substitute + b"\x00\x00" + display + b"\x00\x00"
            payload = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0,
                                  0, len(substitute), len(substitute) + 2, len(display)) + paths
            with Views(home).database() as db:
                for directory in (home, home / "muse"):
                    attacker = win32file.CreateFile(str(directory), 0x100, 7, None, 3, 0x02200000, None)
                    try:
                        with self.assertRaises(pywintypes.error) as caught:
                            win32file.DeviceIoControl(attacker, 0x900A4, payload, None)
                        self.assertEqual(caught.exception.winerror, 145)
                    finally:
                        attacker.Close()
                    with self.assertRaises(OSError):
                        directory.rename(directory.with_name("moved"))
                self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
                self.assertEqual(sentinel.read_bytes(), b"outside sentinel")
            # Protection must end with the connection, not leak handles.
            (home / "muse" / "state.db").rename(home / "muse" / "moved.db")

    def test_missing_fade_database_is_unreadable_without_initialization(self):
        from hermes_muse.fade import Fade
        with tempfile.TemporaryDirectory(prefix="muse-fade-missing-", dir=SCRATCH) as tmp:
            home = Path(tmp) / "missing"
            fade = Fade(home, [])
            self.assertEqual(fade.signals(), [])
            self.assertTrue(fade.unsure)
            self.assertFalse(home.exists())

    def test_database_reads_fail_closed_before_sqlite_or_creation(self):
        from unittest.mock import patch
        from hermes_muse.content_io import DomainError
        from hermes_muse.views import Views
        from hermes_muse.operations import reader
        with tempfile.TemporaryDirectory(prefix="muse-readonly-", dir=SCRATCH) as tmp:
            home = Path(tmp) / "missing"
            with patch("sqlite3.connect", side_effect=AssertionError("unsafe database reopen")):
                for context in (Views(home).database(), reader(home / "state.db")):
                    with self.assertRaises(FileNotFoundError):
                        with context:
                            self.fail("unprotected SQLite connection")
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
