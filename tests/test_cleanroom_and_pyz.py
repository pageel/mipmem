"""Tests for the clean-room static scanner and zipapp archive audits."""

from __future__ import annotations

import getpass
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.check_cleanroom import scan_cleanroom

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestCleanroomScanner(unittest.TestCase):
    """Verify clean-room detection across source files and zipapp bundles."""

    def test_detects_real_user_and_home_paths(self) -> None:
        home_str = str(Path.home())
        user_name = getpass.getuser()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "src").mkdir()
            (root / "src" / "leak.py").write_text(
                f'PATH_A = "{home_str}/secret.md"\n'
                f'PATH_B = "/media/{user_name}/vault"\n',
                encoding="utf-8",
            )
            violations = scan_cleanroom(root)
            self.assertGreaterEqual(len(violations), 2)

    def test_detects_secret_tokens(self) -> None:
        gh_pat = "ghp" + "_" + ("A" * 36)
        api_key = "sk" + "-" + ("B" * 40)
        aws_key = "AKIA" + ("C" * 16)
        pem_header = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "notes.md").write_text(
                "\n".join([gh_pat, api_key, aws_key, pem_header]) + "\n",
                encoding="utf-8",
            )
            violations = scan_cleanroom(root)
            self.assertGreaterEqual(len(violations), 4)

    def test_detects_forbidden_binary_db_and_key_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "leak.db").write_bytes(b"SQLite format 3\x00")
            (root / "id_test.pem").write_text("placeholder", encoding="utf-8")
            violations = scan_cleanroom(root)
            self.assertGreaterEqual(len(violations), 2)

    def test_tbd_allowed_only_in_config_example_toml(self) -> None:
        tbd_marker = "[" + "TBD]"
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "config.example.toml").write_text(
                f'vault_remote_url = "{tbd_marker}"\n',
                encoding="utf-8",
            )
            self.assertEqual(scan_cleanroom(root), [])

            (root / "src").mkdir()
            (root / "src" / "bad.py").write_text(
                f'PLACEHOLDER = "{tbd_marker}"\n',
                encoding="utf-8",
            )
            violations = scan_cleanroom(root)
            self.assertEqual(len(violations), 1)

    def test_scans_inside_pyz_and_zip_archives(self) -> None:
        gh_pat = "ghp" + "_" + ("Z" * 36)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "bin").mkdir()

            clean_pyz = root / "bin" / "clean.pyz"
            with zipfile.ZipFile(clean_pyz, "w") as zf:
                zf.writestr("mipmem/__init__.py", b'__version__ = "0.1.0"\n')
            self.assertEqual(scan_cleanroom(root), [])

            dirty_pyz = root / "bin" / "dirty.pyz"
            with zipfile.ZipFile(dirty_pyz, "w") as zf:
                zf.writestr("mipmem/__init__.py", f'TOKEN = "{gh_pat}"\n'.encode("utf-8"))
                zf.writestr("mipmem/__pycache__/mod.cpython-312.pyc", b"\x00\x01\x02")
            violations = scan_cleanroom(root)
            self.assertGreaterEqual(len(violations), 2)

    def test_actual_mipmem_repo_is_clean(self) -> None:
        violations = scan_cleanroom(REPO_ROOT)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
