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
import unittest
from unittest import mock

from mipmem.cli import main as cli_main
from mipmem.config import load_config
from mipmem.core.diagnostics import (
    BRAINSTORM_NOT_FOUND,
    CLI_INVALID_ARGUMENT,
    CONFIG_INVALID_HOST_ID,
    CONFIG_INVALID_TOML,
    INVALID_BRAINSTORM_STATUS,
    INVALID_SLICE_SELECTOR,
    MIGRATE_SOURCE_MISSING,
    VAULT_NOT_INITIALIZED,
    VAULT_PATH_TRAVERSAL,
    register_default_scrubber,
)

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
                "assert 'mipmem.adapters.hook_io' not in sys.modules; "
                "assert 'mipmem.mcp.server' not in sys.modules; "
                "rc = cli.main(['health', '--json']); "
                "assert rc == 0; "
                "assert 'sqlite3' not in sys.modules; "
                "assert 'mipmem.core.fts_recall' not in sys.modules; "
                "assert 'mipmem.core.redactor' not in sys.modules; "
                "assert 'mipmem.core.migrate_v2' not in sys.modules; "
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


if __name__ == "__main__":
    unittest.main()
