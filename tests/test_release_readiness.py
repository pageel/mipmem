"""Tests for clean-room git history scanning and v1.0.0 release readiness."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from scripts.check_cleanroom import main as cleanroom_main
from scripts.check_cleanroom import scan_cleanroom, scan_git_history
from scripts.verify_release import main as verify_release_main
from scripts.verify_release import validate_release_readiness


def _git(cwd: Path, *args: str) -> None:
    """Run git inside a temporary test repo without altering system config."""
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class TestCleanroomGitHistoryAndDistArchives(unittest.TestCase):
    """Verify full git-history leak detection and .whl/.tar.gz archive audits."""

    def test_detects_secret_and_personal_path_in_deleted_git_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "sample_repo"
            repo.mkdir(parents=True)
            _git(repo, "init")
            _git(repo, "config", "user.name", "MipMem Bot")
            _git(repo, "config", "user.email", "bot@example.com")

            tbd_marker = "[" + "TBD]"
            (repo / "config.example.toml").write_text(
                f'vault_remote_url = "{tbd_marker}"\n',
                encoding="utf-8",
            )
            _git(repo, "add", "config.example.toml")
            _git(repo, "commit", "-m", "chore: clean baseline config")

            # Clean history passes baseline check.
            self.assertEqual(scan_git_history(repo), [])

            # Introduce historical secret, personal path, and forbidden .db file, then delete them.
            fake_pat = "ghp" + "_" + ("X" * 36)
            fake_path = "/home/" + "customdevuser/private_vault"
            (repo / "leak.txt").write_text(
                f"TOKEN={fake_pat}\nVAULT={fake_path}\n",
                encoding="utf-8",
            )
            (repo / "leaked.db").write_bytes(b"SQLite format 3\x00")
            _git(repo, "add", "leak.txt", "leaked.db")
            _git(repo, "commit", "-m", "oops: accidental commit")

            _git(repo, "rm", "leak.txt", "leaked.db")
            _git(repo, "commit", "-m", "fix: remove leaked files")

            # HEAD looks clean, but --git-history catches every historical leak.
            self.assertEqual(scan_cleanroom(repo), [])
            hist_hits = scan_git_history(repo)
            self.assertTrue(
                any("secret-pattern" in item and "leak.txt" in item for item in hist_hits),
                msg=str(hist_hits),
            )
            self.assertTrue(
                any("unix-user-path" in item and "leak.txt" in item for item in hist_hits),
                msg=str(hist_hits),
            )
            self.assertTrue(
                any("leaked.db" in item and "forbidden" in item for item in hist_hits),
                msg=str(hist_hits),
            )

            combined_hits = scan_cleanroom(repo, include_git_history=True)
            self.assertGreaterEqual(len(combined_hits), len(hist_hits))

    def test_scans_wheel_and_tarball_archives_and_cli_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "clean_repo"
            repo.mkdir(parents=True)
            _git(repo, "init")
            _git(repo, "config", "user.name", "MipMem Bot")
            _git(repo, "config", "user.email", "bot@example.com")
            (repo / "README.md").write_text("# Clean Repo\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "docs: initial readme")

            clean_dist = root / "clean_dist"
            clean_dist.mkdir()
            clean_whl = clean_dist / "mipmem-1.0.0-py3-none-any.whl"
            with zipfile.ZipFile(clean_whl, "w") as zf:
                zf.writestr("mipmem/__init__.py", b'__version__ = "1.0.0"\n')

            clean_tgz = clean_dist / "mipmem-1.0.0.tar.gz"
            with tarfile.open(clean_tgz, "w:gz") as tf:
                src_file = root / "src_init.py"
                src_file.write_bytes(b'__version__ = "1.0.0"\n')
                tf.add(src_file, arcname="mipmem-1.0.0/src/mipmem/__init__.py")

            self.assertEqual(
                scan_cleanroom(repo, include_git_history=True, dist_dir=clean_dist),
                [],
            )
            out_ok = io.StringIO()
            err_ok = io.StringIO()
            with contextlib.redirect_stdout(out_ok), contextlib.redirect_stderr(err_ok):
                rc_ok = cleanroom_main(
                    [str(repo), "--git-history", "--dist-dir", str(clean_dist)]
                )
            self.assertEqual(rc_ok, 0, msg=err_ok.getvalue())

            # Dirty dist archives containing .db, __pycache__, and synthetic secret
            dirty_dist = root / "dirty_dist"
            dirty_dist.mkdir()
            dirty_whl = dirty_dist / "mipmem-1.0.0-py3-none-any.whl"
            with zipfile.ZipFile(dirty_whl, "w") as zf:
                zf.writestr("mipmem/__pycache__/cli.cpython-312.pyc", b"\x00\x01")
                zf.writestr("mipmem/data.db", b"SQLite format 3\x00")

            dirty_tgz = dirty_dist / "mipmem-1.0.0.tar.gz"
            secret_tok = "sk" + "-proj-" + ("Z" * 30)
            with tarfile.open(dirty_tgz, "w:gz") as tf:
                leak_src = root / "leak_mod.py"
                leak_src.write_text(f'API = "{secret_tok}"\n', encoding="utf-8")
                tf.add(leak_src, arcname="mipmem-1.0.0/src/leak_mod.py")
                db_src = root / "tmp_state.db"
                db_src.write_bytes(b"SQLite format 3\x00")
                tf.add(db_src, arcname="mipmem-1.0.0/tmp_state.db")

            hits = scan_cleanroom(repo, dist_dir=dirty_dist)
            self.assertTrue(any("archive-bytecode" in h for h in hits), msg=str(hits))
            self.assertTrue(any("archive-forbidden-file" in h for h in hits), msg=str(hits))
            self.assertTrue(any("secret-pattern" in h for h in hits), msg=str(hits))

            out_bad = io.StringIO()
            err_bad = io.StringIO()
            with contextlib.redirect_stdout(out_bad), contextlib.redirect_stderr(err_bad):
                rc_bad = cleanroom_main(
                    [str(repo), "--git-history", "--dist-dir", str(dirty_dist)]
                )
            self.assertEqual(rc_bad, 1)
            self.assertIn("CLEANROOM_VIOLATION", err_bad.getvalue())


def _create_valid_release_repo(root: Path) -> Path:
    """Create a synthetic repository passing all Dual-Channel v1.0.0 release checks."""
    repo = root / "release_repo"
    (repo / "src" / "mipmem").mkdir(parents=True)
    (repo / ".github" / "workflows").mkdir(parents=True)

    (repo / "pyproject.toml").write_text(
        '[build-system]\n'
        'requires = ["setuptools>=61.0"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "mipmem"\n'
        'version = "1.0.0"\n'
        'description = "Zero-dependency 4-layer mipmapped cognitive memory engine"\n'
        'readme = "README.md"\n'
        'requires-python = ">=3.9"\n'
        'license = { text = "MIT" }\n'
        'authors = [{ name = "Pageel Contributors" }]\n'
        "dependencies = []\n\n"
        "[project.urls]\n"
        'Homepage = "https://github.com/pageel/mipmem"\n'
        'Repository = "https://github.com/pageel/mipmem"\n'
        'Issues = "https://github.com/pageel/mipmem/issues"\n',
        encoding="utf-8",
    )
    (repo / "src" / "mipmem" / "__init__.py").write_text(
        '__version__ = "1.0.0"\n',
        encoding="utf-8",
    )
    (repo / "plugin.json").write_text(
        json.dumps({"name": "mipmem", "version": "1.0.0"}) + "\n",
        encoding="utf-8",
    )
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\n"
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: python3 -m py_compile src/mipmem/*.py src/mipmem/sync/*.py\n"
        "  macos-smoke:\n"
        "    if: github.event.repository.private == false\n"
        "    runs-on: macos-latest\n"
        "    steps:\n"
        "      - run: python3 -m unittest discover -s tests -v\n",
        encoding="utf-8",
    )
    (repo / ".github" / "workflows" / "release.yml").write_text(
        "name: Release\n"
        "on:\n"
        "  push:\n"
        '    tags: ["v*"]\n'
        "jobs:\n"
        "  release:\n"
        "    environment:\n"
        "      name: pypi\n"
        "    permissions:\n"
        "      contents: write\n"
        "      id-token: write\n"
        "      attestations: write\n"
        "    steps:\n"
        "      - run: sha256sum mipmem.pyz > SHA256SUMS\n"
        "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
        "        with:\n"
        "          attestations: true\n",
        encoding="utf-8",
    )
    dist_dir = repo / "dist"
    dist_dir.mkdir()
    pyz_file = dist_dir / "mipmem.pyz"
    with zipfile.ZipFile(pyz_file, "w") as zf:
        zf.writestr("__main__.py", b"raise SystemExit(0)\n")
    digest = hashlib.sha256(pyz_file.read_bytes()).hexdigest()
    (dist_dir / "SHA256SUMS").write_text(f"{digest}  mipmem.pyz\n", encoding="utf-8")
    return repo


class TestVerifyReleaseReadiness(unittest.TestCase):
    """Verify Dual-Channel release readiness checks (versions, OIDC, PII, checksums)."""

    def test_passes_on_valid_oidc_dual_channel_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = _create_valid_release_repo(Path(tmp_dir))
            dist_dir = repo / "dist"
            self.assertEqual(validate_release_readiness(repo, dist_dir=dist_dir), [])

            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = verify_release_main([str(repo), "--dist-dir", str(dist_dir)])
            self.assertEqual(rc, 0, msg=err_buf.getvalue())
            self.assertIn("RELEASE_READINESS_OK", out_buf.getvalue())

    def test_detects_version_mismatch_across_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = _create_valid_release_repo(Path(tmp_dir))
            plugin_file = repo / "plugin.json"
            plugin_orig = plugin_file.read_text(encoding="utf-8")
            plugin_file.write_text(
                json.dumps({"name": "mipmem", "version": "0.9.9"}) + "\n",
                encoding="utf-8",
            )
            issues = validate_release_readiness(repo)
            self.assertTrue(any("version-mismatch" in i for i in issues), msg=str(issues))

            plugin_file.write_text(plugin_orig, encoding="utf-8")
            init_file = repo / "src" / "mipmem" / "__init__.py"
            init_file.write_text('__version__ = "0.9.8"\n', encoding="utf-8")
            issues_init = validate_release_readiness(repo)
            self.assertTrue(
                any("version-mismatch" in i for i in issues_init),
                msg=str(issues_init),
            )

    def test_detects_static_pypi_token_and_missing_oidc_or_ci_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = _create_valid_release_repo(Path(tmp_dir))
            rel_yml = repo / ".github" / "workflows" / "release.yml"
            orig_rel = rel_yml.read_text(encoding="utf-8")

            # Static token prohibited
            rel_yml.write_text(
                orig_rel + "          password: ${{ secrets.PYPI_API_TOKEN }}\n",
                encoding="utf-8",
            )
            hits_tok = validate_release_readiness(repo)
            self.assertTrue(
                any("static-pypi-token" in i for i in hits_tok),
                msg=str(hits_tok),
            )

            # Missing id-token / attestations / SHA256SUMS
            rel_yml.write_text("name: Release\nsteps:\n  - run: echo ok\n", encoding="utf-8")
            hits_oidc = validate_release_readiness(repo)
            self.assertTrue(any("oidc" in i for i in hits_oidc), msg=str(hits_oidc))
            self.assertTrue(
                any("attestation" in i for i in hits_oidc),
                msg=str(hits_oidc),
            )
            self.assertTrue(any("SHA256SUMS" in i for i in hits_oidc), msg=str(hits_oidc))
            rel_yml.write_text(orig_rel, encoding="utf-8")

            # Missing sync/*.py or macos-latest in ci.yml
            ci_yml = repo / ".github" / "workflows" / "ci.yml"
            ci_yml.write_text(
                "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
                encoding="utf-8",
            )
            hits_ci = validate_release_readiness(repo)
            self.assertTrue(any("ci-missing-sync" in i for i in hits_ci), msg=str(hits_ci))
            self.assertTrue(any("ci-missing-macos" in i for i in hits_ci), msg=str(hits_ci))

    def test_detects_pii_email_dependencies_urls_and_bad_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = _create_valid_release_repo(Path(tmp_dir))
            pyproj = repo / "pyproject.toml"
            fake_email = "dev" + "@" + "example.com"
            pyproj.write_text(
                "[project]\n"
                'name = "mipmem"\n'
                'version = "1.0.0"\n'
                f'authors = [{{ name = "Dev", email = "{fake_email}" }}]\n'
                'dependencies = ["requests>=2.0"]\n',
                encoding="utf-8",
            )
            issues = validate_release_readiness(repo)
            self.assertTrue(any("author-email-pii" in i for i in issues), msg=str(issues))
            self.assertTrue(any("non-empty-dependencies" in i for i in issues), msg=str(issues))
            self.assertTrue(any("missing-project-urls" in i for i in issues), msg=str(issues))

            # Reset pyproject.toml and test SHA256SUMS mismatch in dist/
            repo2 = _create_valid_release_repo(Path(tmp_dir) / "second")
            sums_file = repo2 / "dist" / "SHA256SUMS"
            sums_file.write_text(("0" * 64) + "  mipmem.pyz\n", encoding="utf-8")
            dist_issues = validate_release_readiness(repo2, dist_dir=repo2 / "dist")
            self.assertTrue(
                any("checksum-mismatch" in i for i in dist_issues),
                msg=str(dist_issues),
            )


if __name__ == "__main__":
    unittest.main()
