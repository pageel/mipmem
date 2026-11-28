"""Unit and integration tests for MipMem Hook IO engine and /mem dispatcher."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from mipmem.config import default_config, save_config
from mipmem.core.brainstorms import MAX_BRAINSTORM_BYTES
from mipmem.core.diagnostics import (
    CP_HK_01,
    HOOK_INVALID_PAYLOAD,
    SLASH_COMMAND_INVALID,
    RotatingJsonLogger,
)
from mipmem.core.fts_recall import sync_fts_index
from mipmem.core.sessions import SessionRecord, write_session_record
from mipmem.core.vault import atomic_write_vault_file, init_vault


class TestHookUnifiedRendererAndFailOpen(unittest.TestCase):
    """Verify terminated hook errorMessage response, CLI ANSI styling, and fail-open wrapper."""

    def test_render_user_markdown_response_exact_schema_for_cli_and_ide(self) -> None:
        from mipmem.adapters.hook_io import (
            render_system_injection,
            render_user_markdown_response,
        )

        sample_md = "| Key | Value |\n| --- | --- |\n| ok | 1 |"
        # Default IDE/generic response uses errorMessage.userMessage (no metadata map panic).
        rendered = render_user_markdown_response(sample_md)
        self.assertEqual(
            rendered,
            {
                "injectSteps": [
                    {
                        "errorMessage": {
                            "userMessage": sample_md,
                        }
                    }
                ],
                "terminationBehavior": "terminate",
            },
        )
        ide_msg = rendered["injectSteps"][0]["errorMessage"]["userMessage"]
        self.assertNotIn("\033[", ide_msg)

        # CLI surface wraps lines with ANSI reset/colors while preserving text content.
        cli_rendered = render_user_markdown_response(
            "### Header\n- ✅ ready\n| Key | Value |",
            surface="cli",
        )
        self.assertEqual(cli_rendered.get("terminationBehavior"), "terminate")
        cli_msg = cli_rendered["injectSteps"][0]["errorMessage"]["userMessage"]
        self.assertIn("\033[0m", cli_msg)
        self.assertIn("Header", cli_msg)
        self.assertIn("Key", cli_msg)

        # Opt-in flag and MIPMEM_HOOK_USE_USER_MARKDOWN=1 restore legacy metadata output.
        legacy_flag = render_user_markdown_response(sample_md, use_user_markdown=True)
        self.assertEqual(
            legacy_flag,
            {
                "injectSteps": [
                    {
                        "systemMessage": {
                            "systemMessage": "",
                            "metadata": {"user_markdown": sample_md},
                        }
                    }
                ],
                "terminationBehavior": "terminate",
            },
        )
        with mock.patch.dict("os.environ", {"MIPMEM_HOOK_USE_USER_MARKDOWN": "1"}, clear=False):
            legacy_env = render_user_markdown_response(sample_md)
        self.assertEqual(
            legacy_env["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"],
            sample_md,
        )

        self.assertEqual(
            render_system_injection("# MipMem L0 Radar\n"),
            {"injectSteps": [{"systemMessage": {"systemMessage": "# MipMem L0 Radar\n"}}]},
        )
        self.assertEqual(render_system_injection("   \n"), {})

    def test_run_hook_safe_fail_open_on_invalid_json_oversize_and_exception(self) -> None:
        from mipmem.adapters.hook_io import MAX_HOOK_STDIN_BYTES, run_hook_safe

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_path)

            # 1. Invalid JSON payload -> {} and exit 0, WARN HOOK_INVALID_PAYLOAD logged
            out_invalid = io.StringIO()
            rc_invalid = run_hook_safe(
                "pre-invocation",
                stdin_stream=io.BytesIO(b"{invalid-json"),
                stdout_stream=out_invalid,
                logger=logger,
            )
            self.assertEqual(rc_invalid, 0)
            self.assertEqual(json.loads(out_invalid.getvalue()), {})

            # 2. Oversize payload (> 4 MB) -> {} and exit 0
            out_big = io.StringIO()
            big_bytes = b'{"x":"' + (b"a" * (MAX_HOOK_STDIN_BYTES + 16)) + b'"}'
            rc_big = run_hook_safe(
                "pre-invocation",
                stdin_stream=io.BytesIO(big_bytes),
                stdout_stream=out_big,
                logger=logger,
            )
            self.assertEqual(rc_big, 0)
            self.assertEqual(json.loads(out_big.getvalue()), {})

            # 3. Unexpected exception inside handler -> {} and exit 0, secret redacted in log
            def _boom(_payload: dict[str, object]) -> dict[str, object]:
                raise RuntimeError("crash with secret ghp_" + "A" * 24)

            out_err = io.StringIO()
            rc_err = run_hook_safe(
                "session-start",
                stdin_stream=io.BytesIO(b'{"conversationId":"c1"}'),
                stdout_stream=out_err,
                handler=_boom,
                logger=logger,
            )
            self.assertEqual(rc_err, 0)
            self.assertEqual(json.loads(out_err.getvalue()), {})

            raw_log = log_path.read_text(encoding="utf-8")
            self.assertNotIn("ghp_" + "A" * 24, raw_log)
            entries = [json.loads(line) for line in raw_log.strip().splitlines()]
            invalid_codes = [e["error_code"] for e in entries if e["event"] == CP_HK_01]
            self.assertGreaterEqual(invalid_codes.count(HOOK_INVALID_PAYLOAD), 2)


class TestSlashMemDispatcher(unittest.TestCase):
    """Verify local shell-free /mem dispatcher and file safety guards (S3 §1.7)."""

    def _setup_vault(self, root: Path) -> tuple[Path, Path, RotatingJsonLogger]:
        vault_dir = root / "vault"
        cfg_file = root / "config" / "config.toml"
        log_path = root / "mipmem.log"
        init_vault(vault_dir, host_id="host-alpha")
        cfg = default_config()
        cfg["core"]["host_id"] = "host-alpha"
        cfg["core"]["vault_path"] = str(vault_dir)
        save_config(cfg, cfg_file)
        return vault_dir, cfg_file, RotatingJsonLogger(log_path=log_path)

    def test_slash_mem_help_status_list_recall_and_handoff(self) -> None:
        from mipmem.adapters.slash_mem import dispatch_slash_mem

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir, cfg_file, logger = self._setup_vault(root)
            long_title = (
                "Architecting Universal Local MCP Server and Dual CLI IDE Transcript Adapters"
            )
            write_session_record(
                vault_dir,
                SessionRecord(
                    conversation_id="conv-long-01",
                    host_id="host-alpha",
                    surface="ide",
                    last_modified_time="2026-11-22T08:00:00.000Z",
                    step_count=15,
                    title=long_title,
                    edited_files=["src/mipmem/mcp/server.py", "src/mipmem/adapters/hook_io.py"],
                    recent_prompts=["Implement FTS5 recall and universal MCP stdio"],
                    last_agent_response="Completed dual adapter and hook engine.",
                ),
            )
            atomic_write_vault_file(
                vault_dir,
                "hosts/host-alpha/latest_handoff.md",
                "# Latest Handoff\nPrimary work: FTS5 recall engine ready.\n",
                actor_host="host-alpha",
            )
            sync_fts_index(vault_dir)

            help_md = dispatch_slash_mem(
                "/mem help", vault_root=vault_dir, config_file=cfg_file, logger=logger
            )
            self.assertIn("/mem recall", help_md)
            self.assertIn("/mem bs view", help_md)

            status_md = dispatch_slash_mem(
                "/mem status", vault_root=vault_dir, config_file=cfg_file, logger=logger
            )
            self.assertIn("host-alpha", status_md)
            self.assertIn("initialized", status_md.lower())

            recall_md = dispatch_slash_mem(
                '/mem recall "FTS5" --host host-alpha --limit 3',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("conv-long-01", recall_md)

            cli_list = dispatch_slash_mem(
                "/mem list 1-5",
                surface="cli",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            ide_list = dispatch_slash_mem(
                "/mem list 1-5",
                surface="ide",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("conv-long-01", cli_list)
            self.assertIn("conv-long-01", ide_list)
            self.assertLess(len(cli_list), len(ide_list))

            handoff_md = dispatch_slash_mem(
                "/mem handoff @host-alpha",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("Primary work: FTS5 recall engine ready.", handoff_md)

            bad_handoff = dispatch_slash_mem(
                "/mem handoff @../etc",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("Invalid", bad_handoff)

    def test_slash_mem_bs_save_view_status_and_file_safety(self) -> None:
        from mipmem.adapters.slash_mem import dispatch_slash_mem

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir, cfg_file, logger = self._setup_vault(root)
            artifact_file = root / "notes.md"
            secret_token = "ghp_" + "B" * 24
            artifact_file.write_text(
                "# Architecture Notes\n\n## Core Design\n"
                f"Decision: use stdlib only. Token: {secret_token}\n\n"
                "## Rollout\nShip v0.3.0 plugin.\n",
                encoding="utf-8",
            )

            save_md = dispatch_slash_mem(
                f'/mem bs save "Architecture Notes" --file "{artifact_file}" --tags arch,v3',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("Saved brainstorm", save_md)
            self.assertNotIn(secret_token, save_md)

            list_md = dispatch_slash_mem(
                "/mem bs list", vault_root=vault_dir, config_file=cfg_file, logger=logger
            )
            self.assertIn("Architecture Notes", list_md)
            # Extract generated bs_id from list table
            bs_id = ""
            for line in list_md.splitlines():
                if line.startswith("| `") and "Architecture Notes" in line:
                    bs_id = line.split("`")[1]
                    break
            self.assertTrue(bool(bs_id) and "architecture-notes" in bs_id, msg=list_md)

            l1_md = dispatch_slash_mem(
                f"/mem bs view {bs_id} --l1",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("## Outline", l1_md)

            sec_md = dispatch_slash_mem(
                f"/mem bs view {bs_id} §1",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("Core Design", sec_md)
            self.assertIn("[REDACTED:GITHUB_PAT]", sec_md)
            self.assertNotIn(secret_token, sec_md)

            lines_md = dispatch_slash_mem(
                f"/mem bs view {bs_id} #L1-L4",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("Architecture Notes", lines_md)

            status_md = dispatch_slash_mem(
                f"/mem bs status {bs_id} decided",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("decided", status_md)

            # Reject symlink
            sym_file = root / "link.md"
            os.symlink(artifact_file, sym_file)
            sym_md = dispatch_slash_mem(
                f'/mem bs save "Symlink" --file "{sym_file}"',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("symlink", sym_md.lower())

            # Reject binary file
            bin_file = root / "blob.bin"
            bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
            bin_md = dispatch_slash_mem(
                f'/mem bs save "Binary" --file "{bin_file}"',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("binary", bin_md.lower())

            # Reject > 2 MB file
            big_file = root / "oversize.md"
            big_file.write_bytes(b"a" * (MAX_BRAINSTORM_BYTES + 10))
            big_md = dispatch_slash_mem(
                f'/mem bs save "Big" --file "{big_file}"',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("limit", big_md.lower())

    def test_slash_mem_config_and_invalid_command_guidance(self) -> None:
        from mipmem.adapters.slash_mem import dispatch_slash_mem

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir, cfg_file, logger = self._setup_vault(root)
            log_path = root / "mipmem.log"

            show_md = dispatch_slash_mem(
                "/mem config show", vault_root=vault_dir, config_file=cfg_file, logger=logger
            )
            self.assertIn("l0_max_tokens", show_md)

            set_md = dispatch_slash_mem(
                "/mem config set budget.l0_max_tokens 250",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("250", set_md)

            get_md = dispatch_slash_mem(
                "/mem config get budget.l0_max_tokens",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("250", get_md)

            bad_md = dispatch_slash_mem(
                '/mem recall "unclosed',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("/mem", bad_md)
            entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(entry["error_code"] == SLASH_COMMAND_INVALID for entry in entries)
            )


class TestSessionStartAndPreInvocationHooks(unittest.TestCase):
    """Verify SessionStart (L0 <= 300) and PreInvocation (/mem + L1 <= 450) (S3 §1.5–§1.6)."""

    def _build_env(self, root: Path) -> dict[str, Path]:
        vault_dir = root / "vault"
        cfg_file = root / "config" / "config.toml"
        log_path = root / "mipmem.log"
        ws_active = root / "ws_active"
        ws_passive = root / "ws_passive"
        ws_disabled = root / "ws_disabled"
        for ws in (ws_active, ws_passive, ws_disabled):
            ws.mkdir(parents=True, exist_ok=True)

        init_vault(vault_dir, host_id="host-alpha")
        cfg = default_config()
        cfg["core"]["host_id"] = "host-alpha"
        cfg["core"]["vault_path"] = str(vault_dir)
        cfg["workspace_policies"]["passive_paths"] = [str(ws_passive)]
        cfg["workspace_policies"]["disabled_paths"] = [str(ws_disabled)]
        save_config(cfg, cfg_file)

        write_session_record(
            vault_dir,
            SessionRecord(
                conversation_id="conv-fts-arch",
                host_id="host-alpha",
                surface="ide",
                last_modified_time="2026-11-22T08:30:00.000Z",
                step_count=16,
                title="FTS5 Bigram Recall Architecture",
                edited_files=["src/mipmem/core/fts_recall.py", "src/mipmem/core/mipmap.py"],
                recent_prompts=["Optimize FTS5 bigram recall architecture and BM25 weights"],
                last_agent_response="Implemented FTS5 bigram recall with 6-column BM25 scoring.",
            ),
        )
        sync_fts_index(vault_dir)
        return {
            "vault": vault_dir,
            "config": cfg_file,
            "log": log_path,
            "active": ws_active,
            "passive": ws_passive,
            "disabled": ws_disabled,
        }

    def test_session_start_active_passive_disabled_and_l0_budget(self) -> None:
        from mipmem.adapters.hook_io import handle_session_start
        from mipmem.core.mipmap import estimate_tokens

        with tempfile.TemporaryDirectory() as tmp_dir:
            env = self._build_env(Path(tmp_dir))
            logger = RotatingJsonLogger(log_path=env["log"])

            res_active = handle_session_start(
                {
                    "conversationId": "c-start-1",
                    "workspacePaths": [str(env["active"])],
                    "transcriptPath": (
                        "/tmp/sb/.gemini/antigravity/brain/c-start-1/transcript.jsonl"
                    ),
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertIn("injectSteps", res_active)
            msg = res_active["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("MipMem L0 Radar", msg)
            self.assertLessEqual(estimate_tokens(msg), 300)

            res_passive = handle_session_start(
                {
                    "conversationId": "c-start-2",
                    "workspacePaths": [str(env["passive"])],
                    "transcriptPath": (
                        "/tmp/sb/.gemini/antigravity/brain/c-start-2/transcript.jsonl"
                    ),
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res_passive, {})

            res_disabled = handle_session_start(
                {
                    "conversationId": "c-start-3",
                    "workspacePaths": [str(env["disabled"])],
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res_disabled, {})

            entries = [
                json.loads(line)
                for line in env["log"].read_text(encoding="utf-8").strip().splitlines()
            ]
            hk_actions = [e["details"]["action"] for e in entries if e["event"] == CP_HK_01]
            self.assertIn("injected_l0", hk_actions)
            self.assertGreaterEqual(hk_actions.count("skipped"), 2)

    def test_pre_invocation_slash_mem_unified_and_topic_shift_l1_paging(self) -> None:
        from mipmem.adapters.hook_io import handle_pre_invocation
        from mipmem.core.mipmap import estimate_tokens

        with tempfile.TemporaryDirectory() as tmp_dir:
            env = self._build_env(Path(tmp_dir))
            logger = RotatingJsonLogger(log_path=env["log"])

            # /mem works on BOTH CLI & IDE and on BOTH active & passive workspaces at step 0
            for surface_path, ws_key in (
                ("/tmp/sb/.gemini/antigravity-cli/brain/c1/t.jsonl", "active"),
                ("/tmp/sb/.gemini/antigravity/brain/c2/t.jsonl", "passive"),
            ):
                res = handle_pre_invocation(
                    {
                        "conversationId": "c-slash",
                        "invocationNum": 0,
                        "lastUserInput": "/mem status",
                        "workspacePaths": [str(env[ws_key])],
                        "transcriptPath": surface_path,
                    },
                    vault_root=env["vault"],
                    config_file=env["config"],
                    logger=logger,
                )
                self.assertEqual(res.get("terminationBehavior"), "terminate")
                err_step = res["injectSteps"][0]["errorMessage"]
                self.assertIn("host-alpha", err_step["userMessage"])
                if "antigravity-cli" in surface_path:
                    self.assertIn("\033[0m", err_step["userMessage"])
                else:
                    self.assertNotIn("\033[", err_step["userMessage"])

            # Missing or malformed invocationNum defaults to 0 (processed on step 0)
            for raw_inv in (None, True, False, "invalid"):
                payload_default: dict[str, object] = {
                    "conversationId": "c-default-inv",
                    "lastUserInput": "/mem status",
                    "workspacePaths": [str(env["active"])],
                }
                if raw_inv is not None:
                    payload_default["invocationNum"] = raw_inv
                res_default = handle_pre_invocation(
                    payload_default,
                    vault_root=env["vault"],
                    config_file=env["config"],
                    logger=logger,
                )
                self.assertEqual(res_default.get("terminationBehavior"), "terminate")

            # Disabled workspace blocks /mem too
            res_dis = handle_pre_invocation(
                {
                    "conversationId": "c-dis",
                    "invocationNum": 0,
                    "lastUserInput": "/mem status",
                    "workspacePaths": [str(env["disabled"])],
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res_dis, {})

            # invocationNum != 0 (1, 2, -1) returns {} immediately
            for skip_num in (1, 2, -1):
                res_turn_skip = handle_pre_invocation(
                    {
                        "conversationId": "c-slash",
                        "invocationNum": skip_num,
                        "lastUserInput": "/mem status",
                        "workspacePaths": [str(env["active"])],
                    },
                    vault_root=env["vault"],
                    config_file=env["config"],
                    logger=logger,
                )
                self.assertEqual(res_turn_skip, {})

            # Passive workspace suppresses automatic L1 injection on normal prompts
            res_pas_prompt = handle_pre_invocation(
                {
                    "conversationId": "c-passive-normal",
                    "invocationNum": 0,
                    "lastUserInput": "FTS5 bigram recall architecture",
                    "workspacePaths": [str(env["passive"])],
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res_pas_prompt, {})

            # Active workspace triggers L1 <= 450 on novel prompt, skips repeated same topic
            res_l1_first = handle_pre_invocation(
                {
                    "conversationId": "c-topic-1",
                    "invocationNum": 0,
                    "lastUserInput": "FTS5 bigram scoring weights",
                    "workspacePaths": [str(env["active"])],
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertIn("injectSteps", res_l1_first)
            l1_text = res_l1_first["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("conv-fts-arch", l1_text)
            self.assertLessEqual(estimate_tokens(l1_text), 450)

            res_l1_repeat = handle_pre_invocation(
                {
                    "conversationId": "c-topic-1",
                    "invocationNum": 0,
                    "lastUserInput": "FTS5 bigram scoring weights",
                    "workspacePaths": [str(env["active"])],
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res_l1_repeat, {})

            # Explicit intent cue ("recall") overrides low novelty
            res_l1_cue = handle_pre_invocation(
                {
                    "conversationId": "c-topic-1",
                    "invocationNum": 0,
                    "lastUserInput": "recall FTS5 bigram scoring weights",
                    "workspacePaths": [str(env["active"])],
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertIn("injectSteps", res_l1_cue)

            # Verify CP-HK-01 logs accurate invocation_num on slash_mem, injected_l1, and skipped
            log_entries = [
                json.loads(line)
                for line in env["log"].read_text(encoding="utf-8").strip().splitlines()
            ]
            pre_entries = [
                e for e in log_entries
                if e.get("event") == CP_HK_01
                and e.get("details", {}).get("event") == "pre-invocation"
            ]
            slash_logs = [
                e["details"] for e in pre_entries if e["details"].get("action") == "slash_mem"
            ]
            l1_logs = [
                e["details"] for e in pre_entries if e["details"].get("action") == "injected_l1"
            ]
            fast_skip_logs = [
                e["details"] for e in pre_entries if e["details"].get("policy") == "skipped_fast"
            ]
            self.assertTrue(slash_logs)
            self.assertTrue(all(d.get("invocation_num") == 0 for d in slash_logs))
            self.assertTrue(l1_logs)
            self.assertTrue(all(d.get("invocation_num") == 0 for d in l1_logs))
            self.assertEqual([d.get("invocation_num") for d in fast_skip_logs], [1, 2, -1])


class TestStopHookHarvester(unittest.TestCase):
    """Verify Stop hook loop guards, CLI/IDE harvesting, redaction & idempotency (S3 §1.8)."""

    def _setup_stop_env(self, root: Path) -> dict[str, Path]:
        vault_dir = root / "vault"
        cfg_file = root / "config" / "config.toml"
        log_path = root / "mipmem.log"
        ws_active = root / "ws_active"
        ws_passive = root / "ws_passive"
        ws_disabled = root / "ws_disabled"
        for ws in (ws_active, ws_passive, ws_disabled):
            ws.mkdir(parents=True, exist_ok=True)
        init_vault(vault_dir, host_id="host-alpha")
        cfg = default_config()
        cfg["core"]["host_id"] = "host-alpha"
        cfg["core"]["vault_path"] = str(vault_dir)
        cfg["workspace_policies"]["passive_paths"] = [str(ws_passive)]
        cfg["workspace_policies"]["disabled_paths"] = [str(ws_disabled)]
        save_config(cfg, cfg_file)
        return {
            "vault": vault_dir,
            "config": cfg_file,
            "log": log_path,
            "active": ws_active,
            "passive": ws_passive,
            "disabled": ws_disabled,
        }

    def test_stop_skips_terminal_custom_hook_continuation_disabled_and_subagent(self) -> None:
        from mipmem.adapters.hook_io import handle_stop

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = self._setup_stop_env(root)
            logger = RotatingJsonLogger(log_path=env["log"])
            transcript = root / "antigravity" / "brain" / "c-skip" / "transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"type": "USER_INPUT", "text": "Should be skipped"}) + "\n",
                encoding="utf-8",
            )

            cases = [
                {
                    "conversationId": "c-skip-1",
                    "terminationReason": "TERMINAL_CUSTOM_HOOK",
                    "workspacePaths": [str(env["active"])],
                    "transcriptPath": str(transcript),
                },
                {
                    "conversationId": "c-skip-2",
                    "terminationReason": "NO_TOOL_CALL",
                    "continuationActive": True,
                    "workspacePaths": [str(env["active"])],
                    "transcriptPath": str(transcript),
                },
                {
                    "conversationId": "c-skip-3",
                    "terminationReason": "NO_TOOL_CALL",
                    "workspacePaths": [str(env["disabled"])],
                    "transcriptPath": str(transcript),
                },
                {
                    "conversationId": "c-skip-4",
                    "terminationReason": "NO_TOOL_CALL",
                    "nestingDepth": 1,
                    "workspacePaths": [str(env["active"])],
                    "transcriptPath": str(transcript),
                },
                {
                    "conversationId": "c-skip-5",
                    "terminationReason": "NO_TOOL_CALL",
                    "parentConversationId": "c-parent",
                    "workspacePaths": [str(env["active"])],
                    "transcriptPath": str(transcript),
                },
            ]
            for payload in cases:
                res = handle_stop(
                    payload,
                    vault_root=env["vault"],
                    config_file=env["config"],
                    logger=logger,
                )
                self.assertEqual(res, {})

            sessions_dir = env["vault"] / "hosts" / "host-alpha" / "sessions"
            written = list(sessions_dir.rglob("*.md")) if sessions_dir.exists() else []
            self.assertEqual(written, [])

    def test_stop_harvests_ide_redacts_updates_handoff_idempotent_no_artifact_scan(self) -> None:
        from mipmem.adapters.hook_io import handle_stop
        from mipmem.core.fts_recall import recall_memory

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = self._setup_stop_env(root)
            logger = RotatingJsonLogger(log_path=env["log"])

            brain_dir = root / "antigravity" / "brain" / "conv-stop-ide"
            logs_dir = brain_dir / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            (brain_dir / "walkthrough.md").write_text(
                "ARTIFACT_SHOULD_NOT_BE_HARVESTED", encoding="utf-8"
            )

            secret = "ghp_" + "C" * 24
            lines = [
                json.dumps(
                    {
                        "type": "USER_INPUT",
                        "text": f"Harvested ide stop pipeline design with token {secret}",
                        "timestamp": "2026-11-22T09:00:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "response": "Implemented harvested ide stop pipeline across 4 files.",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": "/tmp/ws/a.py"}},
                            {"name": "edit_file", "args": {"Path": "/tmp/ws/b.py"}},
                            {"name": "edit_file", "args": {"Path": "/tmp/ws/c.py"}},
                            {"name": "write_file", "args": {"Path": "/tmp/ws/d.py"}},
                        ],
                        "timestamp": "2026-11-22T09:05:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "USER_INPUT",
                        "text": "Run all unit tests now",
                        "timestamp": "2026-11-22T09:06:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "response": "All unit tests pass with redacted secrets.",
                        "timestamp": "2026-11-22T09:07:00.000Z",
                    }
                ),
            ]
            transcript = logs_dir / "transcript.jsonl"
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

            payload = {
                "conversationId": "conv-stop-ide",
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "workspacePaths": [str(env["passive"])],
                "transcriptPath": str(transcript),
                "artifactDirectoryPath": str(brain_dir),
            }
            res1 = handle_stop(
                payload,
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res1, {})

            session_file = (
                env["vault"] / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-stop-ide.md"
            )
            self.assertTrue(session_file.is_file())
            content = session_file.read_text(encoding="utf-8")
            self.assertNotIn(secret, content)
            self.assertIn("[REDACTED:GITHUB_PAT]", content)
            self.assertNotIn("ARTIFACT_SHOULD_NOT_BE_HARVESTED", content)

            handoff_file = env["vault"] / "hosts" / "host-alpha" / "latest_handoff.md"
            self.assertTrue(handoff_file.is_file())
            handoff_text = handoff_file.read_text(encoding="utf-8")
            self.assertIn("conv-stop-ide", handoff_text)
            self.assertNotIn(secret, handoff_text)

            recall_res = recall_memory(env["vault"], "harvested ide stop", auto_sync=False)
            self.assertTrue(any(h.item_id == "conv-stop-ide" for h in recall_res.hits))

            # Second Stop with identical step_count & timestamp is idempotently skipped
            res2 = handle_stop(
                payload,
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res2, {})
            entries = [
                json.loads(line)
                for line in env["log"].read_text(encoding="utf-8").strip().splitlines()
            ]
            stop_entries = [e for e in entries if e["event"] == CP_HK_01]
            self.assertEqual(stop_entries[-2]["details"]["action"], "harvested")
            self.assertEqual(stop_entries[-1]["details"]["action"], "skipped")
            self.assertEqual(stop_entries[-1]["details"].get("reason"), "unchanged_idempotent")

    def test_stop_harvests_cli_sqlite_readonly(self) -> None:
        from mipmem.adapters.hook_io import handle_stop
        from mipmem.core.fts_recall import recall_memory

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = self._setup_stop_env(root)
            logger = RotatingJsonLogger(log_path=env["log"])

            cli_root = root / "antigravity-cli"
            logs_dir = cli_root / "brain" / "conv-stop-cli" / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            db_path = cli_root / "conversation_summaries.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        preview TEXT,
                        step_count INTEGER,
                        last_modified_time TEXT,
                        workspace_uris TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE conversations_fts (
                        conversation_id TEXT,
                        chunk_index INTEGER,
                        user_body TEXT,
                        agent_body TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO conversation_summaries VALUES (
                        'conv-stop-cli',
                        'CLI Stop Harvest Test',
                        'Done',
                        8,
                        '2026-11-22T09:20:00.000Z',
                        '["file:///tmp/ws"]',
                        '',
                        0
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO conversations_fts VALUES (
                        'conv-stop-cli',
                        0,
                        'Ship CLI stop harvester',
                        'CLI SQLite mode=ro harvest verified.'
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            res = handle_stop(
                {
                    "conversationId": "conv-stop-cli",
                    "terminationReason": "NO_TOOL_CALL",
                    "workspacePaths": [str(env["active"])],
                    "transcriptPath": str(logs_dir / "transcript.jsonl"),
                },
                vault_root=env["vault"],
                config_file=env["config"],
                logger=logger,
            )
            self.assertEqual(res, {})
            session_file = (
                env["vault"] / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-stop-cli.md"
            )
            self.assertTrue(session_file.is_file())
            self.assertIn("surface: cli", session_file.read_text(encoding="utf-8"))
            recall_res = recall_memory(env["vault"], "CLI stop harvester", auto_sync=False)
            self.assertTrue(any(h.item_id == "conv-stop-cli" for h in recall_res.hits))


class TestSubAppFleetAndL0HookIntegration(unittest.TestCase):
    """Verify /mem fleet, /mem help, and SessionStart L0 telemetry alerts (Task 1.2)."""

    def test_slash_mem_fleet_help_and_session_start_telemetry(self) -> None:
        from mipmem.adapters.hook_io import handle_session_start
        from mipmem.adapters.slash_mem import dispatch_slash_mem
        from mipmem.core.mipmap import estimate_tokens

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cfg_file = root / "config" / "config.toml"
            active_ws = root / "ws-active"
            active_ws.mkdir(parents=True)
            init_vault(vault_dir, host_id="host-alpha")

            cfg = default_config()
            cfg["core"]["host_id"] = "host-alpha"
            cfg["core"]["vault_path"] = str(vault_dir)
            cfg["workspace_policies"]["passive_paths"] = []
            cfg["workspace_policies"]["disabled_paths"] = []
            save_config(cfg, cfg_file)

            telemetry_dir = vault_dir / "hosts" / "host-alpha" / "telemetry"
            telemetry_dir.mkdir(parents=True, exist_ok=True)
            (telemetry_dir / "kernel-watch.status.json").write_text(
                json.dumps(
                    {
                        "app_id": "kernel-watch",
                        "status": "critical",
                        "badge": "reboot needed",
                        "l0_summary": "Kernel update requires reboot",
                        "alert_on_l0": True,
                    }
                ),
                encoding="utf-8",
            )
            write_session_record(
                vault_dir,
                SessionRecord(
                    conversation_id="conv-sub-01",
                    host_id="host-alpha",
                    surface="cli",
                    last_modified_time="2026-11-23T09:30:00.000Z",
                    step_count=10,
                    title="Implement fleet monitoring",
                    edited_files=["src/mipmem/core/subapps.py"],
                    recent_prompts=["Add fleet view"],
                    last_agent_response="Ready.",
                ),
            )

            help_md = dispatch_slash_mem(
                "/mem help", vault_root=vault_dir, config_file=cfg_file
            )
            self.assertIn("/mem fleet", help_md)

            fleet_cli = dispatch_slash_mem(
                "/mem fleet",
                surface="cli",
                vault_root=vault_dir,
                config_file=cfg_file,
            )
            self.assertIn("kernel-watch", fleet_cli)
            self.assertIn("host-alpha", fleet_cli)

            start_res = handle_session_start(
                {"conversationId": "c-s4", "workspacePaths": [str(active_ws)]},
                vault_root=vault_dir,
                config_file=cfg_file,
            )
            injected = start_res["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("kernel-watch", injected)
            self.assertIn("Kernel update requires reboot", injected)
            self.assertLessEqual(estimate_tokens(injected), 300)

    def test_slash_mem_delegates_trusted_subapp_and_never_overrides_core(self) -> None:
        from mipmem.adapters.slash_mem import dispatch_slash_mem

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cfg_dir = root / "config"
            cfg_file = cfg_dir / "config.toml"
            log_path = root / "mipmem.log"
            init_vault(vault_dir, host_id="host-alpha")

            cfg = default_config()
            cfg["core"]["host_id"] = "host-alpha"
            cfg["core"]["vault_path"] = str(vault_dir)
            save_config(cfg, cfg_file)
            logger = RotatingJsonLogger(log_path=log_path)

            apps_dir = cfg_dir / "apps.d"
            bin_dir = cfg_dir / "bin"
            apps_dir.mkdir(parents=True, exist_ok=True)
            bin_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(apps_dir, 0o700)

            script = bin_dir / "hello-app.py"
            secret = "ghp_" + "F" * 24
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"print('hello-subapp args=' + ','.join(sys.argv[1:]) + ' tok={secret}')\n",
                encoding="utf-8",
            )
            os.chmod(script, 0o700)
            manifest = apps_dir / "hello-app.json"
            manifest.write_text(
                json.dumps(
                    {
                        "subcommand": "hello-app",
                        "executable": str(script),
                        "args": ["--ok"],
                        "timeout_sec": 5,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(manifest, 0o600)

            # Attempt to override core 'recall' command MUST be ignored by dispatcher
            collision_manifest = apps_dir / "recall.json"
            collision_manifest.write_text(
                json.dumps({"subcommand": "recall", "executable": str(script)}),
                encoding="utf-8",
            )
            os.chmod(collision_manifest, 0o600)

            res = dispatch_slash_mem(
                "/mem hello-app --ping",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("hello-subapp", res)
            self.assertIn("--ok,--ping", res)
            self.assertIn("[REDACTED:GITHUB_PAT]", res)
            self.assertNotIn(secret, res)

            recall_res = dispatch_slash_mem(
                '/mem recall "hello"',
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertNotIn("hello-subapp", recall_res)

            # World-writable delegate triggers safe formatted error (no crash)
            os.chmod(manifest, 0o666)
            bad_res = dispatch_slash_mem(
                "/mem hello-app --ping",
                vault_root=vault_dir,
                config_file=cfg_file,
                logger=logger,
            )
            self.assertIn("SUBAPP_UNTRUSTED_PERMS", bad_res)

    def test_stop_hook_spawns_detached_sync_only_after_harvest(self) -> None:
        from mipmem.adapters.hook_io import handle_stop

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cfg_file = root / "config" / "config.toml"
            active_ws = root / "ws_active"
            disabled_ws = root / "ws_disabled"
            active_ws.mkdir(parents=True)
            disabled_ws.mkdir(parents=True)
            init_vault(vault_dir, host_id="host-alpha")

            cfg = default_config()
            cfg["core"]["host_id"] = "host-alpha"
            cfg["core"]["vault_path"] = str(vault_dir)
            cfg["sync"]["backend"] = "git"
            cfg["sync"]["vault_remote_url"] = str(root / "remote.git")
            cfg["sync"]["auto_sync_on_stop"] = True
            cfg["workspace_policies"]["disabled_paths"] = [str(disabled_ws)]
            save_config(cfg, cfg_file)

            brain_dir = root / ".gemini" / "antigravity" / "brain" / "c-sync-1"
            t_path = brain_dir / "logs" / "transcript.jsonl"
            t_path.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-11-23T11:00:00.000Z",
                    "type": "USER_INPUT",
                    "text": "Wire detached stop sync worker",
                },
                {
                    "timestamp": "2026-11-23T11:01:00.000Z",
                    "type": "PLANNER_RESPONSE",
                    "response": "Spawned detached worker cleanly.",
                },
            ]
            t_path.write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8",
            )

            with mock.patch("subprocess.Popen") as popen_mock:
                # 1. TERMINAL_CUSTOM_HOOK fast skip -> zero sync
                handle_stop(
                    {
                        "conversationId": "c-sync-1",
                        "terminationReason": "TERMINAL_CUSTOM_HOOK",
                        "workspacePaths": [str(active_ws)],
                        "transcriptPath": str(t_path),
                    },
                    vault_root=vault_dir,
                    config_file=cfg_file,
                )
                popen_mock.assert_not_called()

                # 2. Disabled workspace -> zero sync
                handle_stop(
                    {
                        "conversationId": "c-sync-1",
                        "terminationReason": "NO_TOOL_CALL",
                        "workspacePaths": [str(disabled_ws)],
                        "transcriptPath": str(t_path),
                    },
                    vault_root=vault_dir,
                    config_file=cfg_file,
                )
                popen_mock.assert_not_called()

                # 3. Valid active harvest -> spawns detached sync once
                payload_ok = {
                    "conversationId": "c-sync-1",
                    "terminationReason": "NO_TOOL_CALL",
                    "workspacePaths": [str(active_ws)],
                    "transcriptPath": str(t_path),
                }
                res = handle_stop(payload_ok, vault_root=vault_dir, config_file=cfg_file)
                self.assertEqual(res, {})
                self.assertEqual(popen_mock.call_count, 1)
                _, kwargs = popen_mock.call_args
                self.assertTrue(kwargs.get("start_new_session"))
                self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)

                # 4. Second Stop (idempotent skip) -> must NOT spawn again
                handle_stop(payload_ok, vault_root=vault_dir, config_file=cfg_file)
                self.assertEqual(popen_mock.call_count, 1)

    def test_slash_mem_sync_and_help_with_folder_backend(self) -> None:
        from mipmem.adapters.slash_mem import dispatch_slash_mem

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_a = root / "vault-a"
            vault_b = root / "vault-b"
            shared_dir = root / "shared-sync"
            shared_dir.mkdir()
            cfg_a = root / "cfg-a" / "config.toml"
            cfg_b = root / "cfg-b" / "config.toml"
            init_vault(vault_a, host_id="host-alpha")
            init_vault(vault_b, host_id="host-beta")

            cfg_none = default_config()
            cfg_none["core"]["host_id"] = "host-alpha"
            cfg_none["core"]["vault_path"] = str(vault_a)
            save_config(cfg_none, cfg_a)

            help_md = dispatch_slash_mem("/mem help", vault_root=vault_a, config_file=cfg_a)
            self.assertIn("/mem sync", help_md)

            unconfigured_md = dispatch_slash_mem("/mem sync", vault_root=vault_a, config_file=cfg_a)
            self.assertIn("SYNC_BACKEND_UNCONFIGURED", unconfigured_md)

            write_session_record(
                vault_a,
                SessionRecord(
                    conversation_id="conv-slash-sync-1",
                    host_id="host-alpha",
                    surface="ide",
                    last_modified_time="2026-11-23T11:30:00.000Z",
                    step_count=9,
                    title="Slash sync verification",
                    edited_files=["sync.py"],
                    recent_prompts=["Test /mem sync"],
                    last_agent_response="Done.",
                ),
            )
            cfg_ok_a = default_config()
            cfg_ok_a["core"]["host_id"] = "host-alpha"
            cfg_ok_a["core"]["vault_path"] = str(vault_a)
            cfg_ok_a["sync"]["backend"] = "folder"
            cfg_ok_a["sync"]["folder_path"] = str(shared_dir)
            save_config(cfg_ok_a, cfg_a)

            cfg_ok_b = default_config()
            cfg_ok_b["core"]["host_id"] = "host-beta"
            cfg_ok_b["core"]["vault_path"] = str(vault_b)
            cfg_ok_b["sync"]["backend"] = "folder"
            cfg_ok_b["sync"]["folder_path"] = str(shared_dir)
            save_config(cfg_ok_b, cfg_b)

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(root / "cache-a")}):
                push_md = dispatch_slash_mem(
                    "/mem sync --push-only", vault_root=vault_a, config_file=cfg_a
                )
            self.assertIn("folder", push_md.lower())

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(root / "cache-b")}):
                pull_md = dispatch_slash_mem(
                    "/mem sync --pull-only", vault_root=vault_b, config_file=cfg_b
                )
            self.assertIn("folder", pull_md.lower())
            self.assertTrue(
                (
                    vault_b
                    / "hosts"
                    / "host-alpha"
                    / "sessions"
                    / "2026-11"
                    / "conv-slash-sync-1.md"
                ).is_file()
            )

    def test_plugin_qualified_slash_mem_prefix_intercepts_at_zero_tokens(self) -> None:
        from mipmem.adapters.hook_io import handle_pre_invocation, is_slash_mem_command
        from mipmem.adapters.slash_mem import dispatch_slash_mem
        from mipmem.config import default_config, save_config
        from mipmem.core.vault import init_vault

        for valid in (
            "/mem",
            "/mem fleet",
            "/MEM status",
            "/mipmem:mem",
            "/mipmem:mem fleet",
            "/MIPMEM:MEM status",
            "  /mipmem:mem\tstatus",
        ):
            self.assertTrue(is_slash_mem_command(valid), f"Expected match for {valid!r}")

        for invalid in (
            "",
            "   ",
            "hello /mem",
            "/memory",
            "/mipmem:memory",
            "/mipmem:mem-extra",
        ):
            self.assertFalse(is_slash_mem_command(invalid), f"Unexpected match for {invalid!r}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cfg_file = root / "config.toml"
            ws_dir = root / "ws"
            ws_dir.mkdir()
            init_vault(vault_dir, host_id="host-alpha")
            cfg = default_config()
            cfg["core"]["host_id"] = "host-alpha"
            cfg["core"]["vault_path"] = str(vault_dir)
            save_config(cfg, cfg_file)

            self.assertEqual(
                dispatch_slash_mem("/mipmem:mem", vault_root=vault_dir, config_file=cfg_file),
                dispatch_slash_mem("/mem help", vault_root=vault_dir, config_file=cfg_file),
            )
            self.assertEqual(
                dispatch_slash_mem(
                    "/mipmem:mem status", vault_root=vault_dir, config_file=cfg_file
                ),
                dispatch_slash_mem("/mem status", vault_root=vault_dir, config_file=cfg_file),
            )
            self.assertEqual(
                dispatch_slash_mem(
                    "/mipmem:mem fleet", vault_root=vault_dir, config_file=cfg_file
                ),
                dispatch_slash_mem("/mem fleet", vault_root=vault_dir, config_file=cfg_file),
            )

            for cmd in ("/mipmem:mem status", "/mipmem:mem fleet"):
                res = handle_pre_invocation(
                    {
                        "conversationId": "c-plugin-prefix",
                        "invocationNum": 0,
                        "lastUserInput": cmd,
                        "workspacePaths": [str(ws_dir)],
                        "transcriptPath": "/tmp/sb/.gemini/antigravity/brain/c1/t.jsonl",
                    },
                    vault_root=vault_dir,
                    config_file=cfg_file,
                )
                self.assertEqual(res.get("terminationBehavior"), "terminate")
                err_step = res["injectSteps"][0]["errorMessage"]
                self.assertIn("host-alpha", err_step["userMessage"])
                self.assertNotIn("\033[", err_step["userMessage"])

            non_match = handle_pre_invocation(
                {
                    "conversationId": "c-plugin-nonmatch",
                    "invocationNum": 0,
                    "lastUserInput": "/mipmem:memory",
                    "workspacePaths": [str(ws_dir)],
                },
                vault_root=vault_dir,
                config_file=cfg_file,
            )
            self.assertNotEqual(non_match.get("terminationBehavior"), "terminate")


if __name__ == "__main__":
    unittest.main()
