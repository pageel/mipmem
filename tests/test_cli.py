"""Tests for lazy-import CLI dispatcher, config validation, and error envelopes."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from mipmem.cli import main as cli_main
from mipmem.cli_commands import dispatch_full_cli
from mipmem.config import load_config
from mipmem.core.diagnostics import (
    BRAINSTORM_NOT_FOUND,
    CLI_INVALID_ARGUMENT,
    CONFIG_INVALID_HOST_ID,
    CONFIG_INVALID_TOML,
    CP_SYN_01,
    INVALID_BRAINSTORM_STATUS,
    INVALID_SLICE_SELECTOR,
    JOIN_HOST_CONFLICT,
    MIGRATE_SOURCE_MISSING,
    SYNC_BACKEND_UNCONFIGURED,
    VAULT_NOT_INITIALIZED,
    VAULT_PATH_TRAVERSAL,
    register_default_scrubber,
)
from mipmem.core.updater import UpdateResult
from mipmem.core.vault import init_vault
from mipmem.sync.base import read_sync_status

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(
    argv: list[str],
    env_overrides: dict[str, str],
) -> tuple[int, str, str]:
    """Invoke cli.main with captured stdout/stderr and isolated environment variables."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with (
        mock.patch.dict(os.environ, env_overrides, clear=False),
        contextlib.redirect_stdout(out_buf),
        contextlib.redirect_stderr(err_buf),
    ):
        exit_code = cli_main(argv)
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


class TestCliDispatcher(unittest.TestCase):
    """Verify subcommands, lazy imports, safe config coercion, and Spec S1 error envelopes."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_strict_lazy_import_on_cli_module_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            probe_code = (
                "import sys; "
                "import mipmem.cli as cli; "
                "assert 'sqlite3' not in sys.modules; "
                "assert 'mipmem.core.fts_recall' not in sys.modules; "
                "assert 'mipmem.core.redactor' not in sys.modules; "
                "assert 'mipmem.core.migrate_v2' not in sys.modules; "
                "assert 'mipmem.core.subapps' not in sys.modules; "
                "assert 'mipmem.core.updater' not in sys.modules; "
                "assert 'mipmem.sync' not in sys.modules; "
                "assert 'urllib' not in sys.modules; "
                "assert 'mipmem.adapters.hook_io' not in sys.modules; "
                "assert 'mipmem.mcp.server' not in sys.modules; "
                "rc = cli.main(['health', '--json']); "
                "assert rc == 0; "
                "assert 'sqlite3' not in sys.modules; "
                "assert 'mipmem.core.fts_recall' not in sys.modules; "
                "assert 'mipmem.core.redactor' not in sys.modules; "
                "assert 'mipmem.core.migrate_v2' not in sys.modules; "
                "assert 'mipmem.core.subapps' not in sys.modules; "
                "assert 'mipmem.core.updater' not in sys.modules; "
                "assert 'mipmem.sync' not in sys.modules; "
                "assert 'urllib' not in sys.modules; "
                "assert 'mipmem.adapters.hook_io' not in sys.modules; "
                "assert 'mipmem.mcp.server' not in sys.modules"
            )
            env = {
                key: val
                for key, val in os.environ.items()
                if key not in ("PYTHONPATH", "MIPMEM_CONFIG_DIR", "MIPMEM_VAULT_DIR")
            }
            env["PYTHONPATH"] = str(REPO_ROOT / "src")
            env["MIPMEM_CONFIG_DIR"] = str(base / "cfg")
            env["MIPMEM_VAULT_DIR"] = str(base / "vault")
            env["MIPMEM_CACHE_DIR"] = str(base / "cache")

            proc = subprocess.run(
                [sys.executable, "-c", probe_code],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["success"])
            self.assertEqual(proc.stderr, "")

    def test_init_config_health_and_migrate_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_dir = base / "cfg"
            vault_dir = base / "vault"
            cache_dir = base / "cache"
            v2_src = base / "legacy_v2"
            (v2_src / "sessions" / "2026-11").mkdir(parents=True)
            (v2_src / "brainstorms").mkdir(parents=True)
            (v2_src / "sessions" / "2026-11" / "s1.md").write_text("# Session\n", encoding="utf-8")
            (v2_src / "brainstorms" / "bs1.md").write_text("# Idea\n", encoding="utf-8")

            env_map = {
                "MIPMEM_CONFIG_DIR": str(cfg_dir),
                "MIPMEM_VAULT_DIR": str(vault_dir),
                "MIPMEM_CACHE_DIR": str(cache_dir),
            }

            rc, out, err = _run_cli(["health", "--json"], env_map)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            health_before = json.loads(out)
            self.assertTrue(health_before["success"])
            self.assertFalse(health_before["vault_initialized"])

            rc, out, err = _run_cli(["init", "--host", "host-alpha", "--json"], env_map)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            init_data = json.loads(out)
            self.assertTrue(init_data["success"])
            self.assertEqual(init_data["host_id"], "host-alpha")

            rc, out, _ = _run_cli(["health", "--json"], env_map)
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(out)["vault_initialized"])

            # Safe type coercion for config set + get + show.
            updates = [
                (["config", "set", "sync.auto_sync_on_stop", "false", "--json"], False),
                (["config", "set", "budget.l0_max_tokens", "512", "--json"], 512),
                (["config", "set", "budget.topic_shift_novelty", "0.85", "--json"], 0.85),
                (
                    [
                        "config",
                        "set",
                        "workspace_policies.passive_paths",
                        "/tmp/p1,/tmp/p2",
                        "--json",
                    ],
                    ["/tmp/p1", "/tmp/p2"],
                ),
                (["config", "set", "workspace_policies.disabled_paths", "[]", "--json"], []),
            ]
            for argv, expected_val in updates:
                rc, out, err = _run_cli(argv, env_map)
                self.assertEqual(rc, 0, msg=out + err)
                self.assertEqual(err, "")
                set_res = json.loads(out)
                self.assertTrue(set_res["success"])
                self.assertEqual(set_res["value"], expected_val)

            rc, out, _ = _run_cli(["config", "get", "budget.l0_max_tokens", "--json"], env_map)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["value"], 512)

            with mock.patch.dict(os.environ, env_map, clear=False):
                loaded_cfg = load_config(cfg_dir / "config.toml")
            self.assertIs(loaded_cfg["sync"]["auto_sync_on_stop"], False)
            self.assertEqual(loaded_cfg["budget"]["l0_max_tokens"], 512)
            self.assertEqual(loaded_cfg["budget"]["topic_shift_novelty"], 0.85)
            self.assertEqual(
                loaded_cfg["workspace_policies"]["passive_paths"],
                ["/tmp/p1", "/tmp/p2"],
            )
            self.assertEqual(loaded_cfg["workspace_policies"]["disabled_paths"], [])

            # Migrate dry-run then apply.
            rc, out, _ = _run_cli(
                ["migrate", "--from-v2", str(v2_src), "--json"],
                env_map,
            )
            self.assertEqual(rc, 0)
            dry_payload = json.loads(out)
            self.assertTrue(dry_payload["report"]["dry_run"])
            self.assertEqual(dry_payload["report"]["sessions_copied"], 0)

            rc, out, _ = _run_cli(
                ["migrate", "--from-v2", str(v2_src), "--apply", "--json"],
                env_map,
            )
            self.assertEqual(rc, 0)
            apply_payload = json.loads(out)
            self.assertFalse(apply_payload["report"]["dry_run"])
            self.assertEqual(apply_payload["report"]["sessions_copied"], 1)
            self.assertEqual(apply_payload["report"]["brainstorms_copied"], 1)

            # Verify python -m mipmem entrypoint end-to-end.
            sub_env = {**os.environ, **env_map, "PYTHONPATH": str(REPO_ROOT / "src")}
            proc = subprocess.run(
                [sys.executable, "-m", "mipmem", "health", "--json"],
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
            self.assertTrue(json.loads(proc.stdout)["vault_initialized"])

    def test_error_envelopes_across_all_spec_error_codes_and_plain_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_dir = base / "cfg"
            vault_dir = base / "vault"
            cache_dir = base / "cache"
            env_map = {
                "MIPMEM_CONFIG_DIR": str(cfg_dir),
                "MIPMEM_VAULT_DIR": str(vault_dir),
                "MIPMEM_CACHE_DIR": str(cache_dir),
            }

            # 1. VAULT_NOT_INITIALIZED
            rc, out, err = _run_cli(["health", "--require-vault", "--json"], env_map)
            self.assertNotEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["error"]["code"], VAULT_NOT_INITIALIZED)

            # 2. CONFIG_INVALID_HOST_ID
            rc, out, err = _run_cli(["init", "--host", "Bad Host", "--json"], env_map)
            self.assertNotEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["error"]["code"], CONFIG_INVALID_HOST_ID)

            # 3. MIGRATE_SOURCE_MISSING
            rc, out, err = _run_cli(
                ["migrate", "--from-v2", str(base / "missing"), "--json"],
                env_map,
            )
            self.assertNotEqual(rc, 0)
            self.assertEqual(json.loads(out)["error"]["code"], MIGRATE_SOURCE_MISSING)

            # 4. CLI_INVALID_ARGUMENT
            for bad_args in (
                ["config", "set", "unknown.key", "1", "--json"],
                ["config", "set", "budget.l0_max_tokens", "not_int", "--json"],
                ["config", "set", "core.locale", "fr", "--json"],
                ["--json", "nonexistent-subcommand"],
            ):
                with self.subTest(args=bad_args):
                    rc, out, err = _run_cli(bad_args, env_map)
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(err, "")
                    parsed = json.loads(out)
                    self.assertFalse(parsed["success"])
                    self.assertEqual(parsed["error"]["code"], CLI_INVALID_ARGUMENT)

            # 5. CONFIG_INVALID_TOML
            cfg_dir.mkdir(parents=True, exist_ok=True)
            bad_cfg = cfg_dir / "config.toml"
            bad_cfg.write_text("[core\nbroken\n", encoding="utf-8")
            rc, out, err = _run_cli(["config", "show", "--json"], env_map)
            self.assertNotEqual(rc, 0)
            self.assertEqual(json.loads(out)["error"]["code"], CONFIG_INVALID_TOML)
            bad_cfg.unlink()

            # Plain text error mode writes to stderr and keeps stdout clean.
            rc, out, err = _run_cli(["health", "--require-vault"], env_map)
            self.assertNotEqual(rc, 0)
            self.assertEqual(out, "")
            self.assertIn(VAULT_NOT_INITIALIZED, err)

            # 6. VAULT_PATH_TRAVERSAL via symlink vault root
            outside = base / "outside"
            outside.mkdir()
            sym_vault = base / "sym_vault"
            try:
                sym_vault.symlink_to(outside, target_is_directory=True)
                has_symlink = True
            except OSError:
                has_symlink = False

            if has_symlink:
                rc, out, _ = _run_cli(
                    ["init", "--host", "host-alpha", "--vault", str(sym_vault), "--json"],
                    env_map,
                )
                self.assertNotEqual(rc, 0)
                self.assertEqual(json.loads(out)["error"]["code"], VAULT_PATH_TRAVERSAL)

    def test_s2_memory_cli_index_recall_bs_health_deep_and_error_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_dir = base / "cfg"
            vault_dir = base / "vault"
            cache_dir = base / "cache"
            env_map = {
                "MIPMEM_CONFIG_DIR": str(cfg_dir),
                "MIPMEM_VAULT_DIR": str(vault_dir),
                "MIPMEM_CACHE_DIR": str(cache_dir),
            }

            rc, out, err = _run_cli(["init", "--host", "host-alpha", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)

            note_file = base / "strategy.md"
            note_file.write_text(
                "# Chiến lược bộ nhớ đa máy\n\n"
                "Tài liệu thiết kế đồng bộ định tuyến.\n\n"
                "## Mục Một\n"
                "Nội dung mục một về bộ nhớ đa máy.\n\n"
                "## Mục Hai\n"
                "Chi tiết đồng bộ không xung đột.\n",
                encoding="utf-8",
            )

            # bs save from --file
            rc, out, err = _run_cli(
                [
                    "bs",
                    "save",
                    "Chiến lược bộ nhớ đa máy",
                    "--file",
                    str(note_file),
                    "--json",
                ],
                env_map,
            )
            self.assertEqual(rc, 0, msg=out + err)
            self.assertEqual(err, "")
            saved = json.loads(out)
            self.assertTrue(saved["success"])
            bs_id = saved["brainstorm"]["bs_id"]

            # index --json
            rc, out, err = _run_cli(["index", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)
            idx_data = json.loads(out)
            self.assertTrue(idx_data["success"])
            self.assertEqual(idx_data["report"]["indexed_brainstorms"], 1)

            # recall unaccented Vietnamese đ/Đ query
            rc, out, err = _run_cli(
                ["recall", "dong bo da may", "--locale", "vi", "--limit", "5", "--json"],
                env_map,
            )
            self.assertEqual(rc, 0, msg=out + err)
            rec_data = json.loads(out)
            self.assertTrue(rec_data["success"])
            self.assertGreaterEqual(len(rec_data["result"]["hits"]), 1)
            self.assertIn("đa máy", rec_data["result"]["hits"][0]["title"])

            # bs view --slice §1 skips H1 document title
            rc, out, err = _run_cli(
                ["bs", "view", bs_id, "--slice", "§1", "--json"],
                env_map,
            )
            self.assertEqual(rc, 0, msg=out + err)
            slice_data = json.loads(out)
            self.assertEqual(slice_data["slice"]["layer"], "L2")
            self.assertIn("## Mục Một", slice_data["slice"]["content"])
            self.assertNotIn("# Chiến lược", slice_data["slice"]["content"])

            # bs status cross-host CRDT mutation + bs list --status shipped
            rc, out, err = _run_cli(
                ["bs", "status", bs_id, "shipped", "--host", "host-beta", "--json"],
                env_map,
            )
            self.assertEqual(rc, 0, msg=out + err)
            self.assertTrue(json.loads(out)["success"])

            rc, out, err = _run_cli(["bs", "list", "--status", "shipped", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)
            listed = json.loads(out)
            self.assertEqual(len(listed["brainstorms"]), 1)
            self.assertEqual(listed["brainstorms"][0]["bs_id"], bs_id)
            self.assertEqual(listed["brainstorms"][0]["effective_status"], "shipped")

            # health --deep --json
            rc, out, err = _run_cli(["health", "--deep", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)
            deep = json.loads(out)
            self.assertTrue(deep["success"])
            self.assertTrue(deep["fts5_supported"])
            self.assertIn("unicode61", deep["fts5_tokenizer"])
            self.assertIsInstance(deep["fts5_query_ms"], (int, float))
            self.assertEqual(deep["indexed_brainstorms"], 1)
            self.assertEqual(deep["indexed_sessions"], 0)

            # Spec S2 CLI error envelopes
            rc, out, err = _run_cli(["bs", "view", "missing-bs-id", "--json"], env_map)
            self.assertNotEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["error"]["code"], BRAINSTORM_NOT_FOUND)

            rc, out, err = _run_cli(
                ["bs", "view", bs_id, "--slice", "§99", "--json"],
                env_map,
            )
            self.assertNotEqual(rc, 0)
            self.assertEqual(json.loads(out)["error"]["code"], INVALID_SLICE_SELECTOR)

            rc, out, err = _run_cli(
                ["bs", "status", bs_id, "invalid-status", "--json"],
                env_map,
            )
            self.assertNotEqual(rc, 0)
            self.assertEqual(json.loads(out)["error"]["code"], INVALID_BRAINSTORM_STATUS)

    def test_fast_hook_dispatch_cli_subcommands_and_zero_sqlite_on_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_dir = base / "cfg"
            vault_dir = base / "vault"
            cache_dir = base / "cache"
            ws_dir = base / "ws"
            ws_dir.mkdir(parents=True)

            env_map = {
                "MIPMEM_CONFIG_DIR": str(cfg_dir),
                "MIPMEM_VAULT_DIR": str(vault_dir),
                "MIPMEM_CACHE_DIR": str(cache_dir),
            }
            rc, out, err = _run_cli(["init", "--host", "host-hook", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)

            sub_env = {
                key: val
                for key, val in os.environ.items()
                if key not in ("PYTHONPATH", "MIPMEM_CONFIG_DIR", "MIPMEM_VAULT_DIR")
            }
            sub_env["PYTHONPATH"] = str(REPO_ROOT / "src")
            sub_env.update(env_map)

            # 1. Fast hook skip path (invocationNum=2) bypasses cli_commands, argparse & sqlite3
            skip_probe = (
                "import sys; import mipmem.cli as cli; "
                "rc = cli.main(['hook', 'pre-invocation']); "
                "assert rc == 0; "
                "assert 'mipmem.cli_commands' not in sys.modules; "
                "assert 'argparse' not in sys.modules; "
                "assert 'sqlite3' not in sys.modules"
            )
            proc_skip = subprocess.run(
                [sys.executable, "-c", skip_probe],
                input=json.dumps({"conversationId": "c1", "invocationNum": 2}),
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_skip.returncode, 0, msg=proc_skip.stderr + proc_skip.stdout)
            self.assertEqual(json.loads(proc_skip.stdout), {})

            # 2. PreInvocation /mem status via cli.main(['hook', 'pre-invocation'])
            slash_payload = json.dumps(
                {
                    "conversationId": "c1",
                    "invocationNum": 1,
                    "lastUserInput": "/mem status",
                    "workspacePaths": [str(ws_dir)],
                    "transcriptPath": "/tmp/sb/.gemini/antigravity-cli/brain/c1/t.jsonl",
                }
            )
            proc_slash = subprocess.run(
                [sys.executable, "-m", "mipmem", "hook", "pre-invocation"],
                input=slash_payload,
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_slash.returncode, 0, msg=proc_slash.stderr + proc_slash.stdout)
            slash_data = json.loads(proc_slash.stdout)
            self.assertEqual(slash_data["terminationBehavior"], "terminate")
            self.assertIn(
                "host-hook",
                slash_data["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"],
            )

            # 3. Stop hook with TERMINAL_CUSTOM_HOOK returns {} cleanly
            proc_stop = subprocess.run(
                [sys.executable, "-m", "mipmem", "hook", "stop"],
                input=json.dumps(
                    {
                        "conversationId": "c1",
                        "terminationReason": "TERMINAL_CUSTOM_HOOK",
                        "workspacePaths": [str(ws_dir)],
                    }
                ),
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_stop.returncode, 0, msg=proc_stop.stderr + proc_stop.stdout)
            self.assertEqual(json.loads(proc_stop.stdout), {})

    def test_cli_mcp_subcommand_e2e_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            env_map = {
                "MIPMEM_CONFIG_DIR": str(base / "cfg"),
                "MIPMEM_VAULT_DIR": str(base / "vault"),
                "MIPMEM_CACHE_DIR": str(base / "cache"),
            }
            rc, out, err = _run_cli(["init", "--host", "host-mcp", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)

            sub_env = {
                key: val
                for key, val in os.environ.items()
                if key not in ("PYTHONPATH", "MIPMEM_CONFIG_DIR", "MIPMEM_VAULT_DIR")
            }
            sub_env["PYTHONPATH"] = str(REPO_ROOT / "src")
            sub_env.update(env_map)

            ndjson_in = "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {"name": "mem_fleet_status", "arguments": {}},
                        }
                    ),
                ]
            ) + "\n"
            proc = subprocess.run(
                [sys.executable, "-m", "mipmem", "mcp"],
                input=ndjson_in,
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
            self.assertEqual(proc.stderr, "")
            lines = proc.stdout.strip().splitlines()
            self.assertEqual(len(lines), 3)
            r1, r2, r3 = (json.loads(line) for line in lines)
            self.assertEqual(r1["result"]["serverInfo"]["name"], "mipmem")
            self.assertEqual(len(r2["result"]["tools"]), 6)
            self.assertEqual(r3["result"]["structuredContent"]["subapp_telemetry"], [])

    def test_s4_fleet_sync_self_update_and_health_deep_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_dir = base / "cfg"
            vault_dir = base / "vault"
            cache_dir = base / "cache"
            sync_dir = base / "shared_sync"
            sync_dir.mkdir(parents=True)

            env_map = {
                "MIPMEM_CONFIG_DIR": str(cfg_dir),
                "MIPMEM_VAULT_DIR": str(vault_dir),
                "MIPMEM_CACHE_DIR": str(cache_dir),
            }
            rc, out, err = _run_cli(["init", "--host", "host-cli", "--json"], env_map)
            self.assertEqual(rc, 0, msg=out + err)

            tel_dir = vault_dir / "hosts" / "host-cli" / "telemetry"
            tel_dir.mkdir(parents=True, exist_ok=True)
            (tel_dir / "docker.status.json").write_text(
                json.dumps(
                    {
                        "app_id": "docker",
                        "status": "ok",
                        "badge": "2 up",
                        "l0_summary": "containers running",
                        "alert_on_l0": False,
                        "detail_file": "telemetry/docker.detail.md",
                        "updated_at": "2026-11-23T10:00:00.000Z",
                    }
                ),
                encoding="utf-8",
            )

            sub_env = {
                key: val
                for key, val in os.environ.items()
                if key not in ("PYTHONPATH", "MIPMEM_CONFIG_DIR", "MIPMEM_VAULT_DIR")
            }
            sub_env["PYTHONPATH"] = str(REPO_ROOT / "src")
            sub_env.update(env_map)

            # 1. mipmem fleet --json
            proc_fleet = subprocess.run(
                [sys.executable, "-m", "mipmem", "fleet", "--json"],
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_fleet.returncode, 0, msg=proc_fleet.stderr + proc_fleet.stdout)
            fleet_data = json.loads(proc_fleet.stdout)
            self.assertTrue(fleet_data["success"])
            self.assertEqual(fleet_data["fleet"]["active_host_id"], "host-cli")
            self.assertEqual(len(fleet_data["fleet"]["subapp_telemetry"]), 1)
            self.assertIn("sync_status", fleet_data["fleet"])

            # 2. mipmem sync --json fails gracefully when unconfigured
            rc_unconf, out_unconf, err_unconf = _run_cli(["sync", "--json"], env_map)
            self.assertNotEqual(rc_unconf, 0)
            self.assertEqual(err_unconf, "")
            self.assertEqual(json.loads(out_unconf)["error"]["code"], SYNC_BACKEND_UNCONFIGURED)

            # Configure folder backend and test sync + --detached + mutual exclusion
            for key, val in (
                ("sync.backend", "folder"),
                ("sync.folder_path", str(sync_dir)),
            ):
                rc_cfg, out_cfg, err_cfg = _run_cli(["config", "set", key, val, "--json"], env_map)
                self.assertEqual(rc_cfg, 0, msg=out_cfg + err_cfg)

            proc_sync = subprocess.run(
                [sys.executable, "-m", "mipmem", "sync", "--json"],
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_sync.returncode, 0, msg=proc_sync.stderr + proc_sync.stdout)
            sync_data = json.loads(proc_sync.stdout)
            self.assertTrue(sync_data["success"])
            self.assertEqual(sync_data["result"]["backend"], "folder")

            proc_det = subprocess.run(
                [sys.executable, "-m", "mipmem", "sync", "--detached"],
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_det.returncode, 0, msg=proc_det.stderr + proc_det.stdout)
            self.assertEqual(proc_det.stdout, "")

            rc_both, out_both, _ = _run_cli(
                ["sync", "--pull-only", "--push-only", "--json"],
                env_map,
            )
            self.assertNotEqual(rc_both, 0)
            self.assertEqual(json.loads(out_both)["error"]["code"], CLI_INVALID_ARGUMENT)

            # 3. health --deep --json includes S4 sync & subapp fields
            rc_deep, out_deep, err_deep = _run_cli(["health", "--deep", "--json"], env_map)
            self.assertEqual(rc_deep, 0, msg=out_deep + err_deep)
            deep_data = json.loads(out_deep)
            self.assertEqual(deep_data["sync_backend"], "folder")
            self.assertEqual(deep_data["last_sync_status"]["backend"], "folder")
            self.assertEqual(deep_data["subapp_delegates_count"], 0)

            # 4. self-update --help & offline mocked --check --json
            proc_help = subprocess.run(
                [sys.executable, "-m", "mipmem", "self-update", "--help"],
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_help.returncode, 0, msg=proc_help.stderr)
            self.assertIn("--check", proc_help.stdout)

            mock_res = UpdateResult(
                install_mode="zipapp",
                target_path="/tmp/mipmem.pyz",
                current_version="0.3.0",
                target_version="1.0.0",
                update_available=True,
                check_only=True,
                recommended_command="mipmem self-update",
                message="Update available: 0.3.0 -> 1.0.0",
            )
            with mock.patch("mipmem.core.updater.run_self_update", return_value=mock_res) as m_upd:
                rc_upd, out_upd, err_upd = _run_cli(
                    ["self-update", "--check", "--json"],
                    env_map,
                )
            self.assertEqual(rc_upd, 0, msg=out_upd + err_upd)
            self.assertEqual(err_upd, "")
            self.assertEqual(m_upd.call_count, 1)
            upd_data = json.loads(out_upd)
            self.assertTrue(upd_data["success"])
            self.assertEqual(upd_data["result"]["target_version"], "1.0.0")
            self.assertTrue(upd_data["result"]["update_available"])

    def test_sync_config_override_if_configured_and_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home_dir = root / "home"
            default_cfg_dir = root / "default-cfg"
            default_vault = root / "default-vault"
            default_cache = root / "default-cache"
            custom_vault = root / "custom-vault"
            sync_dir = root / "shared-sync"
            custom_cfg = root / "outside" / "custom-sync.toml"

            home_dir.mkdir(parents=True)
            default_cfg_dir.mkdir(parents=True)
            default_cache.mkdir(parents=True)
            custom_cfg.parent.mkdir(parents=True)
            sync_dir.mkdir(parents=True)
            init_vault(default_vault, host_id="host-default")
            init_vault(custom_vault, host_id="host-custom")
            (default_cfg_dir / "config.toml").write_text(
                f'[core]\nhost_id = "host-default"\nvault_path = "{default_vault}"\nlocale = "en"\n'
                '[sync]\nbackend = "none"\n',
                encoding="utf-8",
            )

            env_clean = {
                key: val
                for key, val in os.environ.items()
                if not key.startswith("XDG_") and key != "MIPMEM_VAULT_DIR"
            }
            env_clean.update(
                {
                    "HOME": str(home_dir),
                    "MIPMEM_CONFIG_DIR": str(default_cfg_dir),
                    "MIPMEM_CACHE_DIR": str(default_cache),
                }
            )

            with mock.patch.dict(os.environ, env_clean, clear=True):
                # 1. --if-configured exits 0 cleanly when [sync].backend == "none"
                rc_skip, out_skip, err_skip = _run_cli(
                    ["sync", "--if-configured", "--json"],
                    {},
                )
                self.assertEqual(rc_skip, 0, msg=out_skip + err_skip)
                self.assertEqual(err_skip, "")
                skip_data = json.loads(out_skip)
                self.assertTrue(skip_data["success"])
                self.assertTrue(skip_data["skipped"])
                self.assertEqual(skip_data["reason"], "sync_unconfigured")

                # 2. Missing --config file fails explicitly with CLI_INVALID_ARGUMENT
                missing_cfg = root / "outside" / "missing.toml"
                rc_miss, out_miss, _ = _run_cli(
                    ["sync", "--config", str(missing_cfg), "--json"],
                    {},
                )
                self.assertNotEqual(rc_miss, 0)
                self.assertEqual(json.loads(out_miss)["error"]["code"], CLI_INVALID_ARGUMENT)

                # 3. Valid custom --config resolves custom vault_path, host_id, and sync backend
                session_file = (
                    custom_vault / "hosts" / "host-custom" / "sessions" / "2026-11" / "conv-c1.md"
                )
                session_file.parent.mkdir(parents=True, exist_ok=True)
                session_file.write_text("# Custom session\n", encoding="utf-8")
                custom_cfg.write_text(
                    f'[core]\nhost_id = "host-custom"\n'
                    f'vault_path = "{custom_vault}"\n'
                    f'locale = "vi"\n'
                    f'[sync]\nbackend = "folder"\n'
                    f'folder_path = "{sync_dir}"\n',
                    encoding="utf-8",
                )
                rc_cust, out_cust, err_cust = _run_cli(
                    ["sync", "--config", str(custom_cfg), "--json"],
                    {},
                )
                self.assertEqual(rc_cust, 0, msg=out_cust + err_cust)
                self.assertEqual(err_cust, "")
                cust_data = json.loads(out_cust)
                self.assertTrue(cust_data["success"])
                self.assertEqual(cust_data["result"]["host_id"], "host-custom")
                self.assertTrue(
                    (
                        sync_dir
                        / "hosts"
                        / "host-custom"
                        / "sessions"
                        / "2026-11"
                        / "conv-c1.md"
                    ).is_file()
                )
                cached = read_sync_status(default_cache)
                assert cached is not None
                self.assertEqual(cached["host_id"], "host-custom")

    def test_detached_cli_errors_log_cp_syn_01_to_mipmem_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg_dir = root / "cfg"
            vault_dir = root / "vault"
            cache_dir = root / "cache"
            sync_dir = root / "sync"
            cfg_dir.mkdir(parents=True)
            cache_dir.mkdir(parents=True)
            sync_dir.mkdir(parents=True)
            init_vault(vault_dir, host_id="host-det")
            (cfg_dir / "config.toml").write_text(
                f'[core]\nhost_id = "host-det"\nvault_path = "{vault_dir}"\n'
                f'[sync]\nbackend = "folder"\nfolder_path = "{sync_dir}"\n',
                encoding="utf-8",
            )

            sub_env = {
                key: val
                for key, val in os.environ.items()
                if key not in ("PYTHONPATH", "MIPMEM_VAULT_DIR") and not key.startswith("XDG_")
            }
            sub_env.update(
                {
                    "PYTHONPATH": str(REPO_ROOT / "src"),
                    "HOME": str(root / "home"),
                    "MIPMEM_CONFIG_DIR": str(cfg_dir),
                    "MIPMEM_CACHE_DIR": str(cache_dir),
                }
            )

            # 1. Argparse error in --detached subprocess MUST log CP-SYN-01 to mipmem.log
            proc = subprocess.run(
                [sys.executable, "-m", "mipmem", "sync", "--detached", "--unknown-flag"],
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            log_path = cache_dir / "mipmem.log"
            self.assertTrue(log_path.is_file(), msg="mipmem.log was not created for detached error")
            raw_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            entries = [json.loads(line) for line in raw_lines]
            self.assertTrue(
                any(
                    e["event"] == CP_SYN_01
                    and e["level"] == "ERROR"
                    and e["error_code"] == CLI_INVALID_ARGUMENT
                    and e.get("details", {}).get("direction") == "detached_cli"
                    and e.get("details", {}).get("ok") is False
                    for e in entries
                ),
                msg=f"Missing detached_cli error record: {entries}",
            )

            # 2. Unexpected RuntimeError during --detached logs redacted event & returns 1
            fake_secret = "ghp_" + "R" * 24
            with (
                mock.patch.dict(os.environ, sub_env, clear=True),
                mock.patch(
                    "mipmem.sync.base.resolve_sync_backend",
                    side_effect=RuntimeError(f"unexpected boom token={fake_secret}"),
                ),
            ):
                rc_unexpected = dispatch_full_cli(["sync", "--detached"], time.perf_counter())
            self.assertEqual(rc_unexpected, 1)
            raw_log = log_path.read_text(encoding="utf-8")
            self.assertNotIn(fake_secret, raw_log)
            entries_after = [json.loads(line) for line in raw_log.strip().splitlines()]
            self.assertTrue(
                any(
                    e["event"] == CP_SYN_01
                    and e.get("details", {}).get("exception") == "RuntimeError"
                    and e.get("details", {}).get("ok") is False
                    for e in entries_after
                )
            )

    def test_cli_init_refresh_guide_and_join_e2e_with_guards(self) -> None:
        from mipmem.cli_commands import _build_parser
        from mipmem.core.sessions import SessionRecord, write_session_record
        from mipmem.sync.git_backend import GitSyncBackend

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sub_env = {
                "PATH": os.environ.get("PATH", ""),
                "XDG_CONFIG_HOME": str(root / "xdg-cfg"),
                "XDG_DATA_HOME": str(root / "xdg-data"),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
            }
            vault_a = root / "vault-alpha"
            bare_repo = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True,
                           capture_output=True, text=True)

            # 1. init & explicit --refresh-guide
            with mock.patch.dict(os.environ, sub_env, clear=True):
                rc_init = dispatch_full_cli(
                    ["init", "--vault", str(vault_a), "--host", "host-alpha", "--json"],
                    time.perf_counter(),
                )
                self.assertEqual(rc_init, 0)
                (vault_a / "AGENTS.md").write_text("# Edited\n", encoding="utf-8")

                buf_ref = io.StringIO()
                with contextlib.redirect_stdout(buf_ref):
                    rc_ref = dispatch_full_cli(
                        [
                            "init",
                            "--vault",
                            str(vault_a),
                            "--host",
                            "host-alpha",
                            "--refresh-guide",
                            "--json",
                        ],
                        time.perf_counter(),
                    )
            self.assertEqual(rc_ref, 0)
            ref_payload = json.loads(buf_ref.getvalue())
            self.assertEqual(ref_payload["guides_written"], ["AGENTS.md", "README.md"])
            self.assertIn("Safety Guard", (vault_a / "AGENTS.md").read_text(encoding="utf-8"))

            write_session_record(
                vault_a,
                SessionRecord(
                    conversation_id="conv-cli-join-01",
                    host_id="host-alpha",
                    surface="cli",
                    title="CLI onboarding check",
                    last_modified_time="2026-11-26T06:10:00Z",
                    recent_prompts=["Join machine via CLI"],
                ),
            )
            self.assertTrue(
                GitSyncBackend(
                    vault_dir=vault_a,
                    cache_dir=root / "cache-a",
                    remote_url=str(bare_repo),
                    host_id="host-alpha",
                ).sync().ok
            )

            # 2. MP7 Parser contract for join
            parsed = _build_parser().parse_args(
                ["join", str(bare_repo), "--host", "machine-b", "--locale", "vi", "--json"]
            )
            self.assertEqual(parsed.command, "join")
            self.assertEqual(parsed.host, "machine-b")
            self.assertEqual(parsed.locale, "vi")

            # 3. E2E CLI join success
            vault_b = root / "vault-beta"
            cfg_b = root / "cfg-beta" / "config.toml"
            buf_join = io.StringIO()
            with (
                mock.patch.dict(os.environ, sub_env, clear=True),
                contextlib.redirect_stdout(buf_join),
            ):
                rc_join = dispatch_full_cli(
                    [
                        "join",
                        str(bare_repo),
                        "--host",
                        "machine-b",
                        "--locale",
                        "vi",
                        "--vault",
                        str(vault_b),
                        "--config",
                        str(cfg_b),
                        "--json",
                    ],
                    time.perf_counter(),
                )
            self.assertEqual(rc_join, 0)
            join_payload = json.loads(buf_join.getvalue())
            self.assertTrue(join_payload["success"])
            self.assertEqual(join_payload["host_id"], "machine-b")
            self.assertEqual(join_payload["existing_hosts"], ["host-alpha"])
            self.assertIn("indexed_sessions", join_payload)
            self.assertIn("indexed_brainstorms", join_payload)
            self.assertIn("sync_result", join_payload)
            self.assertIn("guides_written", join_payload)

            # 4. Duplicate host blocked -> JOIN_HOST_CONFLICT JSON envelope
            buf_dup = io.StringIO()
            with (
                mock.patch.dict(os.environ, sub_env, clear=True),
                contextlib.redirect_stdout(buf_dup),
            ):
                rc_dup = dispatch_full_cli(
                    [
                        "join",
                        str(bare_repo),
                        "--host",
                        "host-alpha",
                        "--vault",
                        str(root / "vault-dup"),
                        "--config",
                        str(root / "cfg-dup" / "config.toml"),
                        "--json",
                    ],
                    time.perf_counter(),
                )
            self.assertEqual(rc_dup, 1)
            self.assertEqual(json.loads(buf_dup.getvalue())["error"]["code"], JOIN_HOST_CONFLICT)

            # 5. Symlink --vault blocked -> VAULT_PATH_TRAVERSAL
            sym_vault = root / "sym-vault"
            sym_vault.symlink_to(vault_b)
            buf_sym = io.StringIO()
            with (
                mock.patch.dict(os.environ, sub_env, clear=True),
                contextlib.redirect_stdout(buf_sym),
            ):
                rc_sym = dispatch_full_cli(
                    ["join", str(bare_repo), "--vault", str(sym_vault), "--json"],
                    time.perf_counter(),
                )
            self.assertEqual(rc_sym, 1)
            self.assertEqual(
                json.loads(buf_sym.getvalue())["error"]["code"],
                VAULT_PATH_TRAVERSAL,
            )

    def test_cli_init_preserves_existing_host_id_unless_overridden(self) -> None:
        from mipmem.config import load_config
        from mipmem.core.vault import default_host_id

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg_dir = root / "cfg"
            vault = root / "vault"
            cache_dir = root / "cache"
            sub_env = {
                "PATH": os.environ.get("PATH", ""),
                "MIPMEM_CONFIG_DIR": str(cfg_dir),
                "MIPMEM_VAULT_DIR": str(vault),
                "MIPMEM_CACHE_DIR": str(cache_dir),
            }
            cfg_file = cfg_dir / "config.toml"

            with mock.patch.dict(os.environ, sub_env, clear=True):
                buf_init = io.StringIO()
                with contextlib.redirect_stdout(buf_init):
                    rc_init = dispatch_full_cli(
                        ["init", "--vault", str(vault), "--host", "host-custom", "--json"],
                        time.perf_counter(),
                    )
                self.assertEqual(rc_init, 0)
                self.assertEqual(json.loads(buf_init.getvalue())["host_id"], "host-custom")

                # 1. init --refresh-guide without --host preserves host-custom
                (vault / "AGENTS.md").write_text("# Old Guide\n", encoding="utf-8")
                buf_ref = io.StringIO()
                with contextlib.redirect_stdout(buf_ref):
                    rc_ref = dispatch_full_cli(
                        ["init", "--refresh-guide", "--json"],
                        time.perf_counter(),
                    )
                self.assertEqual(rc_ref, 0)
                ref_payload = json.loads(buf_ref.getvalue())
                self.assertEqual(ref_payload["host_id"], "host-custom")
                self.assertEqual(ref_payload["guides_written"], ["AGENTS.md", "README.md"])
                self.assertEqual(load_config(cfg_file)["core"]["host_id"], "host-custom")
                self.assertEqual(
                    sorted(p.name for p in (vault / "hosts").iterdir() if p.is_dir()),
                    ["host-custom"],
                )

                # 2. plain init without --host preserves host-custom
                buf_plain = io.StringIO()
                with contextlib.redirect_stdout(buf_plain):
                    rc_plain = dispatch_full_cli(["init", "--json"], time.perf_counter())
                self.assertEqual(rc_plain, 0)
                self.assertEqual(json.loads(buf_plain.getvalue())["host_id"], "host-custom")
                self.assertEqual(load_config(cfg_file)["core"]["host_id"], "host-custom")
                self.assertEqual(
                    sorted(p.name for p in (vault / "hosts").iterdir() if p.is_dir()),
                    ["host-custom"],
                )

                # 3. explicit --host overrides configured core.host_id
                buf_override = io.StringIO()
                with contextlib.redirect_stdout(buf_override):
                    rc_override = dispatch_full_cli(
                        ["init", "--host", "host-override", "--json"],
                        time.perf_counter(),
                    )
                self.assertEqual(rc_override, 0)
                self.assertEqual(json.loads(buf_override.getvalue())["host_id"], "host-override")
                self.assertEqual(load_config(cfg_file)["core"]["host_id"], "host-override")
                self.assertEqual(
                    sorted(p.name for p in (vault / "hosts").iterdir() if p.is_dir()),
                    ["host-custom", "host-override"],
                )

            # 4. fresh environment without config.toml falls back to default_host_id()
            fresh_env = {
                "PATH": os.environ.get("PATH", ""),
                "MIPMEM_CONFIG_DIR": str(root / "fresh-cfg"),
                "MIPMEM_VAULT_DIR": str(root / "fresh-vault"),
                "MIPMEM_CACHE_DIR": str(root / "fresh-cache"),
            }
            with mock.patch.dict(os.environ, fresh_env, clear=True):
                buf_fresh = io.StringIO()
                with contextlib.redirect_stdout(buf_fresh):
                    rc_fresh = dispatch_full_cli(["init", "--json"], time.perf_counter())
                self.assertEqual(rc_fresh, 0)
                fresh_payload = json.loads(buf_fresh.getvalue())
                self.assertEqual(fresh_payload["host_id"], default_host_id())
                self.assertNotEqual(fresh_payload["host_id"], "workstation-alpha")


if __name__ == "__main__":
    unittest.main()
