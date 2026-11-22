"""Lazy MCP tool registry exposing 6 local memory tools over stdio."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

from mipmem.adapters.slash_mem import (
    _resolve_runtime_context,
    load_recent_sessions,
    read_host_handoff,
    read_safe_text_file,
)
from mipmem.core.brainstorms import (
    load_brainstorm_shard,
    save_brainstorm_shard,
)
from mipmem.core.diagnostics import (
    MCP_INVALID_PARAMS,
    MCP_METHOD_NOT_FOUND,
    VAULT_NOT_INITIALIZED,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.fts_recall import (
    list_indexed_brainstorms,
    recall_memory,
    sync_fts_index,
)
from mipmem.core.mipmap import render_l1_skeleton, slice_brainstorm_l2
from mipmem.core.redactor import redact_text
from mipmem.core.vault import is_vault_initialized, validate_host_id

MCP_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "mem_recall",
        "description": (
            "Search cross-host sessions and lossless brainstorm shards using local "
            "SQLite FTS5 BM25 Bigram recall and return ranked L1 summaries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (EN or VI)."},
                "host": {"type": "string", "description": "Optional origin host_id filter."},
                "status": {
                    "type": "string",
                    "description": "Optional brainstorm status filter.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "sessions", "brainstorms"],
                    "default": "all",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mem_bs_list",
        "description": (
            "List lossless brainstorm shards across hosts with LWW-converged effective_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by effective_status."},
                "host": {"type": "string", "description": "Filter by origin host_id."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
    },
    {
        "name": "mem_bs_slice",
        "description": (
            "Read a brainstorm L1 outline card or surgically slice an L2 section (§k) "
            "or line range (#Lstart-Lend) without loading the entire document."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bs_id": {"type": "string", "description": "Target brainstorm shard ID."},
                "selector": {
                    "type": "string",
                    "description": "Optional L2 selector such as '§2' or '#L10-L40'.",
                },
                "l1": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true and selector is omitted, return L1 skeleton.",
                },
            },
            "required": ["bs_id"],
        },
    },
    {
        "name": "mem_bs_save",
        "description": (
            "Save a redacted lossless brainstorm shard into the active host shard from "
            "inline content or a user-confirmed UTF-8 Markdown file_path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Human-readable brainstorm title."},
                "content": {"type": "string", "description": "Inline Markdown content."},
                "file_path": {
                    "type": "string",
                    "description": (
                        "Optional path to a regular UTF-8 Markdown file (<= 2 MB, non-symlink) "
                        "explicitly confirmed by the user."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": ["open", "decided", "implementing", "shipped", "archived"],
                    "default": "open",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tags.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "mem_handoff",
        "description": (
            "Retrieve the latest Primary Work Anchor handoff document for a host "
            "(defaults to the active host_id)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Optional target host_id."},
            },
        },
    },
    {
        "name": "mem_fleet_status",
        "description": (
            "Summarize cross-host vault inventory (hosts, sessions, brainstorms) "
            "and sub-app telemetry slots."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
)


def _require_str(args: dict[str, Any], key: str) -> str:
    """Validate a required non-empty string argument or raise MCP_INVALID_PARAMS."""
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise MipmemError(MCP_INVALID_PARAMS, f"Parameter {key!r} must be a non-empty string.")
    return val.strip()


def _optional_str(args: dict[str, Any], key: str) -> Optional[str]:
    """Validate an optional string argument or raise MCP_INVALID_PARAMS."""
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise MipmemError(MCP_INVALID_PARAMS, f"Parameter {key!r} must be a string.")
    stripped = val.strip()
    return stripped if stripped else None


def _optional_int(
    args: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Validate an optional bounded integer argument or raise MCP_INVALID_PARAMS."""
    val = args.get(key)
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int):
        raise MipmemError(MCP_INVALID_PARAMS, f"Parameter {key!r} must be an integer.")
    if val < minimum or val > maximum:
        raise MipmemError(
            MCP_INVALID_PARAMS,
            f"Parameter {key!r} must be between {minimum} and {maximum}.",
        )
    return val


# @para-doc [#csa-mcp-memory-tools]
class McpToolRegistry:
    """Registry and executor for the 6 canonical MipMem local MCP memory tools."""

    def __init__(
        self,
        *,
        vault_root: Optional[Union[str, Path]] = None,
        config_file: Optional[Path] = None,
        host_id: Optional[str] = None,
        locale: Optional[str] = None,
        logger: Optional[RotatingJsonLogger] = None,
    ) -> None:
        self._vault_root = vault_root
        self._config_file = config_file
        self._host_id = host_id
        self._locale = locale
        self._logger = logger

    def list_tools(self) -> list[dict[str, Any]]:
        """Return JSON Schema definitions for all 6 MipMem MCP tools."""
        return [dict(item) for item in MCP_TOOL_DEFINITIONS]

    def _runtime(self) -> tuple[Path, str, str]:
        """Resolve active vault directory, host_id, and locale lazily."""
        vault_dir, _, _, active_host, active_locale = _resolve_runtime_context(
            self._vault_root,
            self._config_file,
            self._host_id,
            self._locale,
        )
        return vault_dir, active_host, active_locale

    def _require_vault(self, vault_dir: Path) -> None:
        """Raise VAULT_NOT_INITIALIZED if vault_dir has not been initialized."""
        if not is_vault_initialized(vault_dir):
            raise MipmemError(
                VAULT_NOT_INITIALIZED,
                f"Vault not initialized at {vault_dir}. Run 'mipmem init --host <name>'.",
            )

    def _tool_mem_recall(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _require_str(args, "query")
        host = _optional_str(args, "host")
        status = _optional_str(args, "status")
        scope = _optional_str(args, "scope") or "all"
        if scope not in ("all", "sessions", "brainstorms"):
            raise MipmemError(
                MCP_INVALID_PARAMS,
                f"Invalid scope {scope!r}: expected 'all', 'sessions', or 'brainstorms'.",
            )
        limit = _optional_int(args, "limit", default=5, minimum=1, maximum=50)
        offset = _optional_int(args, "offset", default=0, minimum=0, maximum=500)

        vault_dir, _, locale = self._runtime()
        self._require_vault(vault_dir)
        res = recall_memory(
            vault_dir,
            query,
            locale=locale,
            host_id=host,
            status=status,
            limit=limit + offset,
            scope=scope,
            auto_sync=True,
            logger=self._logger,
        )
        sliced_hits = [hit.to_dict() for hit in res.hits[offset: offset + limit]]
        return {
            "query": query,
            "locale": res.locale,
            "offset": offset,
            "limit": limit,
            "total_returned": len(sliced_hits),
            "duration_ms": res.duration_ms,
            "hits": sliced_hits,
        }

    def _tool_mem_bs_list(self, args: dict[str, Any]) -> dict[str, Any]:
        status = _optional_str(args, "status")
        host = _optional_str(args, "host")
        limit = _optional_int(args, "limit", default=20, minimum=1, maximum=100)
        offset = _optional_int(args, "offset", default=0, minimum=0, maximum=1000)

        vault_dir, _, _ = self._runtime()
        self._require_vault(vault_dir)
        items = list_indexed_brainstorms(
            vault_dir,
            status=status,
            host_id=host,
            auto_sync=True,
            logger=self._logger,
        )
        return {
            "total": len(items),
            "offset": offset,
            "limit": limit,
            "brainstorms": items[offset: offset + limit],
        }

    def _tool_mem_bs_slice(self, args: dict[str, Any]) -> dict[str, Any]:
        bs_id = _require_str(args, "bs_id")
        selector = _optional_str(args, "selector")
        raw_l1 = args.get("l1", False)
        if not isinstance(raw_l1, bool):
            raise MipmemError(MCP_INVALID_PARAMS, "Parameter 'l1' must be a boolean.")

        vault_dir, _, _ = self._runtime()
        self._require_vault(vault_dir)
        shard, content = load_brainstorm_shard(vault_dir, bs_id)
        if raw_l1 and selector is None:
            return render_l1_skeleton(shard, logger=self._logger).to_dict()
        return slice_brainstorm_l2(content, selector=selector, bs_id=shard.bs_id).to_dict()

    def _tool_mem_bs_save(self, args: dict[str, Any]) -> dict[str, Any]:
        title = _require_str(args, "title")
        content_arg = args.get("content")
        file_path_arg = _optional_str(args, "file_path")
        status_arg = _optional_str(args, "status") or "open"
        raw_tags = args.get("tags")

        tags: Optional[list[str]] = None
        if raw_tags is not None:
            if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
                raise MipmemError(
                    MCP_INVALID_PARAMS,
                    "Parameter 'tags' must be an array of strings.",
                )
            tags = [t.strip() for t in raw_tags if t.strip()]

        if file_path_arg is not None:
            raw_content = read_safe_text_file(file_path_arg, error_code=MCP_INVALID_PARAMS)
        elif content_arg is not None:
            if not isinstance(content_arg, str):
                raise MipmemError(MCP_INVALID_PARAMS, "Parameter 'content' must be a string.")
            raw_content = content_arg
        else:
            raw_content = f"# {title}\n"

        vault_dir, active_host, locale = self._runtime()
        self._require_vault(vault_dir)
        redacted_body = redact_text(raw_content, logger=self._logger)
        shard = save_brainstorm_shard(
            vault_dir,
            title,
            redacted_body,
            host_id=active_host,
            status=status_arg,
            tags=tags,
            logger=self._logger,
        )
        sync_fts_index(vault_dir, locale=locale, logger=self._logger)
        return {
            "saved": True,
            "brainstorm": shard.to_meta_dict(),
        }

    def _tool_mem_handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        requested_host = _optional_str(args, "host_id")
        vault_dir, active_host, locale = self._runtime()
        self._require_vault(vault_dir)
        target_host = requested_host if requested_host is not None else active_host
        try:
            safe_host = validate_host_id(target_host)
        except Exception as exc:
            if type(exc).__name__ == "MipmemError":
                raise MipmemError(
                    MCP_INVALID_PARAMS,
                    f"Invalid host_id {target_host!r}.",
                ) from exc
            raise
        handoff_md = read_host_handoff(vault_dir, safe_host, locale=locale)
        return {
            "host_id": safe_host,
            "handoff_markdown": handoff_md,
        }

    def _tool_mem_fleet_status(self, args: dict[str, Any]) -> dict[str, Any]:
        _ = args
        vault_dir, active_host, _ = self._runtime()
        initialized = is_vault_initialized(vault_dir)
        if not initialized:
            return {
                "vault_dir": str(vault_dir),
                "vault_initialized": False,
                "active_host_id": active_host,
                "hosts": [],
                "total_sessions": 0,
                "total_brainstorms": 0,
                "subapp_telemetry": [],
            }

        hosts_dir = vault_dir / "hosts"
        indexed_bs = list_indexed_brainstorms(vault_dir, auto_sync=True, logger=self._logger)
        bs_by_host: dict[str, int] = {}
        for item in indexed_bs:
            origin = str(item.get("origin_host", ""))
            bs_by_host[origin] = bs_by_host.get(origin, 0) + 1

        hosts_summary: list[dict[str, Any]] = []
        total_sessions = 0
        if hosts_dir.is_dir():
            for host_dir in sorted(hosts_dir.iterdir()):
                if not host_dir.is_dir() or host_dir.is_symlink():
                    continue
                h_id = host_dir.name
                meta_info: dict[str, Any] = {}
                meta_file = host_dir / "host_meta.json"
                if meta_file.is_file() and not meta_file.is_symlink():
                    try:
                        parsed = json.loads(meta_file.read_text(encoding="utf-8"))
                        if isinstance(parsed, dict):
                            meta_info = parsed
                    except (OSError, json.JSONDecodeError):
                        meta_info = {}
                sess_count = len(load_recent_sessions(vault_dir, host_id=h_id, limit=2000))
                total_sessions += sess_count
                handoff_exists = (host_dir / "latest_handoff.md").is_file()
                hosts_summary.append(
                    {
                        "host_id": h_id,
                        "sessions_count": sess_count,
                        "brainstorms_count": bs_by_host.get(h_id, 0),
                        "has_latest_handoff": handoff_exists,
                        "meta": meta_info,
                    }
                )

        return {
            "vault_dir": str(vault_dir),
            "vault_initialized": True,
            "active_host_id": active_host,
            "hosts": hosts_summary,
            "total_sessions": total_sessions,
            "total_brainstorms": len(indexed_bs),
            "subapp_telemetry": [],
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one validated MCP tool call by name."""
        dispatch_map = {
            "mem_recall": self._tool_mem_recall,
            "mem_bs_list": self._tool_mem_bs_list,
            "mem_bs_slice": self._tool_mem_bs_slice,
            "mem_bs_save": self._tool_mem_bs_save,
            "mem_handoff": self._tool_mem_handoff,
            "mem_fleet_status": self._tool_mem_fleet_status,
        }
        handler = dispatch_map.get(name)
        if handler is None:
            raise MipmemError(MCP_METHOD_NOT_FOUND, f"Unknown MCP tool: {name!r}.")
        return handler(arguments)


def call_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    vault_root: Optional[Union[str, Path]] = None,
    config_file: Optional[Path] = None,
    host_id: Optional[str] = None,
    locale: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Convenience functional wrapper invoking McpToolRegistry.call_tool()."""
    registry = McpToolRegistry(
        vault_root=vault_root,
        config_file=config_file,
        host_id=host_id,
        locale=locale,
        logger=logger,
    )
    return registry.call_tool(name, arguments)
