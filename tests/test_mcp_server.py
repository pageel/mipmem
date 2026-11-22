"""Unit and integration tests for MipMem Universal Local MCP stdio server and 6 tools."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest

from mipmem.config import default_config, save_config
from mipmem.core.brainstorms import MAX_BRAINSTORM_BYTES
from mipmem.core.diagnostics import CP_MCP_01, RotatingJsonLogger
from mipmem.core.fts_recall import sync_fts_index
from mipmem.core.sessions import SessionRecord, write_session_record
from mipmem.core.vault import init_vault


def _frame_content_length(payload: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message using LSP-style Content-Length stdio framing."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _parse_content_length_stream(raw: bytes) -> list[dict[str, Any]]:
    """Decode one or more Content-Length framed JSON-RPC messages from bytes."""
    messages: list[dict[str, Any]] = []
    pos = 0
    total = len(raw)
    while pos < total:
        sep = raw.find(b"\r\n\r\n", pos)
        if sep < 0:
            raise AssertionError(f"Missing header separator in {raw[pos:]!r}")
        header_text = raw[pos:sep].decode("ascii")
        content_len = -1
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_len = int(line.split(":", 1)[1].strip())
        if content_len < 0:
            raise AssertionError(f"Missing Content-Length header in {header_text!r}")
        body_start = sep + 4
        body_end = body_start + content_len
        messages.append(json.loads(raw[body_start:body_end].decode("utf-8")))
        pos = body_end
    return messages


class TestMcpStdioTransport(unittest.TestCase):
    """Verify StdioJsonRpcTransport auto-framing, 4 MB cap, errors & stdout purity (S3 §1.9)."""

    def test_ndjson_framing_initialize_ping_list_and_errors(self) -> None:
        from mipmem.mcp.server import run_mcp_server

        requests = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1.0"},
                    },
                }
            ),
            # Notification has no id -> MUST NOT produce any stdout response
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "unknown/method"}),
            "{broken-json-line",
            json.dumps({"jsonrpc": "2.0", "id": 4}),  # missing method -> -32600
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}),
        ]
        stdin_buf = io.BytesIO(("\n".join(requests) + "\n").encode("utf-8"))
        stdout_buf = io.BytesIO()

        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = RotatingJsonLogger(log_path=Path(tmp_dir) / "mipmem.log")
            rc = run_mcp_server(
                stdin_buffer=stdin_buf,
                stdout_buffer=stdout_buf,
                logger=logger,
                tool_definitions=[
                    {
                        "name": "mem_ping_stub",
                        "description": "Stub tool",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ],
                tool_executor=lambda name, args: {"name": name, "args": args},
            )
            self.assertEqual(rc, 0)

        lines = stdout_buf.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 6)
        responses = [json.loads(line) for line in lines]

        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "mipmem")

        self.assertEqual(responses[1]["id"], 2)
        self.assertEqual(responses[1]["result"], {})

        self.assertEqual(responses[2]["id"], 3)
        self.assertEqual(responses[2]["error"]["code"], -32601)

        self.assertIsNone(responses[3]["id"])
        self.assertEqual(responses[3]["error"]["code"], -32700)

        self.assertEqual(responses[4]["id"], 4)
        self.assertEqual(responses[4]["error"]["code"], -32600)

        self.assertEqual(responses[5]["id"], 5)
        self.assertEqual(responses[5]["result"]["tools"][0]["name"], "mem_ping_stub")

    def test_content_length_framing_and_oversize_rejection(self) -> None:
        from mipmem.mcp.server import MAX_MCP_MESSAGE_BYTES, run_mcp_server

        stream_in = (
            _frame_content_length(
                {
                    "jsonrpc": "2.0",
                    "id": "init-1",
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
            + _frame_content_length(
                {
                    "jsonrpc": "2.0",
                    "id": "call-1",
                    "method": "tools/call",
                    "params": {"name": "demo_tool", "arguments": {"q": "hello"}},
                }
            )
            + _frame_content_length(
                {
                    "jsonrpc": "2.0",
                    "id": "bad-params",
                    "method": "tools/call",
                    "params": {"name": "demo_tool", "arguments": "not-an-object"},
                }
            )
        )
        stdout_buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = RotatingJsonLogger(log_path=Path(tmp_dir) / "mipmem.log")
            rc = run_mcp_server(
                stdin_buffer=io.BytesIO(stream_in),
                stdout_buffer=stdout_buf,
                logger=logger,
                tool_definitions=[
                    {
                        "name": "demo_tool",
                        "description": "Demo",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ],
                tool_executor=lambda name, args: {"echo": args.get("q")},
            )
            self.assertEqual(rc, 0)

        msgs = _parse_content_length_stream(stdout_buf.getvalue())
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertFalse(msgs[1]["result"]["isError"])
        self.assertEqual(json.loads(msgs[1]["result"]["content"][0]["text"]), {"echo": "hello"})
        self.assertEqual(msgs[2]["error"]["code"], -32602)

        # Oversize Content-Length header (> 4 MB) must be rejected cleanly with -32700
        oversize_header = f"Content-Length: {MAX_MCP_MESSAGE_BYTES + 1}\r\n\r\n".encode("ascii")
        over_out = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = RotatingJsonLogger(log_path=Path(tmp_dir) / "mipmem.log")
            rc_over = run_mcp_server(
                stdin_buffer=io.BytesIO(oversize_header),
                stdout_buffer=over_out,
                logger=logger,
                tool_definitions=[],
                tool_executor=lambda _n, _a: {},
            )
            self.assertEqual(rc_over, 0)
        over_msgs = _parse_content_length_stream(over_out.getvalue())
        self.assertEqual(over_msgs[0]["error"]["code"], -32700)


class TestMcpMemoryToolRegistry(unittest.TestCase):
    """Verify 6 MCP memory tools, safe file_path saving, and CP-MCP-01 telemetry (S3 §1.10)."""

    def _setup_vault(self, root: Path) -> tuple[Path, Path, Path]:
        vault_dir = root / "vault"
        cfg_file = root / "config" / "config.toml"
        log_path = root / "mipmem.log"
        init_vault(vault_dir, host_id="host-alpha")
        cfg = default_config()
        cfg["core"]["host_id"] = "host-alpha"
        cfg["core"]["vault_path"] = str(vault_dir)
        save_config(cfg, cfg_file)
        write_session_record(
            vault_dir,
            SessionRecord(
                conversation_id="conv-mcp-01",
                host_id="host-alpha",
                surface="ide",
                last_modified_time="2026-11-22T10:00:00.000Z",
                step_count=14,
                title="Universal MCP Server Implementation",
                edited_files=[
                    "src/mipmem/mcp/server.py",
                    "src/mipmem/mcp/tools.py",
                    "a.py",
                    "b.py",
                ],
                recent_prompts=["Build 6 MCP tools over stdio"],
                last_agent_response="Completed stdio MCP transport and 6 tools.",
            ),
        )
        sync_fts_index(vault_dir)
        return vault_dir, cfg_file, log_path

    def test_all_six_tools_end_to_end_via_ndjson_and_content_length(self) -> None:
        from mipmem.mcp.server import run_mcp_server

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir, cfg_file, log_path = self._setup_vault(root)
            logger = RotatingJsonLogger(log_path=log_path)

            artifact_path = root / "approved_artifact.md"
            secret = "ghp_" + "D" * 24
            artifact_path.write_text(
                "# Artifact Proposal\n\n## Section One\n"
                f"User confirmed saving this artifact. Secret: {secret}\n\n"
                "## Section Two\nSecondary notes.\n",
                encoding="utf-8",
            )

            ndjson_reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_save",
                        "arguments": {
                            "title": "Direct Idea",
                            "content": "# Direct Idea\n\n## Alpha\nAlpha keyword memory.\n",
                            "tags": ["mcp"],
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_save",
                        "arguments": {
                            "title": "Artifact Proposal",
                            "file_path": str(artifact_path),
                            "tags": ["artifact"],
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "mem_bs_list", "arguments": {"limit": 10, "offset": 0}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_recall",
                        "arguments": {"query": "Alpha keyword", "scope": "brainstorms", "limit": 5},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "mem_handoff", "arguments": {"host_id": "host-alpha"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {"name": "mem_fleet_status", "arguments": {}},
                },
            ]
            out_nd = io.BytesIO()
            rc = run_mcp_server(
                vault_root=vault_dir,
                config_file=cfg_file,
                stdin_buffer=io.BytesIO(
                    ("\n".join(json.dumps(r) for r in ndjson_reqs) + "\n").encode("utf-8")
                ),
                stdout_buffer=out_nd,
                logger=logger,
            )
            self.assertEqual(rc, 0)
            resps = [json.loads(line) for line in out_nd.getvalue().decode("utf-8").splitlines()]
            self.assertEqual(len(resps), 8)

            tool_names = [t["name"] for t in resps[1]["result"]["tools"]]
            self.assertEqual(
                tool_names,
                [
                    "mem_recall",
                    "mem_bs_list",
                    "mem_bs_slice",
                    "mem_bs_save",
                    "mem_handoff",
                    "mem_fleet_status",
                ],
            )
            art_bs_id = resps[3]["result"]["structuredContent"]["brainstorm"]["bs_id"]
            self.assertEqual(resps[4]["result"]["structuredContent"]["total"], 2)
            self.assertGreaterEqual(
                len(resps[5]["result"]["structuredContent"]["hits"]),
                1,
            )
            self.assertIn(
                "conv-mcp-01",
                resps[6]["result"]["structuredContent"]["handoff_markdown"],
            )
            fleet = resps[7]["result"]["structuredContent"]
            self.assertTrue(fleet["vault_initialized"])
            self.assertEqual(fleet["subapp_telemetry"], [])
            self.assertEqual(len(fleet["hosts"]), 1)

            # Verify mem_bs_slice via Content-Length framing and secret redaction
            cl_in = _frame_content_length(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_slice",
                        "arguments": {"bs_id": art_bs_id, "selector": "§1"},
                    },
                }
            ) + _frame_content_length(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_slice",
                        "arguments": {"bs_id": art_bs_id, "l1": True},
                    },
                }
            )
            out_cl = io.BytesIO()
            rc_cl = run_mcp_server(
                vault_root=vault_dir,
                config_file=cfg_file,
                stdin_buffer=io.BytesIO(cl_in),
                stdout_buffer=out_cl,
                logger=logger,
            )
            self.assertEqual(rc_cl, 0)
            cl_msgs = _parse_content_length_stream(out_cl.getvalue())
            slice_content = cl_msgs[0]["result"]["structuredContent"]["content"]
            self.assertIn("Section One", slice_content)
            self.assertIn("[REDACTED:GITHUB_PAT]", slice_content)
            self.assertNotIn(secret, slice_content)
            self.assertIn("## Outline", cl_msgs[1]["result"]["structuredContent"]["content"])

            log_entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            mcp_events = [e for e in log_entries if e["event"] == CP_MCP_01]
            framings = {e["details"]["framing"] for e in mcp_events}
            self.assertIn("ndjson", framings)
            self.assertIn("content-length", framings)

    def test_mcp_tool_validation_and_file_safety_guards(self) -> None:
        from mipmem.mcp.server import run_mcp_server

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir, cfg_file, log_path = self._setup_vault(root)
            logger = RotatingJsonLogger(log_path=log_path)

            real_file = root / "real.md"
            real_file.write_text("# Real\n", encoding="utf-8")
            sym_file = root / "sym.md"
            os.symlink(real_file, sym_file)
            bin_file = root / "bin.dat"
            bin_file.write_bytes(b"\x00\x01\x02binary")
            big_file = root / "big.md"
            big_file.write_bytes(b"x" * (MAX_BRAINSTORM_BYTES + 1))

            reqs = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_save",
                        "arguments": {"title": "Sym", "file_path": str(sym_file)},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_save",
                        "arguments": {"title": "Bin", "file_path": str(bin_file)},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_bs_save",
                        "arguments": {"title": "Big", "file_path": str(big_file)},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "mem_recall", "arguments": {}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "mem_bs_slice", "arguments": {}},
                },
            ]
            out_buf = io.BytesIO()
            rc = run_mcp_server(
                vault_root=vault_dir,
                config_file=cfg_file,
                stdin_buffer=io.BytesIO(
                    ("\n".join(json.dumps(r) for r in reqs) + "\n").encode("utf-8")
                ),
                stdout_buffer=out_buf,
                logger=logger,
            )
            self.assertEqual(rc, 0)
            resps = [json.loads(line) for line in out_buf.getvalue().decode("utf-8").splitlines()]
            self.assertEqual([r["error"]["code"] for r in resps], [-32602] * 5)


if __name__ == "__main__":
    unittest.main()
