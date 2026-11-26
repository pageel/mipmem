"""Unit and integration tests for MipMem multi-host sync backends and detached Stop worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mipmem.cli_commands import _build_parser
from mipmem.config import default_config
from mipmem.core.brainstorms import save_brainstorm_shard, update_brainstorm_status
from mipmem.core.diagnostics import (
    CP_SYN_01,
    SYNC_BACKEND_UNCONFIGURED,
    SYNC_CONFLICT_DETECTED,
    SYNC_TRANSPORT_FAILED,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.fts_recall import list_indexed_brainstorms, sync_fts_index
from mipmem.core.mipmap import render_l0_radar
from mipmem.core.sessions import SessionRecord, write_session_record
from mipmem.core.subapps import build_fleet_status, read_sync_alert
from mipmem.core.vault import init_vault
from mipmem.sync.base import (
    SyncResult,
    is_sync_configured,
    read_sync_status,
    resolve_sync_backend,
    write_sync_status,
)
from mipmem.sync.folder_backend import FolderSyncBackend
from mipmem.sync.git_backend import (
    GitSyncBackend,
    _build_detached_sync_argv,
    spawn_detached_sync,
)


class TestSyncProtocolAndStatusStore(unittest.TestCase):
    """Verify SyncBackend Protocol, atomic sync_status.json, and config resolution (Task 2.1)."""

    def test_sync_init_has_zero_eager_submodules(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import mipmem.sync, sys; "
                "mods=[m for m in sys.modules if m.startswith('mipmem.sync.')]; "
                "assert not mods, mods; print('SYNC_INIT_LAZY_OK')",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("SYNC_INIT_LAZY_OK", proc.stdout)

    def test_sync_result_and_atomic_status_store_with_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "cache"
            secret = "ghp_" + "G" * 24
            res = SyncResult(
                backend="git",
                direction="sync",
                ok=False,
                files_changed=2,
                conflict=True,
                error_code="SYNC_CONFLICT_DETECTED",
                message=f"Conflict pushing with token {secret}",
                host_id="host-alpha",
                last_sync_utc="2026-11-23T12:00:00.000Z",
                duration_ms=14.25,
            )
            self.assertEqual(res.to_dict()["error_code"], "SYNC_CONFLICT_DETECTED")

            written = write_sync_status(cache_dir, res)
            self.assertEqual(written, cache_dir / "sync_status.json")
            raw = written.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            self.assertIn("[REDACTED:GITHUB_PAT]", raw)

            loaded = read_sync_status(cache_dir)
            assert loaded is not None
            self.assertTrue(loaded["conflict"])
            self.assertEqual(loaded["backend"], "git")

            # Corrupt JSON or symlink must fail open to None
            (cache_dir / "sync_status.json").write_text("{corrupt", encoding="utf-8")
            self.assertIsNone(read_sync_status(cache_dir))

            target = Path(tmp_dir) / "real.json"
            target.write_text(json.dumps({"backend": "git"}), encoding="utf-8")
            (cache_dir / "sync_status.json").unlink()
            (cache_dir / "sync_status.json").symlink_to(target)
            self.assertIsNone(read_sync_status(cache_dir))

    def test_resolve_sync_backend_validation_and_legacy_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cache_dir = root / "cache"
            cfg = default_config()

            ok, _, _ = is_sync_configured(cfg)
            self.assertFalse(ok)
            with self.assertRaises(MipmemError) as ctx_none:
                resolve_sync_backend(cfg, vault_dir=vault_dir, cache_dir=cache_dir)
            self.assertEqual(ctx_none.exception.code, SYNC_BACKEND_UNCONFIGURED)

            cfg_git_tbd = default_config()
            cfg_git_tbd["sync"]["backend"] = "git"
            with self.assertRaises(MipmemError) as ctx_tbd:
                resolve_sync_backend(cfg_git_tbd, vault_dir=vault_dir, cache_dir=cache_dir)
            self.assertEqual(ctx_tbd.exception.code, SYNC_BACKEND_UNCONFIGURED)

            cfg_folder_empty = default_config()
            cfg_folder_empty["sync"]["backend"] = "folder"
            cfg_folder_empty["sync"].pop("folder_path", None)
            with self.assertRaises(MipmemError) as ctx_folder:
                resolve_sync_backend(cfg_folder_empty, vault_dir=vault_dir, cache_dir=cache_dir)
            self.assertEqual(ctx_folder.exception.code, SYNC_BACKEND_UNCONFIGURED)

            shared_dir = root / "shared_folder"
            shared_dir.mkdir()
            cfg_folder_ok = default_config()
            cfg_folder_ok["sync"]["backend"] = "folder"
            cfg_folder_ok["sync"]["folder_path"] = str(shared_dir)
            ok_folder, backend_name, target = is_sync_configured(cfg_folder_ok)
            self.assertTrue(ok_folder)
            self.assertEqual(backend_name, "folder")
            self.assertEqual(target, str(shared_dir))


class TestGitSyncBackend(unittest.TestCase):
    """Verify conflict-free 2-host bare Git sync, rebase abort, and CP-SYN-01 (Task 2.2)."""

    def _make_git_backend(
        self,
        root: Path,
        host_id: str,
        remote_url: str,
    ) -> tuple[Path, Path, Path, RotatingJsonLogger, GitSyncBackend]:
        vault_dir = root / f"vault-{host_id}"
        cache_dir = root / f"cache-{host_id}"
        log_path = cache_dir / "mipmem.log"
        init_vault(vault_dir, host_id=host_id)
        logger = RotatingJsonLogger(log_path=log_path)
        backend = GitSyncBackend(
            vault_dir=vault_dir,
            cache_dir=cache_dir,
            remote_url=remote_url,
            host_id=host_id,
            logger=logger,
        )
        return vault_dir, cache_dir, log_path, logger, backend

    def test_two_hosts_concurrent_bare_git_zero_conflicts_and_lww_convergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bare_dir = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True
            )

            vault_a, cache_a, log_a, _, backend_a = self._make_git_backend(
                root, "host-alpha", str(bare_dir)
            )
            vault_b, cache_b, _, _, backend_b = self._make_git_backend(
                root, "host-beta", str(bare_dir)
            )

            write_session_record(
                vault_a,
                SessionRecord(
                    conversation_id="conv-alpha-01",
                    host_id="host-alpha",
                    surface="cli",
                    last_modified_time="2026-11-23T08:00:00.000Z",
                    step_count=10,
                    title="Alpha initial design",
                    edited_files=["src/a.py"],
                    recent_prompts=["Init sync design"],
                    last_agent_response="Done.",
                ),
            )
            shard_a = save_brainstorm_shard(
                vault_a,
                "Multi-Host Strategy",
                "# Multi-Host Strategy\n\n## Plan\nUse sharded folders.\n",
                host_id="host-alpha",
                status="open",
            )
            sync_a1 = backend_a.sync()
            self.assertTrue(sync_a1.ok, msg=sync_a1.message)
            self.assertFalse(sync_a1.conflict)

            pull_b1 = backend_b.pull()
            self.assertTrue(pull_b1.ok, msg=pull_b1.message)
            self.assertTrue(
                (vault_b / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-alpha-01.md")
                .is_file()
            )

            # Concurrent unpulled writes on both hosts + cross-host CRDT-lite mutation
            write_session_record(
                vault_a,
                SessionRecord(
                    conversation_id="conv-alpha-02",
                    host_id="host-alpha",
                    surface="ide",
                    last_modified_time="2026-11-23T09:00:00.000Z",
                    step_count=12,
                    title="Alpha concurrent session",
                    edited_files=["src/b.py"],
                    recent_prompts=["Work offline"],
                    last_agent_response="Ready.",
                ),
            )
            write_session_record(
                vault_b,
                SessionRecord(
                    conversation_id="conv-beta-01",
                    host_id="host-beta",
                    surface="ide",
                    last_modified_time="2026-11-23T09:05:00.000Z",
                    step_count=14,
                    title="Beta concurrent session",
                    edited_files=["src/c.py"],
                    recent_prompts=["Review and decide"],
                    last_agent_response="Decided.",
                ),
            )
            save_brainstorm_shard(
                vault_b,
                "Beta Followup",
                "# Beta Followup\n\n## Notes\nConcurrent shard.\n",
                host_id="host-beta",
                status="open",
            )
            update_brainstorm_status(
                vault_b, shard_a.bs_id, "decided", actor_host="host-beta"
            )

            # Build local .cache DB on both hosts to ensure .cache/ is never synced
            sync_fts_index(vault_a)
            sync_fts_index(vault_b)

            sync_a2 = backend_a.sync()
            self.assertTrue(sync_a2.ok, msg=sync_a2.message)
            sync_b2 = backend_b.sync()
            self.assertTrue(sync_b2.ok, msg=sync_b2.message)
            self.assertFalse(sync_b2.conflict)
            pull_a2 = backend_a.pull()
            self.assertTrue(pull_a2.ok, msg=pull_a2.message)

            # Verify convergence on both hosts (files + LWW status)
            self.assertTrue(
                (vault_a / "hosts" / "host-beta" / "sessions" / "2026-11" / "conv-beta-01.md")
                .is_file()
            )
            self.assertTrue(
                (vault_b / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-alpha-02.md")
                .is_file()
            )
            sync_fts_index(vault_a)
            sync_fts_index(vault_b)
            bs_a = {b["bs_id"]: b for b in list_indexed_brainstorms(vault_a, auto_sync=False)}
            bs_b = {b["bs_id"]: b for b in list_indexed_brainstorms(vault_b, auto_sync=False)}
            self.assertEqual(bs_a[shard_a.bs_id]["effective_status"], "decided")
            self.assertEqual(bs_b[shard_a.bs_id]["effective_status"], "decided")

            ls_tree = subprocess.run(
                ["git", "--git-dir", str(bare_dir), "ls-tree", "-r", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertNotIn(".cache/", ls_tree.stdout)
            status_a = read_sync_status(cache_a)
            assert status_a is not None
            self.assertTrue(status_a["ok"])
            self.assertFalse(status_a["conflict"])
            log_entries = [
                json.loads(line) for line in log_a.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(
                    e["event"] == CP_SYN_01 and e["details"]["backend"] == "git"
                    for e in log_entries
                )
            )

    def test_manual_shared_file_conflict_aborts_rebase_and_surfaces_l0_fleet_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bare_dir = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True
            )

            vault_a, _, _, _, backend_a = self._make_git_backend(root, "host-alpha", str(bare_dir))
            vault_b, cache_b, _, _, backend_b = self._make_git_backend(
                root, "host-beta", str(bare_dir)
            )

            pinned_a = vault_a / "shared" / "pinned_global.md"
            pinned_a.write_text("# Global Pins\nLine 1: alpha initial\n", encoding="utf-8")
            self.assertTrue(backend_a.sync().ok)
            self.assertTrue(backend_b.pull().ok)

            # Edit the same line differently on both hosts without pulling first
            pinned_a.write_text("# Global Pins\nLine 1: alpha override\n", encoding="utf-8")
            self.assertTrue(backend_a.sync().ok)

            pinned_b = vault_b / "shared" / "pinned_global.md"
            pinned_b.write_text(
                "# Global Pins\nLine 1: beta conflicting override\n", encoding="utf-8"
            )

            res_b = backend_b.sync()
            self.assertFalse(res_b.ok)
            self.assertTrue(res_b.conflict)
            self.assertEqual(res_b.error_code, SYNC_CONFLICT_DETECTED)
            self.assertFalse((vault_b / ".git" / "rebase-merge").exists())
            self.assertFalse((vault_b / ".git" / "rebase-apply").exists())

            cached = read_sync_status(cache_b)
            assert cached is not None
            self.assertTrue(cached["conflict"])
            self.assertEqual(cached["error_code"], SYNC_CONFLICT_DETECTED)

            alert = read_sync_alert(cache_b)
            self.assertIsNotNone(alert)
            l0 = render_l0_radar(sync_alert=alert)
            self.assertIn("SYNC WARN", l0.content)
            fleet = build_fleet_status(vault_b, active_host_id="host-beta", cache_dir=cache_b)
            self.assertTrue(fleet["sync_status"]["conflict"])

    def test_git_transport_error_fails_fast_and_logs_cp_syn_01(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _, cache_a, log_a, _, backend_a = self._make_git_backend(
                root, "host-alpha", str(root / "nonexistent-remote.git")
            )
            res = backend_a.sync()
            self.assertFalse(res.ok)
            self.assertFalse(res.conflict)
            self.assertEqual(res.error_code, SYNC_TRANSPORT_FAILED)
            cached = read_sync_status(cache_a)
            assert cached is not None
            self.assertEqual(cached["error_code"], SYNC_TRANSPORT_FAILED)
            entries = [
                json.loads(line) for line in log_a.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(
                    e["event"] == CP_SYN_01 and e["error_code"] == SYNC_TRANSPORT_FAILED
                    for e in entries
                )
            )


class TestDetachedStopSyncWorker(unittest.TestCase):
    """Verify non-blocking detached background sync spawn gating and Popen flags (Task 2.3)."""

    def test_spawn_detached_sync_gates_and_popen_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cfg_file = root / "config" / "config.toml"
            log_path = root / "mipmem.log"
            vault_dir.mkdir(parents=True)
            cfg_file.parent.mkdir(parents=True)
            logger = RotatingJsonLogger(log_path=log_path)

            with mock.patch("mipmem.sync.git_backend.subprocess.Popen") as popen_mock:
                # 1. backend == "none" -> skipped
                cfg_none = default_config()
                self.assertFalse(
                    spawn_detached_sync(vault_dir=vault_dir, cfg=cfg_none, logger=logger)
                )
                popen_mock.assert_not_called()

                # 2. auto_sync_on_stop == False -> skipped
                cfg_disabled = default_config()
                cfg_disabled["sync"]["backend"] = "git"
                cfg_disabled["sync"]["vault_remote_url"] = str(root / "bare.git")
                cfg_disabled["sync"]["auto_sync_on_stop"] = False
                self.assertFalse(
                    spawn_detached_sync(vault_dir=vault_dir, cfg=cfg_disabled, logger=logger)
                )
                popen_mock.assert_not_called()

                # 3. Configured git backend -> detached Popen with start_new_session & DEVNULL
                cfg_ok = default_config()
                cfg_ok["sync"]["backend"] = "git"
                cfg_ok["sync"]["vault_remote_url"] = str(root / "bare.git")
                cfg_ok["sync"]["auto_sync_on_stop"] = True
                self.assertTrue(
                    spawn_detached_sync(
                        vault_dir=vault_dir,
                        config_file=cfg_file,
                        cfg=cfg_ok,
                        logger=logger,
                    )
                )
                popen_mock.assert_called_once()
                call_args, call_kwargs = popen_mock.call_args
                argv = call_args[0]
                self.assertIn("sync", argv)
                self.assertIn("--detached", argv)
                self.assertIn(str(vault_dir), argv)
                self.assertIn(str(cfg_file), argv)
                self.assertTrue(call_kwargs["start_new_session"])
                self.assertEqual(call_kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(call_kwargs["stdout"], subprocess.DEVNULL)
                self.assertEqual(call_kwargs["stderr"], subprocess.DEVNULL)
                self.assertEqual(call_kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

            # 4. OSError on Popen must fail-open returning False
            with mock.patch(
                "mipmem.sync.git_backend.subprocess.Popen",
                side_effect=OSError("spawn failed"),
            ):
                self.assertFalse(
                    spawn_detached_sync(vault_dir=vault_dir, cfg=cfg_ok, logger=logger)
                )

            entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(
                    e["event"] == CP_SYN_01
                    and e.get("details", {}).get("direction") == "detached_spawn"
                    for e in entries
                )
            )

    def test_detached_sync_argv_parser_contract_and_real_git_folder_e2e(self) -> None:
        src_root = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home_dir = root / "home"
            unused_cfg_dir = root / "unused-xdg-cfg"
            home_dir.mkdir(parents=True)
            unused_cfg_dir.mkdir(parents=True)

            # 1. Caller <-> Parser Contract Test (MP7)
            vault_git = root / "vault-git"
            cache_git = root / "cache-git"
            cfg_git = root / "custom-git" / "config.toml"
            bare_repo = root / "bare.git"
            cfg_git.parent.mkdir(parents=True)
            cache_git.mkdir(parents=True)
            init_vault(vault_git, host_id="host-detached-git")
            session_git = (
                vault_git / "hosts" / "host-detached-git" / "sessions" / "2026-11" / "conv-git1.md"
            )
            session_git.parent.mkdir(parents=True, exist_ok=True)
            session_git.write_text("# Detached Git E2E\n", encoding="utf-8")

            bare_init = subprocess.run(
                ["git", "init", "--bare", str(bare_repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bare_init.returncode, 0, msg=bare_init.stderr)
            cfg_git.write_text(
                f'[core]\nhost_id = "host-detached-git"\n'
                f'vault_path = "{vault_git}"\n'
                f'locale = "en"\n'
                f'[sync]\nbackend = "git"\n'
                f'vault_remote_url = "{bare_repo}"\n'
                f'auto_sync_on_stop = true\n',
                encoding="utf-8",
            )

            argv_git, _ = _build_detached_sync_argv(vault_git, cfg_git)
            sync_idx = argv_git.index("sync")
            cli_tokens = argv_git[sync_idx:]
            parsed = _build_parser().parse_args(cli_tokens)
            self.assertEqual(parsed.command, "sync")
            self.assertTrue(parsed.detached)
            self.assertEqual(parsed.vault, str(vault_git))
            self.assertEqual(parsed.config, str(cfg_git))
            self.assertFalse(parsed.if_configured)

            # 2. Real non-mocked subprocess E2E with bare Git remote & custom --config
            base_env = {
                key: val
                for key, val in os.environ.items()
                if key not in ("PYTHONPATH", "MIPMEM_VAULT_DIR") and not key.startswith("XDG_")
            }
            base_env.update(
                {
                    "PYTHONPATH": src_root,
                    "HOME": str(home_dir),
                    "MIPMEM_CONFIG_DIR": str(unused_cfg_dir),
                    "MIPMEM_CACHE_DIR": str(cache_git),
                }
            )
            proc_git = subprocess.run(
                argv_git,
                env=base_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                proc_git.returncode,
                0,
                msg=f"stdout={proc_git.stdout}\nstderr={proc_git.stderr}",
            )
            ls_tree = subprocess.run(
                ["git", "--git-dir", str(bare_repo), "ls-tree", "-r", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ls_tree.returncode, 0, msg=ls_tree.stderr)
            self.assertIn(
                "hosts/host-detached-git/sessions/2026-11/conv-git1.md",
                ls_tree.stdout,
            )
            status_git = read_sync_status(cache_git)
            assert status_git is not None
            self.assertTrue(status_git["ok"])
            self.assertEqual(status_git["backend"], "git")
            self.assertEqual(status_git["host_id"], "host-detached-git")

            # 3. Real non-mocked subprocess E2E with Folder backend & custom --config
            vault_folder = root / "vault-folder"
            cache_folder = root / "cache-folder"
            shared_dir = root / "shared-folder"
            cfg_folder = root / "custom-folder" / "config.toml"
            cache_folder.mkdir(parents=True)
            shared_dir.mkdir(parents=True)
            cfg_folder.parent.mkdir(parents=True)
            init_vault(vault_folder, host_id="host-detached-folder")
            session_folder = (
                vault_folder
                / "hosts"
                / "host-detached-folder"
                / "sessions"
                / "2026-11"
                / "conv-fld1.md"
            )
            session_folder.parent.mkdir(parents=True, exist_ok=True)
            session_folder.write_text("# Detached Folder E2E\n", encoding="utf-8")
            cfg_folder.write_text(
                f'[core]\nhost_id = "host-detached-folder"\n'
                f'vault_path = "{vault_folder}"\n'
                f'locale = "vi"\n'
                f'[sync]\nbackend = "folder"\n'
                f'folder_path = "{shared_dir}"\n',
                encoding="utf-8",
            )
            argv_folder, _ = _build_detached_sync_argv(vault_folder, cfg_folder)
            env_folder = {**base_env, "MIPMEM_CACHE_DIR": str(cache_folder)}
            proc_folder = subprocess.run(
                argv_folder,
                env=env_folder,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                proc_folder.returncode,
                0,
                msg=f"stdout={proc_folder.stdout}\nstderr={proc_folder.stderr}",
            )
            self.assertTrue(
                (
                    shared_dir
                    / "hosts"
                    / "host-detached-folder"
                    / "sessions"
                    / "2026-11"
                    / "conv-fld1.md"
                ).is_file()
            )
            status_folder = read_sync_status(cache_folder)
            assert status_folder is not None
            self.assertTrue(status_folder["ok"])
            self.assertEqual(status_folder["backend"], "folder")
            self.assertEqual(status_folder["host_id"], "host-detached-folder")


class TestFolderSyncBackend(unittest.TestCase):
    """Verify single-writer FolderSyncBackend, symlink/nesting guards, and CP-SYN-01 (Task 2.4)."""

    def test_two_host_single_writer_folder_sync_ignores_cache_and_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "syncthing_vault"
            vault_a = root / "vault-alpha"
            vault_b = root / "vault-beta"
            cache_a = root / "cache-alpha"
            cache_b = root / "cache-beta"
            init_vault(vault_a, host_id="host-alpha")
            init_vault(vault_b, host_id="host-beta")

            # Seed dummy .cache and .git artifacts inside vault_a that MUST NOT sync
            (vault_a / ".cache" / "local.db").parent.mkdir(parents=True, exist_ok=True)
            (vault_a / ".cache" / "local.db").write_text("DO_NOT_COPY", encoding="utf-8")
            (vault_a / ".git").mkdir(parents=True, exist_ok=True)
            (vault_a / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

            write_session_record(
                vault_a,
                SessionRecord(
                    conversation_id="conv-folder-a1",
                    host_id="host-alpha",
                    surface="cli",
                    last_modified_time="2026-11-23T10:00:00.000Z",
                    step_count=8,
                    title="Alpha folder sync record",
                    edited_files=["a.py"],
                    recent_prompts=["Test folder backend"],
                    last_agent_response="Ready.",
                ),
            )
            shard_a = save_brainstorm_shard(
                vault_a,
                "Folder Sync Spec",
                "# Folder Sync Spec\n\n## Core\nSingle-writer folder copy.\n",
                host_id="host-alpha",
                status="open",
            )
            write_session_record(
                vault_b,
                SessionRecord(
                    conversation_id="conv-folder-b1",
                    host_id="host-beta",
                    surface="ide",
                    last_modified_time="2026-11-23T10:05:00.000Z",
                    step_count=10,
                    title="Beta folder sync record",
                    edited_files=["b.py"],
                    recent_prompts=["Push beta"],
                    last_agent_response="OK.",
                ),
            )

            log_a = cache_a / "mipmem.log"
            backend_a = FolderSyncBackend(
                vault_dir=vault_a,
                cache_dir=cache_a,
                folder_path=shared_dir,
                host_id="host-alpha",
                logger=RotatingJsonLogger(log_path=log_a),
            )
            backend_b = FolderSyncBackend(
                vault_dir=vault_b,
                cache_dir=cache_b,
                folder_path=shared_dir,
                host_id="host-beta",
            )

            # Seed foreign stale file for host-beta in shared_dir; alpha.push() must NOT touch it
            beta_shared_note = shared_dir / "hosts" / "host-beta" / "notes.txt"
            beta_shared_note.parent.mkdir(parents=True, exist_ok=True)
            beta_shared_note.write_text("beta-untouched", encoding="utf-8")

            push_a = backend_a.push()
            self.assertTrue(push_a.ok, msg=push_a.message)
            self.assertGreater(push_a.files_changed, 0)
            self.assertEqual(beta_shared_note.read_text(encoding="utf-8"), "beta-untouched")
            self.assertFalse((shared_dir / ".cache").exists())
            self.assertFalse((shared_dir / ".git").exists())

            # Poison host-alpha's own file in shared_dir; alpha.pull() must NOT overwrite local host
            local_a_session = (
                vault_a / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-folder-a1.md"
            )
            original_a_text = local_a_session.read_text(encoding="utf-8")
            shared_a_session = (
                shared_dir / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-folder-a1.md"
            )
            shared_a_session.write_text("TAMPERED_BY_OTHER_MACHINE", encoding="utf-8")

            sync_b = backend_b.sync()
            self.assertTrue(sync_b.ok, msg=sync_b.message)
            pull_a = backend_a.pull()
            self.assertTrue(pull_a.ok, msg=pull_a.message)

            self.assertEqual(local_a_session.read_text(encoding="utf-8"), original_a_text)
            self.assertTrue(
                (vault_a / "hosts" / "host-beta" / "sessions" / "2026-11" / "conv-folder-b1.md")
                .is_file()
            )
            bs_b = {b["bs_id"]: b for b in list_indexed_brainstorms(vault_b, auto_sync=True)}
            self.assertIn(shard_a.bs_id, bs_b)

            cached = read_sync_status(cache_a)
            assert cached is not None
            self.assertEqual(cached["backend"], "folder")
            entries = [
                json.loads(line) for line in log_a.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(
                    e["event"] == CP_SYN_01 and e["details"]["backend"] == "folder"
                    for e in entries
                )
            )

    def test_folder_sync_rejects_nested_vault_dir_and_escaping_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            init_vault(vault_dir, host_id="host-beta")
            cache_dir = root / "cache"

            # 1. Nested inside vault_dir, equal to vault_dir, ancestor of vault_dir, or symlink
            nested_path = vault_dir / "nested_sync"
            sym_path = root / "sym_sync"
            real_target = root / "real_sync"
            real_target.mkdir()
            sym_path.symlink_to(real_target)

            for bad_folder in (vault_dir, nested_path, root, sym_path):
                backend = FolderSyncBackend(
                    vault_dir=vault_dir,
                    cache_dir=cache_dir,
                    folder_path=bad_folder,
                    host_id="host-beta",
                )
                with self.assertRaises(MipmemError) as ctx:
                    backend.sync()
                self.assertEqual(ctx.exception.code, SYNC_TRANSPORT_FAILED)

            # 2. Symlinks inside remote host shard MUST be skipped on pull()
            outside = root / "outside_secret.txt"
            outside.write_text("SECRET_CONTENT", encoding="utf-8")
            evil_dir = real_target / "hosts" / "host-alpha" / "sessions"
            evil_dir.mkdir(parents=True)
            (evil_dir / "escape.md").symlink_to(outside)

            ok_backend = FolderSyncBackend(
                vault_dir=vault_dir,
                cache_dir=cache_dir,
                folder_path=real_target,
                host_id="host-beta",
            )
            res = ok_backend.pull()
            self.assertTrue(res.ok)
            escaped = vault_dir / "hosts" / "host-alpha" / "sessions" / "escape.md"
            self.assertFalse(escaped.exists())

    def test_join_git_vault_e2e_conflict_guards_adopt_host_and_unborn_remote(self) -> None:
        from mipmem.config import load_config
        from mipmem.core.diagnostics import (
            CLI_INVALID_ARGUMENT,
            JOIN_HOST_CONFLICT,
            JOIN_VAULT_OCCUPIED,
        )
        from mipmem.core.fts_recall import recall_memory

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bare_remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True,
                           capture_output=True, text=True)

            # Host alpha initializes vault, creates session + brainstorm, syncs to bare remote.
            vault_a = root / "vault-alpha"
            cache_a = root / "cache-alpha"
            init_vault(vault_a, host_id="host-alpha")
            write_session_record(
                vault_a,
                SessionRecord(
                    conversation_id="conv-alpha-join-01",
                    host_id="host-alpha",
                    surface="cli",
                    title="Distributed onboarding checkpoint",
                    last_modified_time="2026-11-26T06:00:00Z",
                    recent_prompts=["Onboard machine with mipmem join command"],
                ),
            )
            save_brainstorm_shard(
                vault_a,
                "Cross Host Sync Plan",
                "## Architecture\nZero conflict host shards.\n",
                host_id="host-alpha",
            )
            backend_a = GitSyncBackend(
                vault_dir=vault_a,
                cache_dir=cache_a,
                remote_url=str(bare_remote),
                host_id="host-alpha",
            )
            self.assertTrue(backend_a.sync().ok)

            tree = subprocess.run(
                ["git", "--git-dir", str(bare_remote), "ls-tree", "-r", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn("AGENTS.md", tree)
            self.assertIn("README.md", tree)

            from mipmem.sync.git_backend import join_git_vault, validate_git_remote_url

            tbd = "[" + "TBD]"
            for bad_url in ("", tbd, "-oProxyCommand=id", "--upload-pack=sh", "a\nb", "a\x00b"):
                with self.assertRaises(MipmemError) as url_ctx:
                    validate_git_remote_url(bad_url)
                self.assertEqual(url_ctx.exception.code, CLI_INVALID_ARGUMENT)

            # Host beta joins bare remote into fresh directory -> recalls alpha + pushes cleanly.
            vault_b = root / "vault-beta"
            cache_b = root / "cache-beta"
            cfg_b = root / "cfg-beta" / "config.toml"
            log_b = cache_b / "mipmem.log"
            join_res = join_git_vault(
                str(bare_remote),
                vault_dir=vault_b,
                cache_dir=cache_b,
                config_file=cfg_b,
                host_id="host-beta",
                locale="en",
                logger=RotatingJsonLogger(log_path=log_b),
            )
            self.assertTrue(join_res.ok)
            self.assertEqual(join_res.host_id, "host-beta")
            self.assertEqual(join_res.existing_hosts, ["host-alpha"])
            self.assertTrue((vault_b / "AGENTS.md").is_file())
            self.assertGreaterEqual(join_res.indexed_sessions, 1)
            loaded_b = load_config(cfg_b)
            self.assertEqual(loaded_b["core"]["host_id"], "host-beta")
            self.assertEqual(loaded_b["sync"]["backend"], "git")
            self.assertEqual(loaded_b["sync"]["vault_remote_url"], str(bare_remote))
            hits = recall_memory(vault_b, "Onboard machine with mipmem join", locale="en")
            self.assertGreaterEqual(len(hits.hits), 1)
            filemode = subprocess.run(
                ["git", "-C", str(vault_b), "config", "--get", "core.filemode"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(filemode, "false")

            # Colliding host_id rejected without --adopt-host, allowed with adopt_host=True.
            vault_dup = root / "vault-dup"
            with self.assertRaises(MipmemError) as dup_ctx:
                join_git_vault(
                    str(bare_remote),
                    vault_dir=vault_dup,
                    cache_dir=root / "cache-dup",
                    config_file=root / "cfg-dup" / "config.toml",
                    host_id="host-alpha",
                    adopt_host=False,
                )
            self.assertEqual(dup_ctx.exception.code, JOIN_HOST_CONFLICT)

            adopted = join_git_vault(
                str(bare_remote),
                vault_dir=vault_dup,
                cache_dir=root / "cache-dup",
                config_file=root / "cfg-dup" / "config.toml",
                host_id="host-alpha",
                adopt_host=True,
            )
            self.assertTrue(adopted.ok)
            self.assertEqual(adopted.host_id, "host-alpha")

            # Occupied non-matching vault dirs rejected cleanly without mutating files.
            occupied_dir = root / "occupied-non-git"
            occupied_dir.mkdir()
            (occupied_dir / "keep.txt").write_text("keep me", encoding="utf-8")
            with self.assertRaises(MipmemError) as occ_ctx:
                join_git_vault(
                    str(bare_remote),
                    vault_dir=occupied_dir,
                    cache_dir=root / "cache-occ",
                    config_file=root / "cfg-occ" / "config.toml",
                    host_id="host-gamma",
                )
            self.assertEqual(occ_ctx.exception.code, JOIN_VAULT_OCCUPIED)
            self.assertEqual((occupied_dir / "keep.txt").read_text(encoding="utf-8"), "keep me")

            other_remote = root / "other-remote.git"
            subprocess.run(["git", "init", "--bare", str(other_remote)], check=True,
                           capture_output=True, text=True)
            with self.assertRaises(MipmemError) as mismatch_ctx:
                join_git_vault(
                    str(other_remote),
                    vault_dir=vault_b,
                    cache_dir=cache_b,
                    config_file=cfg_b,
                    host_id="host-beta",
                )
            self.assertEqual(mismatch_ctx.exception.code, JOIN_VAULT_OCCUPIED)

            # Empty unborn bare repository join succeeds and creates initial commit.
            unborn_remote = root / "unborn.git"
            subprocess.run(["git", "init", "--bare", str(unborn_remote)], check=True,
                           capture_output=True, text=True)
            unborn_res = join_git_vault(
                str(unborn_remote),
                vault_dir=root / "vault-unborn",
                cache_dir=root / "cache-unborn",
                config_file=root / "cfg-unborn" / "config.toml",
                host_id="host-delta",
            )
            self.assertTrue(unborn_res.ok)
            self.assertEqual(unborn_res.existing_hosts, [])


if __name__ == "__main__":
    unittest.main()
