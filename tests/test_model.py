import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from model import prepare


class Integrity(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "weights"
        self.root.mkdir()
        self.file = self.root / "config.json"
        self.file.write_bytes(b"pinned")
        self.lock = Path(self.directory.name) / "lock.json"
        self.lock.write_text(json.dumps({"files": {"config.json": {
            "sha256": hashlib.sha256(b"pinned").hexdigest(), "size": 6}}}))

    def test_correct_files_accepted(self):
        prepare(self.root, lock_path=self.lock)

    def test_modified_weights_fail(self):
        self.file.write_bytes(b"edited")
        with self.assertRaisesRegex(ValueError, "integrity"):
            prepare(self.root, lock_path=self.lock)

    def test_extra_config_or_code_fails(self):
        (self.root / "modeling_custom.py").write_text("extra code")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            prepare(self.root, lock_path=self.lock)

    def test_symlinks_fail(self):
        target = self.root.parent / "external"
        self.file.rename(target)
        self.file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            prepare(self.root, lock_path=self.lock)


if __name__ == "__main__":
    unittest.main()
