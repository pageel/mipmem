"""Stdio JSON-RPC 2.0 MCP server auto-detecting NDJSON and Content-Length framing."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Optional, Sequence, Union

from mipmem import __version__
from mipmem.core.diagnostics import (
    CP_MCP_01,
    MCP_INVALID_PARAMS,
    MCP_METHOD_NOT_FOUND,
    MCP_PARSE_ERROR,
    RotatingJsonLogger,
)

MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class _TransportReadResult:
    """Internal result returned by StdioJsonRpcTransport.read_message()."""

    __slots__ = ("eof", "message", "error_code", "error_message", "abort_after_error")

    def __init__(
        self,
        *,
        eof: bool = False,
        message: Optional[Any] = None,
        error_code: Optional[int] = None,
        error_message: str = "",
        abort_after_error: bool = False,
    ) -> None:
        self.eof = eof
        self.message = message
        self.error_code = error_code
        self.error_message = error_message
        self.abort_after_error = abort_after_error


# @para-doc [#csa-mcp-stdio-transport]
class StdioJsonRpcTransport:
    """Binary stdio JSON-RPC 2.0 transport auto-detecting NDJSON vs Content-Length."""

    def __init__(
        self,
        stdin_buffer: Optional[Any] = None,
        stdout_buffer: Optional[Any] = None,
        *,
        max_message_bytes: int = MAX_MCP_MESSAGE_BYTES,
    ) -> None:
        self._in = stdin_buffer if stdin_buffer is not None else sys.stdin.buffer
        self._out = stdout_buffer if stdout_buffer is not None else sys.stdout.buffer
        self.max_message_bytes = max_message_bytes
        self.framing: Optional[str] = None

    def _read_exact(self, length: int) -> Optional[bytes]:
        """Read exactly length bytes from stdin_buffer, returning None on premature EOF."""
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            part = self._in.read(remaining)
            if not part:
                return None
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)

    def _read_content_length_frame(self, first_line: bytes) -> _TransportReadResult:
        """Read remaining headers and exact body bytes for a Content-Length framed message."""
        self.framing = "content-length"
        headers: dict[str, str] = {}

        def _parse_header_line(raw: bytes) -> bool:
            try:
                text = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                return False
            if ":" not in text:
                return False
            key, val = text.split(":", 1)
            headers[key.strip().lower()] = val.strip()
            return True

        if not _parse_header_line(first_line):
            return _TransportReadResult(
                error_code=JSONRPC_PARSE_ERROR,
                error_message="Malformed framing header.",
                abort_after_error=True,
            )

        while True:
            hdr_line = self._in.readline(8192)
            if not hdr_line:
                return _TransportReadResult(eof=True)
            if hdr_line in (b"\r\n", b"\n"):
                break
            _parse_header_line(hdr_line)

        raw_len = headers.get("content-length")
        if raw_len is None:
            return _TransportReadResult(
                error_code=JSONRPC_PARSE_ERROR,
                error_message="Missing Content-Length header.",
                abort_after_error=True,
            )
        try:
            content_length = int(raw_len)
        except ValueError:
            return _TransportReadResult(
                error_code=JSONRPC_PARSE_ERROR,
                error_message=f"Invalid Content-Length header: {raw_len!r}.",
                abort_after_error=True,
            )
        if content_length < 0 or content_length > self.max_message_bytes:
            return _TransportReadResult(
                error_code=JSONRPC_PARSE_ERROR,
                error_message=(
                    f"Message size {content_length} exceeds {self.max_message_bytes} cap."
                ),
                abort_after_error=True,
            )

        body = self._read_exact(content_length)
        if body is None:
            return _TransportReadResult(eof=True)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _TransportReadResult(
                error_code=JSONRPC_PARSE_ERROR,
                error_message=f"JSON parse error: {exc}",
            )
        return _TransportReadResult(message=decoded)

    def read_message(self) -> _TransportReadResult:
        """Read one JSON-RPC message auto-detecting NDJSON vs Content-Length headers."""
        while True:
            line = self._in.readline(self.max_message_bytes + 1)
            if not line:
                return _TransportReadResult(eof=True)
            stripped = line.strip()
            if not stripped:
                continue

            lower_prefix = stripped[:20].lower()
            is_header = lower_prefix.startswith(b"content-length:") or (
                not stripped.startswith((b"{", b"["))
                and b":" in stripped
                and lower_prefix.startswith(b"content-type:")
            )
            if is_header:
                return self._read_content_length_frame(line)

            if self.framing is None:
                self.framing = "ndjson"
            if len(line) > self.max_message_bytes:
                return _TransportReadResult(
                    error_code=JSONRPC_PARSE_ERROR,
                    error_message=f"NDJSON line exceeds {self.max_message_bytes} byte limit.",
                    abort_after_error=True,
                )
            try:
                decoded = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return _TransportReadResult(
                    error_code=JSONRPC_PARSE_ERROR,
                    error_message=f"JSON parse error: {exc}",
                )
            return _TransportReadResult(message=decoded)

    def write_message(self, payload: dict[str, Any]) -> None:
        """Write one JSON-RPC response using the active framing without stray logs."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.framing == "content-length":
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            self._out.write(header + body)
        else:
            self._out.write(body + b"\n")
        self._out.flush()


def _error_response(
    req_id: Any,
    code: int,
    message: str,
    *,
    mipmem_code: Optional[str] = None,
) -> dict[str, Any]:
    """Build a canonical JSON-RPC 2.0 error response envelope."""
    err_obj: dict[str, Any] = {"code": code, "message": message}
    if mipmem_code:
        err_obj["data"] = {"error_code": mipmem_code}
    return {"jsonrpc": "2.0", "id": req_id, "error": err_obj}


def _result_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a canonical JSON-RPC 2.0 success response envelope."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def run_mcp_server(
    *,
    vault_root: Optional[Union[str, Path]] = None,
    config_file: Optional[Path] = None,
    stdin_buffer: Optional[Any] = None,
    stdout_buffer: Optional[Any] = None,
    logger: Optional[RotatingJsonLogger] = None,
    tool_definitions: Optional[Sequence[dict[str, Any]]] = None,
    tool_executor: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
) -> int:
    """Run the universal local MCP stdio server loop until stdin EOF."""
    transport = StdioJsonRpcTransport(stdin_buffer=stdin_buffer, stdout_buffer=stdout_buffer)
    active_logger = logger if logger is not None else RotatingJsonLogger()

    if tool_definitions is not None and tool_executor is not None:
        defs = list(tool_definitions)
        executor = tool_executor
    else:
        from mipmem.mcp.tools import McpToolRegistry

        registry = McpToolRegistry(
            vault_root=vault_root,
            config_file=config_file,
            logger=active_logger,
        )
        defs = registry.list_tools()
        executor = registry.call_tool

    known_tool_names = {str(item.get("name", "")) for item in defs}

    while True:
        res = transport.read_message()
        if res.eof:
            break
        if res.error_code is not None:
            active_logger.log_event(
                "WARN",
                "mcp",
                CP_MCP_01,
                error_code=MCP_PARSE_ERROR,
                details={
                    "tool_name": None,
                    "framing": transport.framing or "ndjson",
                    "success": False,
                    "reason": res.error_message,
                },
            )
            transport.write_message(
                _error_response(
                    None,
                    res.error_code,
                    res.error_message,
                    mipmem_code=MCP_PARSE_ERROR,
                )
            )
            if res.abort_after_error:
                break
            continue

        msg = res.message
        if not isinstance(msg, dict):
            transport.write_message(
                _error_response(None, JSONRPC_INVALID_REQUEST, "Request must be a JSON object.")
            )
            continue

        has_id = "id" in msg
        req_id = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str) or not method.strip():
            if has_id:
                transport.write_message(
                    _error_response(req_id, JSONRPC_INVALID_REQUEST, "Missing request method.")
                )
            continue

        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                transport.write_message(
                    _error_response(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Request params must be a JSON object.",
                        mipmem_code=MCP_INVALID_PARAMS,
                    )
                )
            continue

        # Notifications carry no id and require no JSON-RPC response.
        if not has_id or method.startswith("notifications/"):
            continue

        if method == "initialize":
            client_proto = str(params.get("protocolVersion") or "").strip()
            negotiated = (
                client_proto
                if client_proto in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            transport.write_message(
                _result_response(
                    req_id,
                    {
                        "protocolVersion": negotiated,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mipmem", "version": __version__},
                    },
                )
            )
            continue

        if method == "ping":
            transport.write_message(_result_response(req_id, {}))
            continue

        if method == "tools/list":
            transport.write_message(_result_response(req_id, {"tools": defs}))
            continue

        if method == "tools/call":
            started = time.perf_counter()
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            framing_label = transport.framing or "ndjson"
            if not isinstance(tool_name, str) or not tool_name.strip():
                transport.write_message(
                    _error_response(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "tools/call requires string 'name'.",
                        mipmem_code=MCP_INVALID_PARAMS,
                    )
                )
                continue
            if tool_args is None:
                tool_args = {}
            if not isinstance(tool_args, dict):
                active_logger.log_event(
                    "WARN",
                    "mcp",
                    CP_MCP_01,
                    error_code=MCP_INVALID_PARAMS,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    details={
                        "tool_name": tool_name,
                        "framing": framing_label,
                        "success": False,
                    },
                )
                transport.write_message(
                    _error_response(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "tools/call 'arguments' must be a JSON object.",
                        mipmem_code=MCP_INVALID_PARAMS,
                    )
                )
                continue
            if tool_name not in known_tool_names:
                active_logger.log_event(
                    "WARN",
                    "mcp",
                    CP_MCP_01,
                    error_code=MCP_METHOD_NOT_FOUND,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    details={
                        "tool_name": tool_name,
                        "framing": framing_label,
                        "success": False,
                    },
                )
                transport.write_message(
                    _error_response(
                        req_id,
                        JSONRPC_METHOD_NOT_FOUND,
                        f"Unknown MCP tool: {tool_name!r}.",
                        mipmem_code=MCP_METHOD_NOT_FOUND,
                    )
                )
                continue

            try:
                data = executor(tool_name, tool_args)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                active_logger.log_event(
                    "INFO",
                    "mcp",
                    CP_MCP_01,
                    duration_ms=elapsed_ms,
                    details={
                        "tool_name": tool_name,
                        "framing": framing_label,
                        "success": True,
                        "duration_ms": round(elapsed_ms, 3),
                    },
                )
                transport.write_message(
                    _result_response(
                        req_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(data, ensure_ascii=False),
                                }
                            ],
                            "structuredContent": data,
                            "isError": False,
                        },
                    )
                )
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if type(exc).__name__ == "MipmemError":
                    err_code = str(getattr(exc, "code", MCP_INVALID_PARAMS))
                    err_msg = str(getattr(exc, "message", str(exc)))
                    active_logger.log_event(
                        "WARN",
                        "mcp",
                        CP_MCP_01,
                        error_code=err_code,
                        duration_ms=elapsed_ms,
                        details={
                            "tool_name": tool_name,
                            "framing": framing_label,
                            "success": False,
                            "duration_ms": round(elapsed_ms, 3),
                        },
                    )
                    if err_code == MCP_INVALID_PARAMS:
                        transport.write_message(
                            _error_response(
                                req_id,
                                JSONRPC_INVALID_PARAMS,
                                err_msg,
                                mipmem_code=err_code,
                            )
                        )
                    elif err_code == MCP_METHOD_NOT_FOUND:
                        transport.write_message(
                            _error_response(
                                req_id,
                                JSONRPC_METHOD_NOT_FOUND,
                                err_msg,
                                mipmem_code=err_code,
                            )
                        )
                    else:
                        err_payload = {"error": {"code": err_code, "message": err_msg}}
                        transport.write_message(
                            _result_response(
                                req_id,
                                {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": json.dumps(err_payload, ensure_ascii=False),
                                        }
                                    ],
                                    "isError": True,
                                },
                            )
                        )
                else:
                    active_logger.log_event(
                        "ERROR",
                        "mcp",
                        CP_MCP_01,
                        duration_ms=elapsed_ms,
                        details={
                            "tool_name": tool_name,
                            "framing": framing_label,
                            "success": False,
                            "exception": type(exc).__name__,
                        },
                    )
                    transport.write_message(
                        _error_response(
                            req_id,
                            JSONRPC_INTERNAL_ERROR,
                            f"Internal tool execution error: {type(exc).__name__}",
                        )
                    )
            continue

        active_logger.log_event(
            "WARN",
            "mcp",
            CP_MCP_01,
            error_code=MCP_METHOD_NOT_FOUND,
            details={"method": method, "framing": transport.framing or "ndjson", "success": False},
        )
        transport.write_message(
            _error_response(
                req_id,
                JSONRPC_METHOD_NOT_FOUND,
                f"Method not found: {method!r}.",
                mipmem_code=MCP_METHOD_NOT_FOUND,
            )
        )

    return 0
