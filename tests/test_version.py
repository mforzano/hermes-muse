"""The package, shipped surfaces and generated install receipt agree."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from hermes_muse import __version__
from hermes_muse.store import Store, VERSION
from hermes_muse.host_v1 import HermesHost


class VersionTests(unittest.TestCase):
    def test_release_and_install_versions_agree(self):
        root = Path(__file__).resolve().parents[1]
        manifest_version = re.search(r'^version: [\'"]?([^\'"\s]+)',
                                     (root / "plugin.yaml").read_text(), re.M)[1]
        self.assertEqual(VERSION, manifest_version)
        self.assertEqual(__version__, manifest_version)
        self.assertEqual(json.loads((root / "dashboard/manifest.json").read_text())["version"], VERSION)
        with tempfile.TemporaryDirectory(prefix="muse-version-") as home:
            store = Store(Path(home))
            host = HermesHost(store)
            manifest = host.manifest()
            manifest["version"] = "older"
            host.save_manifest(manifest)
            self.assertEqual(host.manifest()["version"], VERSION)
