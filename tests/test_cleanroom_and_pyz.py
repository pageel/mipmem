"""Tests for the clean-room static scanner and zipapp archive audits."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts.build_pyz import FIXED_ZIP_TIMESTAMP, build_zipapp
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


class TestPyzBundleAndLatency(unittest.TestCase):
    """Verify deterministic .pyz packaging, bytecode exclusion, and <25ms cold-start."""

    def test_build_zipapp_is_deterministic_and_excludes_bytecode(self) -> None:
        src_dir = REPO_ROOT / "src"
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            first_pyz = build_zipapp(src_dir=src_dir, output_path=base / "a" / "mipmem.pyz")
            second_pyz = build_zipapp(src_dir=src_dir, output_path=base / "b" / "mipmem.pyz")

            self.assertEqual(first_pyz.read_bytes(), second_pyz.read_bytes())
            with zipfile.ZipFile(first_pyz, "r") as zf:
                self.assertIsNone(zf.testzip())
                names = [info.filename for info in zf.infolist()]
                self.assertIn("__main__.py", names)
                self.assertIn("mipmem/cli.py", names)
                for info in zf.infolist():
                    self.assertNotIn("__pycache__", info.filename)
                    self.assertFalse(info.filename.endswith((".pyc", ".pyo")))
                    self.assertEqual(info.date_time, FIXED_ZIP_TIMESTAMP)

    def test_built_pyz_cold_start_under_25ms_in_isolated_sandbox(self) -> None:
        repo_pyz = REPO_ROOT / "bin" / "mipmem.pyz"
        self.assertTrue(repo_pyz.is_file(), f"Missing built zipapp at {repo_pyz}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            tmp_pyz = base / "mipmem.pyz"
            shutil.copy2(repo_pyz, tmp_pyz)
            isolated_home = base / "home"
            isolated_home.mkdir()
            isolated_vault = base / "vault"

            clean_env = {
                key: val
                for key, val in os.environ.items()
                if key
                not in (
                    "PYTHONPATH",
                    "MIPMEM_CONFIG_DIR",
                    "MIPMEM_VAULT_DIR",
                    "MIPMEM_CACHE_DIR",
                    "XDG_CONFIG_HOME",
                    "XDG_DATA_HOME",
                    "XDG_CACHE_HOME",
                )
            }
            clean_env["HOME"] = str(isolated_home)
            clean_env["MIPMEM_CONFIG_DIR"] = str(base / "cfg")
            clean_env["MIPMEM_VAULT_DIR"] = str(isolated_vault)
            clean_env["MIPMEM_CACHE_DIR"] = str(base / "cache")

            # Warm OS page cache once before measuring 10 consecutive cold invocations.
            warm = subprocess.run(
                [sys.executable, str(tmp_pyz), "health", "--json"],
                cwd=str(base),
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(warm.returncode, 0, msg=warm.stderr + warm.stdout)

            latencies_ms: list[float] = []
            for _ in range(10):
                proc = subprocess.run(
                    [sys.executable, str(tmp_pyz), "health", "--json"],
                    cwd=str(base),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
                self.assertEqual(proc.stderr, "")
                payload = json.loads(proc.stdout)
                self.assertTrue(payload["success"])
                self.assertFalse(payload["vault_initialized"])
                self.assertEqual(payload["vault_dir"], str(isolated_vault))
                latencies_ms.append(float(payload["cold_start_ms"]))

            avg_ms = sum(latencies_ms) / len(latencies_ms)
            self.assertLess(avg_ms, 25.0, msg=f"Average cold_start_ms={avg_ms:.2f}ms >= 25.0ms")


if __name__ == "__main__":
    unittest.main()
