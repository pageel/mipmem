"""Tests for sharded multi-host vault init, atomic redacted I/O, and mutation journal."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from mipmem.core.diagnostics import (
    CONFIG_INVALID_HOST_ID,
    CP_VLT_01,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    register_default_scrubber,
)
from mipmem.core import vault as vault_mod
from mipmem.core.vault import (
    VAULT_MANIFEST_SCHEMA,
    append_mutation_event,
    atomic_write_vault_file,
    default_host_id,
    init_vault,
    is_vault_initialized,
    validate_host_id,
)


def _supports_posix_permissions(directory: Path) -> bool:
    """Return True if the filesystem hosting directory enforces 0700/0600 chmod modes."""
    if os.name != "posix":
        return False
    probe = directory / ".perm_probe"
    probe.write_text("x", encoding="utf-8")
    try:
        os.chmod(probe, 0o600)
        return stat.S_IMODE(probe.stat().st_mode) == 0o600
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


class TestVaultInitializer(unittest.TestCase):
    """Verify host validation, anonymized default host_id, sharded layout, and permissions."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_validate_host_id_accepts_valid_and_rejects_invalid(self) -> None:
        for valid in ("host-alpha", "host_01", "abc", "a" + ("0" * 62) + "z"):
            self.assertEqual(validate_host_id(valid), valid)

        invalid_cases = (
            "",
            "ab",
            "Host-Alpha",
            "host alpha",
            "../evil",
            "host/sub",
            "-leading",
            "trailing-",
            "a" * 65,
            "host.name",
        )
        for bad in invalid_cases:
            with self.subTest(host_id=bad):
                with self.assertRaises(MipmemError) as ctx:
                    validate_host_id(bad)
                self.assertEqual(ctx.exception.code, CONFIG_INVALID_HOST_ID)

    def test_default_host_id_is_deterministic_and_anonymized(self) -> None:
        with mock.patch("platform.node", return_value="secret-laptop-hostname"):
            first = default_host_id()
            second = default_host_id()
        self.assertEqual(first, second)
        self.assertNotIn("secret-laptop-hostname", first)
        self.assertNotIn("secret", first)
        self.assertEqual(validate_host_id(first), first)
        self.assertRegex(first, r"^(?:linux|darwin|windows|posix)-[0-9a-f]{4}$")

        alpha = default_host_id(seed="machine-one")
        beta = default_host_id(seed="machine-two")
        self.assertNotEqual(alpha, beta)

    def test_init_vault_sharded_layout_write_if_missing_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            self.assertFalse(is_vault_initialized(vault_root))

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                layout_alpha = init_vault(vault_root, host_id="host-alpha")

                self.assertTrue(is_vault_initialized(vault_root))
                self.assertEqual(layout_alpha.host_id, "host-alpha")
                self.assertTrue(layout_alpha.cache_dir.is_dir())
                self.assertTrue(layout_alpha.sessions_dir.is_dir())
                self.assertTrue(layout_alpha.events_dir.is_dir())
                self.assertTrue(layout_alpha.telemetry_dir.is_dir())
                self.assertTrue(layout_alpha.brainstorms_host_dir.is_dir())
                self.assertTrue(layout_alpha.host_meta_file.is_file())

                manifest = json.loads(layout_alpha.manifest_file.read_text(encoding="utf-8"))
                self.assertEqual(manifest, VAULT_MANIFEST_SCHEMA)
                gitignore_text = layout_alpha.gitignore_file.read_text(encoding="utf-8")
                self.assertIn(".cache/", gitignore_text)

                host_meta = json.loads(layout_alpha.host_meta_file.read_text(encoding="utf-8"))
                self.assertEqual(host_meta["host_id"], "host-alpha")
                self.assertEqual(host_meta["schema_version"], 1)
                self.assertTrue(host_meta["created_at"].endswith("Z"))

                if _supports_posix_permissions(base):
                    self.assertEqual(stat.S_IMODE(layout_alpha.host_dir.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE(layout_alpha.events_dir.stat().st_mode), 0o700)
                    self.assertEqual(
                        stat.S_IMODE(layout_alpha.host_meta_file.stat().st_mode),
                        0o600,
                    )

                # Mutate shared global files and re-run init for host-beta: must not overwrite.
                custom_pinned = "# Custom Pinned\nKeep across hosts.\n"
                layout_alpha.pinned_global_file.write_text(custom_pinned, encoding="utf-8")
                layout_alpha.gitignore_file.write_text(
                    gitignore_text + "# custom\n",
                    encoding="utf-8",
                )

                layout_beta = init_vault(vault_root, host_id="host-beta")
                self.assertEqual(layout_beta.host_id, "host-beta")
                self.assertEqual(
                    layout_beta.pinned_global_file.read_text(encoding="utf-8"),
                    custom_pinned,
                )
                self.assertIn("# custom", layout_beta.gitignore_file.read_text(encoding="utf-8"))
                self.assertTrue(
                    (vault_root / "shared" / "brainstorms" / "by-host" / "host-beta").is_dir()
                )
                self.assertTrue(
                    (vault_root / "hosts" / "host-beta" / "host_meta.json").is_file()
                )

    def test_init_vault_survives_chmod_oserror_on_ntfs_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "ntfs_vault"
            cache_dir = base / "cache"
            with (
                mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False),
                mock.patch("os.chmod", side_effect=OSError("chmod unsupported on NTFS")),
            ):
                layout = init_vault(vault_root, host_id="host-ntfs")
            self.assertTrue(layout.manifest_file.is_file())
            self.assertTrue(layout.host_meta_file.is_file())


class TestAtomicVaultWriter(unittest.TestCase):
    """Verify path traversal, symlink scanning, single-writer guard, and redacted atomic I/O."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_rejects_path_traversal_and_symlink_chains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            outside_dir = base / "outside"
            outside_dir.mkdir(parents=True)

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                init_vault(vault_root, host_id="host-alpha")

                bad_paths = (
                    "",
                    "../outside.md",
                    "hosts/host-alpha/../../outside.md",
                    "/etc/passwd",
                    "~/.ssh/config",
                    "hosts\\host-alpha\\..\\..\\evil.md",
                )
                for bad in bad_paths:
                    with self.subTest(rel_path=bad):
                        with self.assertRaises(MipmemError) as ctx:
                            atomic_write_vault_file(vault_root, bad, "secret")
                        self.assertEqual(ctx.exception.code, VAULT_PATH_TRAVERSAL)

                escape_dir = vault_root / "hosts" / "host-alpha" / "sessions" / "escape_dir"
                escape_file = vault_root / "hosts" / "host-alpha" / "sessions" / "link_file.md"
                outside_target = outside_dir / "external.md"
                outside_target.write_text("original", encoding="utf-8")
                try:
                    escape_dir.symlink_to(outside_dir, target_is_directory=True)
                    escape_file.symlink_to(outside_target)
                    has_symlink = True
                except OSError:
                    has_symlink = False

                if has_symlink:
                    with self.assertRaises(MipmemError) as dir_ctx:
                        atomic_write_vault_file(
                            vault_root,
                            "hosts/host-alpha/sessions/escape_dir/pwn.md",
                            "compromised",
                        )
                    self.assertEqual(dir_ctx.exception.code, VAULT_PATH_TRAVERSAL)
                    self.assertFalse((outside_dir / "pwn.md").exists())

                    with self.assertRaises(MipmemError) as file_ctx:
                        atomic_write_vault_file(
                            vault_root,
                            "hosts/host-alpha/sessions/link_file.md",
                            "compromised",
                        )
                    self.assertEqual(file_ctx.exception.code, VAULT_PATH_TRAVERSAL)
                    self.assertEqual(outside_target.read_text(encoding="utf-8"), "original")

    def test_single_writer_ownership_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                init_vault(vault_root, host_id="host-alpha")
                init_vault(vault_root, host_id="host-beta")

                forbidden = (
                    "hosts/host-alpha/sessions/2026-11/s1.md",
                    "shared/brainstorms/by-host/host-alpha/bs1.md",
                    "shared/brainstorms/index.json",
                )
                for rel in forbidden:
                    with self.subTest(rel_path=rel):
                        with self.assertRaises(MipmemError) as ctx:
                            atomic_write_vault_file(
                                vault_root,
                                rel,
                                "cross-host overwrite",
                                actor_host="host-beta",
                            )
                        self.assertEqual(ctx.exception.code, VAULT_PATH_TRAVERSAL)

                session_out = atomic_write_vault_file(
                    vault_root,
                    "hosts/host-beta/sessions/2026-11/s1.md",
                    "# Session on Beta\n",
                    actor_host="host-beta",
                )
                bs_out = atomic_write_vault_file(
                    vault_root,
                    "shared/brainstorms/by-host/host-beta/bs1.md",
                    "# Brainstorm on Beta\n",
                    actor_host="host-beta",
                )
                self.assertTrue(session_out.is_file())
                self.assertTrue(bs_out.is_file())

    def test_atomic_write_redacts_secrets_home_and_enforces_0600_with_ntfs_fallback(self) -> None:
        openai_proj = "sk" + "-proj-" + ("B" * 24)
        gh_pat = "ghp" + "_" + ("A" * 28)

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            mock_home = base / "home_user"
            mock_home.mkdir()

            payload = (
                f"key={openai_proj}\n"
                f"pat={gh_pat}\n"
                f"path={mock_home}/workspace/notes.md\n"
            )
            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                init_vault(vault_root, host_id="host-alpha", home_dir=mock_home)
                written = atomic_write_vault_file(
                    vault_root,
                    "hosts/host-alpha/sessions/2026-11/s1.md",
                    payload,
                    actor_host="host-alpha",
                    home_dir=mock_home,
                )

                disk_text = written.read_text(encoding="utf-8")
                self.assertNotIn(openai_proj, disk_text)
                self.assertNotIn(gh_pat, disk_text)
                self.assertNotIn(str(mock_home), disk_text)
                self.assertIn("[REDACTED:OPENAI_KEY]", disk_text)
                self.assertIn("[REDACTED:GITHUB_PAT]", disk_text)
                self.assertIn("~/workspace/notes.md", disk_text)

                leftovers = [
                    p for p in written.parent.iterdir() if p.name.startswith(".tmp_mipmem_")
                ]
                self.assertEqual(leftovers, [])
                if _supports_posix_permissions(base):
                    self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)

                with mock.patch("os.chmod", side_effect=OSError("chmod blocked on NTFS")):
                    ntfs_written = atomic_write_vault_file(
                        vault_root,
                        "hosts/host-alpha/sessions/2026-11/s2.md",
                        f"second={gh_pat}\n",
                        actor_host="host-alpha",
                        home_dir=mock_home,
                    )
                self.assertIn("[REDACTED:GITHUB_PAT]", ntfs_written.read_text(encoding="utf-8"))


class TestMutationJournal(unittest.TestCase):
    """Verify CRDT-lite append-only journal isolation, 6-field schema, redaction, and CP-VLT-01."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_concurrent_multi_host_appends_never_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                init_vault(vault_root, host_id="host-alpha")
                init_vault(vault_root, host_id="host-beta")
                atomic_write_vault_file(
                    vault_root,
                    "shared/brainstorms/by-host/host-alpha/bs-arch-0001.md",
                    "# Architecture\nstatus: open\n",
                    actor_host="host-alpha",
                )

                def _emit(pair: tuple[str, int]) -> dict[str, object]:
                    host_name, seq = pair
                    return append_mutation_event(
                        vault_root,
                        host_name,
                        "brainstorm",
                        "bs-arch-0001",
                        {"status": "decided", "seq": seq},
                    )

                work = [("host-alpha", idx) for idx in range(8)] + [
                    ("host-beta", idx) for idx in range(8)
                ]
                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(_emit, work))

                self.assertEqual(len(results), 16)
                alpha_log = vault_root / "hosts" / "host-alpha" / "events" / "mutations.jsonl"
                beta_log = vault_root / "hosts" / "host-beta" / "events" / "mutations.jsonl"
                alpha_lines = alpha_log.read_text(encoding="utf-8").splitlines()
                beta_lines = beta_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(alpha_lines), 8)
                self.assertEqual(len(beta_lines), 8)

                for line in alpha_lines:
                    self.assertEqual(json.loads(line)["actor_host"], "host-alpha")
                for line in beta_lines:
                    self.assertEqual(json.loads(line)["actor_host"], "host-beta")

                alpha_snapshot = {
                    str(p.relative_to(vault_root)): p.read_bytes()
                    for p in (vault_root / "hosts" / "host-alpha").rglob("*")
                    if p.is_file()
                }
                append_mutation_event(
                    vault_root,
                    "host-beta",
                    "brainstorm",
                    "bs-arch-0001",
                    {"status": "superseded", "reason": "beta override"},
                )
                alpha_after = {
                    str(p.relative_to(vault_root)): p.read_bytes()
                    for p in (vault_root / "hosts" / "host-alpha").rglob("*")
                    if p.is_file()
                }
                self.assertEqual(alpha_snapshot, alpha_after)

    def test_event_schema_secret_home_redaction_and_cp_vlt_01_logging(self) -> None:
        gh_pat = "ghp" + "_" + ("K" * 28)
        openai_proj = "sk" + "-proj-" + ("M" * 24)

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            mock_home = base / "home_alice"
            mock_home.mkdir()

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                init_vault(vault_root, host_id="host-beta", home_dir=mock_home)
                event = append_mutation_event(
                    vault_root,
                    "host-beta",
                    "brainstorm",
                    "bs-arch-0001",
                    {
                        "new_status": "decided",
                        "notes": f"token={gh_pat} path={mock_home}/repo",
                        "nested": {"api": openai_proj},
                    },
                    home_dir=mock_home,
                )

                self.assertEqual(
                    set(event.keys()),
                    {"event_id", "ts_utc", "actor_host", "target_type", "target_id", "payload"},
                )
                self.assertTrue(str(event["event_id"]).startswith("evt-"))
                self.assertRegex(
                    str(event["ts_utc"]),
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
                )

                journal = vault_root / "hosts" / "host-beta" / "events" / "mutations.jsonl"
                raw_lines = journal.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(raw_lines), 1)
                self.assertNotIn(gh_pat, raw_lines[0])
                self.assertNotIn(openai_proj, raw_lines[0])
                self.assertNotIn(str(mock_home), raw_lines[0])
                self.assertIn("[REDACTED:GITHUB_PAT]", raw_lines[0])
                self.assertIn("[REDACTED:OPENAI_KEY]", raw_lines[0])
                self.assertIn("~/repo", raw_lines[0])
                self.assertEqual(json.loads(raw_lines[0]), event)

                if _supports_posix_permissions(base):
                    self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)

                diag_log = cache_dir / "mipmem.log"
                self.assertTrue(diag_log.is_file())
                diag_text = diag_log.read_text(encoding="utf-8")
                self.assertIn(CP_VLT_01, diag_text)
                self.assertNotIn(gh_pat, diag_text)
                self.assertNotIn(openai_proj, diag_text)

                with (
                    mock.patch.object(vault_mod, "_fcntl", None),
                    mock.patch("os.chmod", side_effect=OSError("ntfs fallback")),
                ):
                    second = append_mutation_event(
                        vault_root,
                        "host-beta",
                        "session",
                        "sess-01",
                        {"summary": "fallback flock and chmod check"},
                        home_dir=mock_home,
                    )
                self.assertEqual(second["target_id"], "sess-01")
                self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 2)

    def test_rejects_invalid_actor_host_and_empty_target_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_root = Path(tmp_dir) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            with self.assertRaises(MipmemError) as ctx:
                append_mutation_event(vault_root, "../bad", "brainstorm", "bs-1", {})
            self.assertEqual(ctx.exception.code, CONFIG_INVALID_HOST_ID)

            with self.assertRaises(ValueError):
                append_mutation_event(vault_root, "host-alpha", "", "bs-1", {})
            with self.assertRaises(ValueError):
                append_mutation_event(vault_root, "host-alpha", "brainstorm", "   ", {})


if __name__ == "__main__":
    unittest.main()
