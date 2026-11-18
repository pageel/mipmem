"""Tests for non-destructive, idempotent legacy v2 memory migration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mipmem.core.diagnostics import (
    CP_VLT_01,
    MIGRATE_SOURCE_MISSING,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    register_default_scrubber,
)
from mipmem.core.migrate_v2 import migrate_from_v2
from mipmem.core.vault import init_vault


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """Capture a relative-path -> bytes map of every file under root."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class TestMigrateV2(unittest.TestCase):
    """Verify source safety, dry-run zero writes, sidecar schema, idempotency, and conflicts."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_missing_source_and_symlink_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            vault_root = base / "vault"
            cache_dir = base / "cache"
            missing_src = base / "does-not-exist"

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                with self.assertRaises(MipmemError) as missing_ctx:
                    migrate_from_v2(missing_src, vault_root, host_id="host-alpha")
                self.assertEqual(missing_ctx.exception.code, MIGRATE_SOURCE_MISSING)

                v2_src = base / "legacy_v2"
                outside = base / "outside"
                outside.mkdir()
                secret_outside = outside / "leak.md"
                secret_outside.write_text("# Escaped\n", encoding="utf-8")
                (v2_src / "sessions").mkdir(parents=True)

                escape_link = v2_src / "sessions" / "escape.md"
                try:
                    escape_link.symlink_to(secret_outside)
                    has_symlink = True
                except OSError:
                    has_symlink = False

                if has_symlink:
                    with self.assertRaises(MipmemError) as sym_ctx:
                        migrate_from_v2(v2_src, vault_root, host_id="host-alpha")
                    self.assertEqual(sym_ctx.exception.code, VAULT_PATH_TRAVERSAL)

    def test_dry_run_writes_zero_vault_files_and_emits_cp_vlt_01(self) -> None:
        openai_proj = "sk" + "-proj-" + ("A" * 24)
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            v2_src = base / "legacy_v2"
            vault_root = base / "vault"
            cache_dir = base / "cache"
            (v2_src / "sessions" / "2026-10").mkdir(parents=True)
            (v2_src / "brainstorms").mkdir(parents=True)
            (v2_src / "sessions" / "2026-10" / "s1.md").write_text(
                f"# Session 1\nkey={openai_proj}\n",
                encoding="utf-8",
            )
            (v2_src / "brainstorms" / "bs-one.md").write_text(
                "# Topic One\n**Status:** open\n",
                encoding="utf-8",
            )
            (v2_src / "pinned_notes.md").write_text("# Host Pin\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                report = migrate_from_v2(v2_src, vault_root, host_id="host-alpha")

            self.assertTrue(report.dry_run)
            self.assertFalse(vault_root.exists())
            self.assertEqual(report.sessions_found, 1)
            self.assertEqual(report.brainstorms_found, 1)
            self.assertTrue(report.pinned_found)
            self.assertEqual(report.sessions_copied, 0)
            self.assertEqual(report.brainstorms_copied, 0)
            self.assertFalse(report.pinned_copied)
            self.assertGreaterEqual(len(report.planned_actions), 3)

            diag_log = cache_dir / "mipmem.log"
            self.assertTrue(diag_log.is_file())
            lines = [json.loads(line) for line in diag_log.read_text(encoding="utf-8").splitlines()]
            vlt_events = [entry for entry in lines if entry["event"] == CP_VLT_01]
            self.assertEqual(len(vlt_events), 1)
            self.assertTrue(vlt_events[0]["details"]["dry_run"])
            self.assertNotIn(openai_proj, diag_log.read_text(encoding="utf-8"))

    def test_apply_migrates_redacts_generates_sidecar_and_preserves_source(self) -> None:
        openai_proj = "sk" + "-proj-" + ("A" * 24)
        gh_pat = "ghp" + "_" + ("B" * 28)
        aws_key = "AKIA" + ("C" * 16)

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            v2_src = base / "legacy_v2"
            vault_root = base / "vault"
            cache_dir = base / "cache"
            mock_home = base / "home_alice"
            mock_home.mkdir()

            (v2_src / "sessions" / "2026-10").mkdir(parents=True)
            (v2_src / "sessions" / "nested").mkdir(parents=True)
            (v2_src / "brainstorms").mkdir(parents=True)

            sess_a = v2_src / "sessions" / "2026-10" / "sess-one.md"
            sess_a.write_text(
                f"# Session One\ntoken={openai_proj}\nhome={mock_home}/repo\n",
                encoding="utf-8",
            )
            sess_b = v2_src / "sessions" / "nested" / "2026-11-05-dated.md"
            sess_b.write_text("# Dated Nested\nPlain note.\n", encoding="utf-8")

            pinned = v2_src / "pinned_notes.md"
            pinned.write_text(f"# Local Pins\npat={gh_pat}\n", encoding="utf-8")

            bs_file = v2_src / "brainstorms" / "arch-design.md"
            bs_file.write_text(
                f"# Architecture Design\n> **Status:** decided\n\naws={aws_key}\n",
                encoding="utf-8",
            )

            source_before = _snapshot_tree(v2_src)
            custom_global = "# Existing Global Pinned\nDo not overwrite.\n"

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                layout = init_vault(vault_root, host_id="host-alpha", home_dir=mock_home)
                layout.pinned_global_file.write_text(custom_global, encoding="utf-8")

                report = migrate_from_v2(
                    v2_src,
                    vault_root,
                    host_id="host-alpha",
                    dry_run=False,
                    home_dir=mock_home,
                )

            self.assertFalse(report.dry_run)
            self.assertEqual(report.sessions_copied, 2)
            self.assertEqual(report.brainstorms_copied, 1)
            self.assertTrue(report.pinned_copied)
            self.assertEqual(report.skipped_existing, 0)
            self.assertEqual(_snapshot_tree(v2_src), source_before)
            self.assertEqual(
                layout.pinned_global_file.read_text(encoding="utf-8"),
                custom_global,
            )

            sess_out = vault_root / "hosts" / "host-alpha" / "sessions" / "2026-10" / "sess-one.md"
            sess_text = sess_out.read_text(encoding="utf-8")
            self.assertNotIn(openai_proj, sess_text)
            self.assertNotIn(str(mock_home), sess_text)
            self.assertIn("[REDACTED:OPENAI_KEY]", sess_text)
            self.assertIn("~/repo", sess_text)

            dated_out = (
                vault_root / "hosts" / "host-alpha" / "sessions" / "2026-11"
                / "nested__2026-11-05-dated.md"
            )
            self.assertTrue(dated_out.is_file())

            pinned_out = vault_root / "hosts" / "host-alpha" / "pinned_host.md"
            self.assertIn("[REDACTED:GITHUB_PAT]", pinned_out.read_text(encoding="utf-8"))
            self.assertNotIn(gh_pat, pinned_out.read_text(encoding="utf-8"))

            bs_out = (
                vault_root / "shared" / "brainstorms" / "by-host" / "host-alpha" / "arch-design.md"
            )
            bs_text = bs_out.read_text(encoding="utf-8")
            self.assertIn("[REDACTED:AWS_ACCESS_KEY]", bs_text)
            self.assertNotIn(aws_key, bs_text)

            meta_out = bs_out.with_suffix(".meta.json")
            meta = json.loads(meta_out.read_text(encoding="utf-8"))
            self.assertEqual(
                set(meta.keys()),
                {
                    "schema_version",
                    "bs_id",
                    "title",
                    "origin_host",
                    "status",
                    "line_count",
                    "sha256",
                    "created_at",
                    "updated_at",
                },
            )
            self.assertEqual(meta["schema_version"], 1)
            self.assertEqual(meta["bs_id"], "arch-design")
            self.assertEqual(meta["title"], "Architecture Design")
            self.assertEqual(meta["origin_host"], "host-alpha")
            self.assertEqual(meta["status"], "decided")
            self.assertEqual(meta["line_count"], len(bs_text.splitlines()))
            self.assertEqual(meta["sha256"], hashlib.sha256(bs_text.encode("utf-8")).hexdigest())

            mutations = vault_root / "hosts" / "host-alpha" / "events" / "mutations.jsonl"
            self.assertTrue(mutations.is_file())
            self.assertIn("v2-import", mutations.read_text(encoding="utf-8"))

            diag_lines = (cache_dir / "mipmem.log").read_text(encoding="utf-8").splitlines()
            vlt_logs = [json.loads(ln) for ln in diag_lines if json.loads(ln)["event"] == CP_VLT_01]
            self.assertTrue(vlt_logs)
            latest = vlt_logs[-1]["details"]
            for field in (
                "dry_run",
                "sessions_copied",
                "brainstorms_copied",
                "skipped_existing",
                "duration_ms",
            ):
                self.assertIn(field, latest)

    def test_second_apply_is_idempotent_and_protects_conflicts_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            v2_src = base / "legacy_v2"
            vault_root = base / "vault"
            cache_dir = base / "cache"
            (v2_src / "sessions" / "2026-11").mkdir(parents=True)
            (v2_src / "brainstorms").mkdir(parents=True)

            (v2_src / "sessions" / "2026-11" / "s1.md").write_text("# First\n", encoding="utf-8")
            (v2_src / "brainstorms" / "idea.md").write_text("# Idea\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                first = migrate_from_v2(v2_src, vault_root, host_id="host-alpha", dry_run=False)
                self.assertEqual(first.sessions_copied, 1)
                self.assertEqual(first.brainstorms_copied, 1)

                second = migrate_from_v2(v2_src, vault_root, host_id="host-alpha", dry_run=False)
                self.assertEqual(second.sessions_copied, 0)
                self.assertEqual(second.brainstorms_copied, 0)
                self.assertGreaterEqual(second.skipped_existing, 2)
                self.assertEqual(second.conflicts_skipped, 0)

                # Modify target file in vault to trigger conflict when source differs.
                target_sess = vault_root / "hosts" / "host-alpha" / "sessions" / "2026-11" / "s1.md"
                target_sess.write_text("# Edited Manually In Vault\n", encoding="utf-8")
                (v2_src / "sessions" / "2026-11" / "s1.md").write_text(
                    "# Updated Upstream Source\n",
                    encoding="utf-8",
                )

                blocked = migrate_from_v2(
                    v2_src,
                    vault_root,
                    host_id="host-alpha",
                    dry_run=False,
                    force=False,
                )
                self.assertEqual(blocked.conflicts_skipped, 1)
                self.assertEqual(
                    target_sess.read_text(encoding="utf-8"),
                    "# Edited Manually In Vault\n",
                )

                forced = migrate_from_v2(
                    v2_src,
                    vault_root,
                    host_id="host-alpha",
                    dry_run=False,
                    force=True,
                )
                self.assertEqual(forced.conflicts_skipped, 0)
                self.assertEqual(forced.sessions_copied, 1)
                self.assertEqual(
                    target_sess.read_text(encoding="utf-8"),
                    "# Updated Upstream Source\n",
                )


if __name__ == "__main__":
    unittest.main()
