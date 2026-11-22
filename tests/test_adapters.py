"""Unit tests for dual Antigravity CLI/IDE transcript adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mipmem.core.diagnostics import (
    ADAPTER_READ_FAILED,
    CP_ADP_01,
    RotatingJsonLogger,
)


class TestNormalizedAdapterBase(unittest.TestCase):
    """Verify NormalizedConversation schema, Protocol, and surface detection (S3 §1.1)."""

    def test_detect_surface_from_path_variants(self) -> None:
        from mipmem.adapters.base import detect_surface_from_path

        self.assertEqual(
            detect_surface_from_path(
                "/tmp/sandbox/.gemini/antigravity-cli/brain/c1/transcript.jsonl"
            ),
            "cli",
        )
        self.assertEqual(
            detect_surface_from_path(r"C:\Users\dev\.gemini\antigravity-cli\logs\t.jsonl"),
            "cli",
        )
        self.assertEqual(
            detect_surface_from_path("/tmp/sandbox/.gemini/antigravity/brain/c1/transcript.jsonl"),
            "ide",
        )
        self.assertEqual(
            detect_surface_from_path(
                "/tmp/sandbox/.gemini/antigravity-ide/brain/c1/transcript.jsonl"
            ),
            "ide",
        )
        self.assertEqual(detect_surface_from_path("/tmp/custom/transcript.jsonl"), "generic")
        self.assertEqual(detect_surface_from_path(""), "generic")
        self.assertEqual(detect_surface_from_path(None), "generic")

    def test_normalized_conversation_to_session_record_and_dict(self) -> None:
        from mipmem.adapters.base import NormalizedConversation, TranscriptAdapter
        from mipmem.core.sessions import SessionRecord

        conv = NormalizedConversation(
            conversation_id="conv-alpha-01",
            surface="CLI",
            source_kind="sqlite",
            title="Implement FTS5 recall",
            step_count=12,
            last_modified_time="2026-11-22T05:00:00.000Z",
            workspace_paths=["/tmp/ws/app"],
            recent_prompts=["add fts5 search", "run unit tests"],
            last_agent_response="Implemented BM25 search.",
            edited_files=["src/mipmem/core/fts_recall.py"],
            touched_files=["src/mipmem/core/fts_recall.py", "tests/test_fts_recall.py"],
            brainstorm_refs=["bs-20261122-01"],
        )
        record = conv.to_session_record("host-alpha")
        self.assertIsInstance(record, SessionRecord)
        self.assertEqual(record.conversation_id, "conv-alpha-01")
        self.assertEqual(record.host_id, "host-alpha")
        self.assertEqual(record.surface, "cli")
        self.assertEqual(record.step_count, 12)
        self.assertEqual(record.title, "Implement FTS5 recall")
        self.assertEqual(record.edited_files, ["src/mipmem/core/fts_recall.py"])
        self.assertEqual(record.recent_prompts, ["add fts5 search", "run unit tests"])
        self.assertEqual(record.last_agent_response, "Implemented BM25 search.")
        self.assertEqual(record.brainstorm_refs, ["bs-20261122-01"])

        serialized = conv.to_dict()
        self.assertEqual(serialized["conversation_id"], "conv-alpha-01")
        self.assertEqual(serialized["source_kind"], "sqlite")
        self.assertTrue(hasattr(TranscriptAdapter, "extract"))


def _create_synthetic_cli_db(db_path: Path) -> None:
    """Create a synthetic Antigravity CLI conversation_summaries.db fixture."""
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
                status TEXT,
                source TEXT,
                project_id TEXT,
                agent_name TEXT,
                parent_conversation_id TEXT,
                nesting_depth INTEGER,
                not_fully_idle INTEGER,
                killed INTEGER,
                last_user_input_time TEXT,
                last_user_input_step_index INTEGER,
                app_data_dir TEXT,
                group_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE conversations_fts (
                conversation_id TEXT,
                chunk_index INTEGER,
                title TEXT,
                workspace TEXT,
                project TEXT,
                user_body TEXT,
                agent_body TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_summaries VALUES (
                'conv-cli-01',
                'CLI Refactor',
                'Refactored hook router',
                14,
                '2026-11-22T06:10:00.000Z',
                '["file:///tmp/ws/demo"]',
                'IDLE',
                'cli',
                'demo',
                'antigravity',
                '',
                0,
                0,
                0,
                '2026-11-22T06:09:00.000Z',
                13,
                '/tmp/sandbox/.gemini/antigravity-cli',
                ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_summaries VALUES (
                'conv-sub-depth',
                'Subagent Depth',
                'Child task',
                4,
                '2026-11-22T06:11:00.000Z',
                '["file:///tmp/ws/demo"]',
                'IDLE',
                'cli',
                'demo',
                'research',
                '',
                1,
                0,
                0,
                '2026-11-22T06:11:00.000Z',
                3,
                '/tmp/sandbox/.gemini/antigravity-cli',
                ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_summaries VALUES (
                'conv-sub-parent',
                'Subagent Parent',
                'Child task',
                5,
                '2026-11-22T06:12:00.000Z',
                '["file:///tmp/ws/demo"]',
                'IDLE',
                'cli',
                'demo',
                'research',
                'conv-cli-01',
                0,
                0,
                0,
                '2026-11-22T06:12:00.000Z',
                4,
                '/tmp/sandbox/.gemini/antigravity-cli',
                ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations_fts VALUES (
                'conv-cli-01',
                0,
                'CLI Refactor',
                '/tmp/ws/demo',
                'demo',
                'Implement fast hook router\nCheck cold start latency',
                'Added _fast_hook_dispatch in cli.py.'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations_fts VALUES (
                'conv-cli-01',
                1,
                'CLI Refactor',
                '/tmp/ws/demo',
                'demo',
                'Verify unit tests pass',
                'All 12 unit tests passed under 15ms.'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


class TestAntigravityCliAdapter(unittest.TestCase):
    """Verify Antigravity CLI read-only SQLite harvester & subagent filter (S3 §1.2)."""

    def test_extract_cli_conversation_readonly_sqlite_and_checkpoint(self) -> None:
        from mipmem.adapters.agy_cli import AntigravityCliAdapter, extract_cli_conversation
        from mipmem.adapters.base import TranscriptAdapter

        self.assertIsInstance(AntigravityCliAdapter(), TranscriptAdapter)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "conversation_summaries.db"
            log_path = root / "mipmem.log"
            _create_synthetic_cli_db(db_path)
            os.chmod(db_path, 0o444)
            try:
                logger = RotatingJsonLogger(log_path=log_path)
                conv = extract_cli_conversation("conv-cli-01", db_path=db_path, logger=logger)
            finally:
                os.chmod(db_path, 0o644)

            self.assertIsNotNone(conv)
            assert conv is not None
            self.assertEqual(conv.conversation_id, "conv-cli-01")
            self.assertEqual(conv.surface, "cli")
            self.assertEqual(conv.source_kind, "sqlite")
            self.assertEqual(conv.title, "CLI Refactor")
            self.assertEqual(conv.step_count, 14)
            self.assertEqual(conv.last_modified_time, "2026-11-22T06:10:00.000Z")
            self.assertEqual(conv.workspace_paths, ["/tmp/ws/demo"])
            self.assertIn("Implement fast hook router", conv.recent_prompts)
            self.assertIn("Verify unit tests pass", conv.recent_prompts)
            self.assertIn("All 12 unit tests passed under 15ms.", conv.last_agent_response)

            log_entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(any(entry["event"] == CP_ADP_01 for entry in log_entries))
            cp_entry = [entry for entry in log_entries if entry["event"] == CP_ADP_01][-1]
            self.assertEqual(cp_entry["details"]["surface"], "cli")
            self.assertEqual(cp_entry["details"]["source_kind"], "sqlite")
            self.assertEqual(cp_entry["details"]["step_count"], 14)
            self.assertIsNone(cp_entry["details"]["skipped_reason"])

    def test_extract_cli_conversation_skips_subagents(self) -> None:
        from mipmem.adapters.agy_cli import extract_cli_conversation

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "conversation_summaries.db"
            log_path = root / "mipmem.log"
            _create_synthetic_cli_db(db_path)
            logger = RotatingJsonLogger(log_path=log_path)

            self.assertIsNone(
                extract_cli_conversation("conv-sub-depth", db_path=db_path, logger=logger)
            )
            self.assertIsNone(
                extract_cli_conversation("conv-sub-parent", db_path=db_path, logger=logger)
            )

            log_entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            skipped = [
                entry["details"]["skipped_reason"]
                for entry in log_entries
                if entry["event"] == CP_ADP_01
            ]
            self.assertEqual(skipped, ["subagent", "subagent"])

    def test_extract_cli_conversation_corrupt_db_logs_warn_and_falls_back_to_jsonl(self) -> None:
        from mipmem.adapters.agy_cli import extract_cli_conversation

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_db = root / "conversation_summaries.db"
            bad_db.write_bytes(b"not-a-valid-sqlite-database")
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "USER_INPUT", "text": "Fallback CLI prompt"}),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "response": "Recovered from JSONL fallback.",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            log_path = root / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_path)

            conv = extract_cli_conversation(
                "conv-cli-fallback",
                db_path=bad_db,
                transcript_path=transcript,
                logger=logger,
            )
            self.assertIsNotNone(conv)
            assert conv is not None
            self.assertEqual(conv.surface, "cli")
            self.assertEqual(conv.source_kind, "jsonl")
            self.assertEqual(conv.recent_prompts, ["Fallback CLI prompt"])
            self.assertEqual(conv.last_agent_response, "Recovered from JSONL fallback.")

            log_entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(entry["error_code"] == ADAPTER_READ_FAILED for entry in log_entries)
            )


class TestAntigravityIdeAdapter(unittest.TestCase):
    """Verify Antigravity IDE JSONL harvester, truncation expansion & no artifact scan (S3 §1.3)."""

    def test_extract_ide_conversation_expands_truncated_and_ignores_artifacts(self) -> None:
        from mipmem.adapters.agy_ide import AntigravityIdeAdapter, extract_ide_conversation
        from mipmem.adapters.base import TranscriptAdapter

        self.assertIsInstance(AntigravityIdeAdapter(), TranscriptAdapter)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            brain_dir = root / ".gemini" / "antigravity" / "brain" / "conv-ide-01"
            logs_dir = brain_dir / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)

            # Sentinel artifacts inside artifactDirectoryPath MUST NOT be auto-scanned.
            (brain_dir / "walkthrough.md").write_text(
                "# Walkthrough\nDO_NOT_AUTO_SCAN_ARTIFACT_SENTINEL\n",
                encoding="utf-8",
            )
            (brain_dir / "scratch_notes.md").write_text(
                "DO_NOT_AUTO_SCAN_ARTIFACT_SENTINEL",
                encoding="utf-8",
            )

            compact_lines = [
                json.dumps(
                    {
                        "type": "USER_INPUT",
                        "text": "Build universal MCP stdio server",
                        "timestamp": "2026-11-22T07:00:00.000Z",
                    }
                ),
                "{corrupted-json-line",
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "response": "Shortened...",
                        "truncated_fields": ["response"],
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/tmp/ws/src/cli.py"}},
                            {"name": "edit_file", "args": {"Path": "/tmp/ws/src/mcp/server.py"}},
                        ],
                        "timestamp": "2026-11-22T07:02:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "userInput": {"items": [{"text": "Add Content-Length framing"}]},
                        "timestamp": "2026-11-22T07:05:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "response": "Truncated summary...",
                        "truncated_fields": ["response"],
                        "toolCalls": [
                            {
                                "name": "write_to_file",
                                "arguments": {"TargetFile": "/tmp/ws/tests/test_mcp.py"},
                            }
                        ],
                        "timestamp": "2026-11-22T07:08:00.000Z",
                    }
                ),
            ]
            full_lines = [
                compact_lines[0],
                compact_lines[1],
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "response": "Expanded full planner response for step 2.",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/tmp/ws/src/cli.py"}},
                            {"name": "edit_file", "args": {"Path": "/tmp/ws/src/mcp/server.py"}},
                        ],
                        "timestamp": "2026-11-22T07:02:00.000Z",
                    }
                ),
                compact_lines[3],
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "response": "Expanded full planner response with Content-Length framing.",
                        "toolCalls": [
                            {
                                "name": "write_to_file",
                                "arguments": {"TargetFile": "/tmp/ws/tests/test_mcp.py"},
                            }
                        ],
                        "timestamp": "2026-11-22T07:08:00.000Z",
                    }
                ),
            ]

            transcript_path = logs_dir / "transcript.jsonl"
            transcript_path.write_text("\n".join(compact_lines) + "\n", encoding="utf-8")
            (logs_dir / "transcript_full.jsonl").write_text(
                "\n".join(full_lines) + "\n",
                encoding="utf-8",
            )

            log_path = root / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_path)
            conv = extract_ide_conversation(
                transcript_path,
                workspace_paths=["/tmp/ws"],
                logger=logger,
            )

            self.assertIsNotNone(conv)
            assert conv is not None
            self.assertEqual(conv.conversation_id, "conv-ide-01")
            self.assertEqual(conv.surface, "ide")
            self.assertEqual(conv.source_kind, "jsonl")
            self.assertEqual(conv.step_count, 4)
            self.assertEqual(conv.expanded_truncated_steps, 2)
            self.assertEqual(conv.last_modified_time, "2026-11-22T07:08:00.000Z")
            self.assertEqual(conv.workspace_paths, ["/tmp/ws"])
            self.assertEqual(
                conv.recent_prompts,
                ["Build universal MCP stdio server", "Add Content-Length framing"],
            )
            self.assertEqual(
                conv.last_agent_response,
                "Expanded full planner response with Content-Length framing.",
            )
            self.assertEqual(
                conv.edited_files,
                ["/tmp/ws/src/mcp/server.py", "/tmp/ws/tests/test_mcp.py"],
            )
            self.assertEqual(
                conv.touched_files,
                [
                    "/tmp/ws/src/cli.py",
                    "/tmp/ws/src/mcp/server.py",
                    "/tmp/ws/tests/test_mcp.py",
                ],
            )
            self.assertNotIn("DO_NOT_AUTO_SCAN_ARTIFACT_SENTINEL", json.dumps(conv.to_dict()))

            adapter_conv = AntigravityIdeAdapter().extract(
                conversation_id="conv-ide-01",
                transcript_path=transcript_path,
            )
            self.assertIsNotNone(adapter_conv)
            assert adapter_conv is not None
            self.assertEqual(adapter_conv.expanded_truncated_steps, 2)

            log_entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            cp_entry = [entry for entry in log_entries if entry["event"] == CP_ADP_01][-1]
            self.assertEqual(cp_entry["details"]["surface"], "ide")
            self.assertEqual(cp_entry["details"]["source_kind"], "jsonl")
            self.assertEqual(cp_entry["details"]["expanded_truncated_steps"], 2)
            self.assertIsNone(cp_entry["details"]["skipped_reason"])

    def test_extract_ide_conversation_missing_file_logs_warn(self) -> None:
        from mipmem.adapters.agy_ide import extract_ide_conversation

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_path = root / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_path)
            conv = extract_ide_conversation(
                root / "missing-transcript.jsonl",
                conversation_id="conv-missing",
                logger=logger,
            )
            self.assertIsNone(conv)
            log_entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(entry["error_code"] == ADAPTER_READ_FAILED for entry in log_entries)
            )


if __name__ == "__main__":
    unittest.main()
