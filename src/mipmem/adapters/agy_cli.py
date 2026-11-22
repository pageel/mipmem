"""Read-only Antigravity CLI SQLite transcript harvester with JSONL fallback."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional, Union
from urllib.parse import quote, unquote

from mipmem.adapters.base import NormalizedConversation
from mipmem.core.diagnostics import (
    ADAPTER_READ_FAILED,
    CP_ADP_01,
    RotatingJsonLogger,
)

_MAX_RECENT_PROMPTS = 10
_MAX_PROMPT_CHARS = 500
_MAX_RESPONSE_CHARS = 2000


def _discover_cli_db_path(transcript_path: Optional[Union[str, Path]]) -> Optional[Path]:
    """Walk up from a CLI transcript path to locate conversation_summaries.db."""
    if transcript_path is None:
        return None
    raw = str(transcript_path).strip()
    if not raw:
        return None
    current = Path(raw).expanduser()
    parent = current if current.is_dir() else current.parent
    for _ in range(6):
        candidate = parent / "conversation_summaries.db"
        if candidate.is_file():
            return candidate
        if parent.parent == parent:
            break
        parent = parent.parent
    return None


def _normalize_timestamp(raw: Any) -> str:
    """Convert string ISO or numeric epoch timestamps into ISO-8601 UTC Z format."""
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


def _parse_workspace_uris(raw: Any) -> list[str]:
    """Decode JSON array or delimited workspace_uris into clean filesystem paths."""
    if raw is None:
        return []
    candidates: list[Any]
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
                candidates = decoded if isinstance(decoded, list) else [stripped]
            except json.JSONDecodeError:
                candidates = [part for part in stripped.replace("\n", ",").split(",")]
        else:
            candidates = [part for part in stripped.replace("\n", ",").split(",")]
    else:
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item).strip()
        if not text:
            continue
        if text.startswith("file://"):
            text = unquote(text[7:])
        if text and text not in seen:
            seen.add(text)
            paths.append(text)
    return paths


def _collect_lines(chunks: list[str], *, limit: int) -> list[str]:
    """Extract deduplicated non-empty lines from FTS user_body chunks."""
    items: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        for raw_line in chunk.splitlines():
            cleaned = raw_line.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            items.append(cleaned[:_MAX_PROMPT_CHARS])
    if len(items) > limit:
        return items[-limit:]
    return items


def _safe_int(raw: Any, default: int = 0) -> int:
    """Convert SQLite column values safely into non-negative integers."""
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, int):
        return max(0, raw)
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return default


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Safely read an optional column from a sqlite3.Row."""
    keys = row.keys()
    if key in keys:
        return row[key]
    return default


def _log_adapter_event(
    logger: Optional[RotatingJsonLogger],
    *,
    level: str,
    conversation_id: str,
    source_kind: str,
    step_count: int = 0,
    skipped_reason: Optional[str] = None,
    error_code: Optional[str] = None,
    duration_ms: float = 0.0,
) -> None:
    """Emit structured CP-ADP-01 diagnostics event without raw transcript text."""
    if logger is None:
        return
    logger.log_event(
        level,
        "adapters",
        CP_ADP_01,
        error_code=error_code,
        duration_ms=duration_ms,
        details={
            "surface": "cli",
            "source_kind": source_kind,
            "conversation_id": conversation_id,
            "step_count": step_count,
            "expanded_truncated_steps": 0,
            "skipped_reason": skipped_reason,
        },
    )


def _extract_jsonl_fallback(
    transcript_path: Union[str, Path],
    conversation_id: str,
    *,
    logger: Optional[RotatingJsonLogger] = None,
) -> Optional[NormalizedConversation]:
    """Parse a CLI JSONL transcript file when SQLite summary is unavailable."""
    start = time.perf_counter()
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return None

    prompts: list[str] = []
    last_response = ""
    step_count = 0
    title = ""
    last_modified = ""

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                step_count += 1
                parent_id = str(entry.get("parent_conversation_id") or "").strip()
                nesting_depth = _safe_int(entry.get("nesting_depth"), default=0)
                if nesting_depth > 0 or parent_id:
                    _log_adapter_event(
                        logger,
                        level="INFO",
                        conversation_id=conversation_id,
                        source_kind="jsonl",
                        step_count=step_count,
                        skipped_reason="subagent",
                        duration_ms=(time.perf_counter() - start) * 1000.0,
                    )
                    return None

                ts = _normalize_timestamp(entry.get("timestamp") or entry.get("last_modified_time"))
                if ts:
                    last_modified = ts
                if not title and isinstance(entry.get("title"), str) and entry["title"].strip():
                    title = entry["title"].strip()

                entry_type = str(entry.get("type") or "").upper()
                role = str(entry.get("role") or "").lower()
                if entry_type == "USER_INPUT" or role == "user":
                    text = str(
                        entry.get("text") or entry.get("content") or entry.get("message") or ""
                    ).strip()
                    if text:
                        prompts.append(text[:_MAX_PROMPT_CHARS])
                elif entry_type == "PLANNER_RESPONSE" or role in ("assistant", "model"):
                    resp = str(
                        entry.get("response") or entry.get("text") or entry.get("content") or ""
                    ).strip()
                    if resp:
                        last_response = resp[:_MAX_RESPONSE_CHARS]
    except OSError:
        _log_adapter_event(
            logger,
            level="WARN",
            conversation_id=conversation_id,
            source_kind="jsonl",
            skipped_reason="read_failed",
            error_code=ADAPTER_READ_FAILED,
            duration_ms=(time.perf_counter() - start) * 1000.0,
        )
        return None

    if step_count == 0:
        _log_adapter_event(
            logger,
            level="INFO",
            conversation_id=conversation_id,
            source_kind="jsonl",
            skipped_reason="empty_transcript",
            duration_ms=(time.perf_counter() - start) * 1000.0,
        )
        return None

    if not last_modified:
        try:
            last_modified = _normalize_timestamp(path.stat().st_mtime)
        except OSError:
            last_modified = ""

    resolved_title = title or (prompts[0][:120] if prompts else conversation_id)
    conv = NormalizedConversation(
        conversation_id=conversation_id,
        surface="cli",
        source_kind="jsonl",
        title=resolved_title,
        step_count=step_count,
        last_modified_time=last_modified,
        recent_prompts=prompts[-_MAX_RECENT_PROMPTS:],
        last_agent_response=last_response,
    )
    _log_adapter_event(
        logger,
        level="INFO",
        conversation_id=conversation_id,
        source_kind="jsonl",
        step_count=step_count,
        skipped_reason=None,
        duration_ms=(time.perf_counter() - start) * 1000.0,
    )
    return conv


# @para-doc [#csa-adapter-agy-cli-harvester]
def extract_cli_conversation(
    conversation_id: str,
    *,
    db_path: Optional[Union[str, Path]] = None,
    transcript_path: Optional[Union[str, Path]] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> Optional[NormalizedConversation]:
    """Extract an Antigravity CLI conversation from read-only SQLite or JSONL fallback."""
    start = time.perf_counter()
    conv_id = conversation_id.strip()
    if not conv_id:
        return None

    resolved_db: Optional[Path]
    if db_path is not None:
        resolved_db = Path(db_path).expanduser()
    else:
        resolved_db = _discover_cli_db_path(transcript_path)

    sqlite_attempted = False
    if resolved_db is not None and resolved_db.is_file():
        sqlite_attempted = True
        uri = f"file:{quote(str(resolved_db.resolve()), safe='/:')}?mode=ro"
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            if row is not None:
                parent_id = str(_row_get(row, "parent_conversation_id", "") or "").strip()
                nesting_depth = _safe_int(_row_get(row, "nesting_depth", 0), default=0)
                if nesting_depth > 0 or parent_id:
                    _log_adapter_event(
                        logger,
                        level="INFO",
                        conversation_id=conv_id,
                        source_kind="sqlite",
                        step_count=_safe_int(_row_get(row, "step_count", 0)),
                        skipped_reason="subagent",
                        duration_ms=(time.perf_counter() - start) * 1000.0,
                    )
                    return None

                user_chunks: list[str] = []
                agent_chunks: list[str] = []
                has_fts = (
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'conversations_fts'"
                    ).fetchone()
                    is not None
                )
                if has_fts:
                    fts_rows = conn.execute(
                        "SELECT user_body, agent_body FROM conversations_fts "
                        "WHERE conversation_id = ? ORDER BY chunk_index ASC",
                        (conv_id,),
                    ).fetchall()
                    for fts_row in fts_rows:
                        user_body = str(_row_get(fts_row, "user_body", "") or "").strip()
                        agent_body = str(_row_get(fts_row, "agent_body", "") or "").strip()
                        if user_body:
                            user_chunks.append(user_body)
                        if agent_body:
                            agent_chunks.append(agent_body)

                title = str(_row_get(row, "title", "") or "").strip() or conv_id
                preview = str(_row_get(row, "preview", "") or "").strip()
                step_count = _safe_int(_row_get(row, "step_count", 0))
                last_modified = _normalize_timestamp(_row_get(row, "last_modified_time", ""))
                workspace_paths = _parse_workspace_uris(_row_get(row, "workspace_uris", ""))
                recent_prompts = _collect_lines(user_chunks, limit=_MAX_RECENT_PROMPTS)
                last_agent_response = (
                    agent_chunks[-1][:_MAX_RESPONSE_CHARS]
                    if agent_chunks
                    else preview[:_MAX_RESPONSE_CHARS]
                )

                conv = NormalizedConversation(
                    conversation_id=conv_id,
                    surface="cli",
                    source_kind="sqlite",
                    title=title,
                    step_count=step_count,
                    last_modified_time=last_modified,
                    workspace_paths=workspace_paths,
                    recent_prompts=recent_prompts,
                    last_agent_response=last_agent_response,
                )
                _log_adapter_event(
                    logger,
                    level="INFO",
                    conversation_id=conv_id,
                    source_kind="sqlite",
                    step_count=step_count,
                    skipped_reason=None,
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                )
                return conv
        except (sqlite3.Error, OSError):
            _log_adapter_event(
                logger,
                level="WARN",
                conversation_id=conv_id,
                source_kind="sqlite",
                skipped_reason="read_failed",
                error_code=ADAPTER_READ_FAILED,
                duration_ms=(time.perf_counter() - start) * 1000.0,
            )
        finally:
            if conn is not None:
                conn.close()

    if transcript_path is not None:
        fallback = _extract_jsonl_fallback(transcript_path, conv_id, logger=logger)
        if fallback is not None:
            return fallback

    if sqlite_attempted or resolved_db is not None:
        _log_adapter_event(
            logger,
            level="INFO",
            conversation_id=conv_id,
            source_kind="sqlite",
            skipped_reason="not_found",
            duration_ms=(time.perf_counter() - start) * 1000.0,
        )
    return None


class AntigravityCliAdapter:
    """TranscriptAdapter implementation targeting Antigravity CLI SQLite stores."""

    surface: str = "cli"

    def extract(
        self,
        *,
        conversation_id: str,
        transcript_path: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        logger: Optional[RotatingJsonLogger] = None,
    ) -> Optional[NormalizedConversation]:
        """Extract a normalized CLI conversation via read-only SQLite or JSONL fallback."""
        return extract_cli_conversation(
            conversation_id,
            db_path=db_path,
            transcript_path=transcript_path,
            logger=logger,
        )
