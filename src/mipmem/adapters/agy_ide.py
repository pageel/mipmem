"""Antigravity IDE compact/full JSONL transcript harvester (no auto artifact scan)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Optional, Union

from mipmem.adapters.base import NormalizedConversation, detect_surface_from_path
from mipmem.core.diagnostics import (
    ADAPTER_READ_FAILED,
    CP_ADP_01,
    RotatingJsonLogger,
)

_MAX_RECENT_PROMPTS = 10
_MAX_PROMPT_CHARS = 500
_MAX_RESPONSE_CHARS = 2000

_EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "write_file",
        "write_to_file",
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "apply_patch",
        "create_file",
    }
)
_READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "view_file",
        "view_file_outline",
        "view_code_item",
        "open_file",
    }
)
_PATH_ARG_KEYS: tuple[str, ...] = (
    "Path",
    "path",
    "TargetFile",
    "target_file",
    "file_path",
    "AbsolutePath",
    "absolute_path",
)


def _normalize_timestamp(raw: Any) -> str:
    """Normalize string ISO or numeric epoch timestamps into ISO-8601 UTC Z format."""
    if raw is None:
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value <= 0:
            return ""
        if value > 1e12:
            value /= 1000.0
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        ms = dt.microsecond // 1000
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"
    text = str(raw).strip()
    if not text:
        return ""
    if text.isdigit():
        return _normalize_timestamp(int(text))
    return text


def _infer_conversation_id(transcript_path: Path) -> str:
    """Infer conversation_id from a brain/<conv_id>/.../transcript.jsonl path."""
    parts = list(transcript_path.parts)
    for idx, segment in enumerate(parts):
        if segment.lower() == "brain" and idx + 1 < len(parts):
            candidate = parts[idx + 1].strip()
            if candidate and not candidate.startswith("."):
                return candidate
    return transcript_path.stem or "session-unknown"


# @para-doc [#csa-ide-prompt-wrapper-stripper]
def _strip_ide_prompt_wrappers(text: str) -> str:
    """Strip outer Antigravity IDE XML request/metadata wrappers when present at root."""
    stripped = text.strip()
    open_tag = "<USER_REQUEST>"
    close_tag = "</USER_REQUEST>"
    if not stripped.startswith(open_tag):
        return text
    end_idx = stripped.find(close_tag, len(open_tag))
    if end_idx < 0:
        return text
    inner = stripped[len(open_tag):end_idx].strip()
    return inner or stripped


def _extract_user_text(entry: dict[str, Any]) -> str:
    """Extract user prompt text from compact or nested Antigravity userInput objects."""
    user_obj = entry.get("userInput") or entry.get("user_input")
    if isinstance(user_obj, dict):
        items = user_obj.get("items")
        if isinstance(items, list):
            parts: list[str] = []
            for item in items:
                if isinstance(item, dict):
                    part = str(item.get("text") or item.get("content") or "").strip()
                    if part:
                        parts.append(part)
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            if parts:
                return _strip_ide_prompt_wrappers("\n".join(parts))
        for key in ("text", "content", "message"):
            val = user_obj.get(key)
            if isinstance(val, str) and val.strip():
                return _strip_ide_prompt_wrappers(val.strip())

    entry_type = str(entry.get("type") or "").upper()
    role = str(entry.get("role") or "").lower()
    if entry_type in ("USER_INPUT", "USER") or role == "user":
        for key in ("text", "content", "message"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return _strip_ide_prompt_wrappers(val.strip())
    return ""


def _extract_planner_response(entry: dict[str, Any]) -> str:
    """Extract planner response text from flat or nested Antigravity plannerResponse."""
    planner_obj = entry.get("plannerResponse") or entry.get("planner_response")
    if isinstance(planner_obj, dict):
        for key in ("modifiedResponse", "response", "text", "content", "message"):
            val = planner_obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    entry_type = str(entry.get("type") or "").upper()
    role = str(entry.get("role") or "").lower()
    if entry_type in ("PLANNER_RESPONSE", "ASSISTANT", "MODEL") or role in ("assistant", "model"):
        for key in ("modifiedResponse", "response", "text", "content", "message"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    """Decode tool call arguments whether provided as a dict or JSON string."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                return {}
    return {}


def _extract_tool_calls(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect tool call records from flat or nested transcript entries."""
    calls: list[dict[str, Any]] = []
    for key in ("tool_calls", "toolCalls"):
        raw = entry.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    calls.append(item)

    planner_obj = entry.get("plannerResponse") or entry.get("planner_response")
    if isinstance(planner_obj, dict):
        for key in ("tool_calls", "toolCalls"):
            raw = planner_obj.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        calls.append(item)

    entry_type = str(entry.get("type") or "").upper()
    if entry_type == "TOOL_CALL" and (entry.get("name") or entry.get("tool_name")):
        calls.append(entry)
    return calls


def _record_file_paths(
    tool_calls: list[dict[str, Any]],
    edited_files: list[str],
    edited_seen: set[str],
    touched_files: list[str],
    touched_seen: set[str],
) -> None:
    """Update edited_files and touched_files lists from parsed tool calls."""
    for call in tool_calls:
        raw_name = call.get("name") or call.get("tool_name")
        if not raw_name and isinstance(call.get("function"), dict):
            raw_name = call["function"].get("name")
        tool_name = str(raw_name or "").strip().lower()
        if tool_name not in _EDIT_TOOL_NAMES and tool_name not in _READ_TOOL_NAMES:
            continue

        raw_args = call.get("args")
        if raw_args is None:
            raw_args = call.get("arguments")
        if raw_args is None and isinstance(call.get("function"), dict):
            raw_args = call["function"].get("arguments")
        args = _parse_tool_args(raw_args)

        for key in _PATH_ARG_KEYS:
            val = args.get(key)
            if not isinstance(val, str):
                continue
            cleaned = val.strip()
            if not cleaned:
                continue
            if cleaned not in touched_seen:
                touched_seen.add(cleaned)
                touched_files.append(cleaned)
            if tool_name in _EDIT_TOOL_NAMES and cleaned not in edited_seen:
                edited_seen.add(cleaned)
                edited_files.append(cleaned)
            break


def _load_full_transcript_lines(compact_path: Path) -> Optional[list[str]]:
    """Lazily read sibling transcript_full.jsonl lines if present."""
    full_path = compact_path.parent / "transcript_full.jsonl"
    if not full_path.is_file():
        return None
    try:
        return full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _log_ide_event(
    logger: Optional[RotatingJsonLogger],
    *,
    level: str,
    surface: str,
    conversation_id: str,
    step_count: int = 0,
    expanded_truncated_steps: int = 0,
    skipped_reason: Optional[str] = None,
    error_code: Optional[str] = None,
    duration_ms: float = 0.0,
) -> None:
    """Emit structured CP-ADP-01 diagnostics without leaking raw transcript text."""
    if logger is None:
        return
    logger.log_event(
        level,
        "adapters",
        CP_ADP_01,
        error_code=error_code,
        duration_ms=duration_ms,
        details={
            "surface": surface,
            "source_kind": "jsonl",
            "conversation_id": conversation_id,
            "step_count": step_count,
            "expanded_truncated_steps": expanded_truncated_steps,
            "skipped_reason": skipped_reason,
        },
    )


# @para-doc [#csa-adapter-agy-ide-harvester]
def extract_ide_conversation(
    transcript_path: Union[str, Path],
    *,
    conversation_id: Optional[str] = None,
    workspace_paths: Optional[list[str]] = None,
    surface_override: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> Optional[NormalizedConversation]:
    """Extract a normalized IDE conversation from transcript.jsonl and transcript_full.jsonl.

    Does NOT scan artifactDirectoryPath or sibling Markdown artifacts (S3 Assumption #3).
    """
    start = time.perf_counter()
    path = Path(transcript_path).expanduser()
    surface = surface_override if surface_override else detect_surface_from_path(path)
    if surface == "generic" and surface_override is None and "antigravity" in str(path).lower():
        surface = "ide"
    resolved_conv_id = (conversation_id or "").strip() or _infer_conversation_id(path)

    if not path.is_file():
        _log_ide_event(
            logger,
            level="WARN",
            surface=surface,
            conversation_id=resolved_conv_id,
            skipped_reason="read_failed",
            error_code=ADAPTER_READ_FAILED,
            duration_ms=(time.perf_counter() - start) * 1000.0,
        )
        return None

    full_lines_loaded = False
    full_lines: Optional[list[str]] = None

    prompts: list[str] = []
    last_response = ""
    edited_files: list[str] = []
    edited_seen: set[str] = set()
    touched_files: list[str] = []
    touched_seen: set[str] = set()
    step_count = 0
    expanded_truncated_steps = 0
    title = ""
    last_modified = ""

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line_index, raw_line in enumerate(handle):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                if entry.get("truncated_fields"):
                    if not full_lines_loaded:
                        full_lines = _load_full_transcript_lines(path)
                        full_lines_loaded = True
                    if full_lines is not None and line_index < len(full_lines):
                        candidate_raw = full_lines[line_index].strip()
                        if candidate_raw:
                            try:
                                expanded = json.loads(candidate_raw)
                                if isinstance(expanded, dict):
                                    entry = expanded
                                    expanded_truncated_steps += 1
                            except json.JSONDecodeError:
                                pass

                parent_id = str(entry.get("parent_conversation_id") or "").strip()
                raw_depth = entry.get("nesting_depth", 0)
                if isinstance(raw_depth, int) and not isinstance(raw_depth, bool):
                    nesting_depth = raw_depth
                else:
                    nesting_depth = 0
                if nesting_depth > 0 or parent_id:
                    _log_ide_event(
                        logger,
                        level="INFO",
                        surface=surface,
                        conversation_id=resolved_conv_id,
                        step_count=step_count,
                        expanded_truncated_steps=expanded_truncated_steps,
                        skipped_reason="subagent",
                        duration_ms=(time.perf_counter() - start) * 1000.0,
                    )
                    return None

                step_count += 1
                ts = _normalize_timestamp(entry.get("timestamp") or entry.get("last_modified_time"))
                if ts:
                    last_modified = ts
                if not title and isinstance(entry.get("title"), str) and entry["title"].strip():
                    title = entry["title"].strip()

                user_text = _extract_user_text(entry)
                if user_text:
                    prompts.append(user_text[:_MAX_PROMPT_CHARS])

                planner_text = _extract_planner_response(entry)
                if planner_text:
                    last_response = planner_text[:_MAX_RESPONSE_CHARS]

                tool_calls = _extract_tool_calls(entry)
                if tool_calls:
                    _record_file_paths(
                        tool_calls,
                        edited_files,
                        edited_seen,
                        touched_files,
                        touched_seen,
                    )
    except OSError:
        _log_ide_event(
            logger,
            level="WARN",
            surface=surface,
            conversation_id=resolved_conv_id,
            skipped_reason="read_failed",
            error_code=ADAPTER_READ_FAILED,
            duration_ms=(time.perf_counter() - start) * 1000.0,
        )
        return None

    if step_count == 0:
        _log_ide_event(
            logger,
            level="INFO",
            surface=surface,
            conversation_id=resolved_conv_id,
            skipped_reason="empty_transcript",
            duration_ms=(time.perf_counter() - start) * 1000.0,
        )
        return None

    if not last_modified:
        try:
            last_modified = _normalize_timestamp(path.stat().st_mtime)
        except OSError:
            last_modified = ""

    non_slash_prompts = [p for p in prompts if not p.lstrip().startswith("/mem")]
    resolved_title = title or (
        non_slash_prompts[0][:120]
        if non_slash_prompts
        else (prompts[0][:120] if prompts else resolved_conv_id)
    )
    conv = NormalizedConversation(
        conversation_id=resolved_conv_id,
        surface=surface,
        source_kind="jsonl",
        title=resolved_title,
        step_count=step_count,
        last_modified_time=last_modified,
        workspace_paths=list(workspace_paths) if workspace_paths else [],
        recent_prompts=prompts[-_MAX_RECENT_PROMPTS:],
        last_agent_response=last_response,
        edited_files=edited_files,
        touched_files=touched_files,
        expanded_truncated_steps=expanded_truncated_steps,
    )
    _log_ide_event(
        logger,
        level="INFO",
        surface=surface,
        conversation_id=resolved_conv_id,
        step_count=step_count,
        expanded_truncated_steps=expanded_truncated_steps,
        skipped_reason=None,
        duration_ms=(time.perf_counter() - start) * 1000.0,
    )
    return conv


class AntigravityIdeAdapter:
    """TranscriptAdapter implementation targeting Antigravity IDE JSONL logs."""

    surface: str = "ide"

    def extract(
        self,
        *,
        conversation_id: str,
        transcript_path: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        logger: Optional[RotatingJsonLogger] = None,
    ) -> Optional[NormalizedConversation]:
        """Extract a normalized IDE conversation from transcript.jsonl."""
        _ = db_path
        if transcript_path is None:
            return None
        return extract_ide_conversation(
            transcript_path,
            conversation_id=conversation_id,
            surface_override=self.surface,
            logger=logger,
        )
