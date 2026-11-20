"""Canonical Session Markdown Frontmatter v1 parser, formatter, and vault writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Optional, Union

from mipmem.core.diagnostics import (
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    RotatingJsonLogger,
    _utc_timestamp_ms,
)
from mipmem.core.vault import atomic_write_vault_file, validate_host_id

VALID_SURFACES: frozenset[str] = frozenset({"cli", "ide", "mcp", "generic"})

_CONV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MONTH_RE = re.compile(r"^(\d{4}-\d{2})")
_KEY_VAL_RE = re.compile(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


@dataclass(frozen=True)
class SessionRecord:
    """Normalized representation of a host session artifact."""

    conversation_id: str
    host_id: str
    surface: str = "generic"
    last_modified_time: str = ""
    step_count: int = 0
    title: str = ""
    edited_files: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    recent_prompts: list[str] = field(default_factory=list)
    last_agent_response: str = ""
    brainstorm_refs: list[str] = field(default_factory=list)
    body: str = ""
    file_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize session record into a JSON-compatible dictionary."""
        return asdict(self)


def normalize_surface(raw_surface: Optional[str]) -> str:
    """Normalize any surface label into the canonical agent-agnostic set."""
    if not isinstance(raw_surface, str):
        return "generic"
    cleaned = raw_surface.strip().lower()
    return cleaned if cleaned in VALID_SURFACES else "generic"


def _clean_str_list(raw: Any) -> list[str]:
    """Coerce a frontmatter field value into a clean deduplicated string list."""
    items: list[str] = []
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
                candidates = [part.strip() for part in stripped.split(",")]
        else:
            candidates = [part.strip() for part in stripped.split(",")]
    else:
        return []

    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate).strip().strip("`")
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _parse_scalar(raw_val: str) -> Any:
    """Parse a flat frontmatter scalar or inline JSON array without PyYAML."""
    stripped = raw_val.strip()
    if not stripped:
        return ""
    if stripped[0] in ('"', "["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
        return stripped[1:-1]
    return stripped


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split a leading flat '---' frontmatter block from the Markdown body."""
    cleaned = content.lstrip("\ufeff")
    lines = cleaned.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, cleaned

    closing_idx: Optional[int] = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing_idx = idx
            break
    if closing_idx is None:
        return {}, cleaned

    header_lines = lines[1:closing_idx]
    body_lines = lines[closing_idx + 1:]
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)
    if cleaned.endswith("\n") and body:
        body += "\n"

    meta: dict[str, Any] = {}
    active_list_key: Optional[str] = None
    for raw_line in header_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if active_list_key is not None and stripped.startswith("- "):
            existing = meta.get(active_list_key)
            if not isinstance(existing, list):
                existing = []
                meta[active_list_key] = existing
            existing.append(str(_parse_scalar(stripped[2:])))
            continue
        match = _KEY_VAL_RE.match(stripped)
        if not match:
            active_list_key = None
            continue
        key = match.group(1).strip().lower()
        raw_value = match.group(2).strip()
        if not raw_value:
            meta[key] = []
            active_list_key = key
        else:
            meta[key] = _parse_scalar(raw_value)
            active_list_key = None

    return meta, body


def _extract_markdown_sections(body: str) -> tuple[str, dict[str, list[str]]]:
    """Extract first outside-fence H1 title and H2 sections from Markdown body."""
    h1_title = ""
    sections: dict[str, list[str]] = {}
    current_h2: Optional[str] = None
    fence_char: Optional[str] = None
    fence_len = 0

    for line in body.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
                fence_len = 0
            if current_h2 is not None:
                sections[current_h2].append(line)
            continue

        if fence_char is not None:
            if current_h2 is not None:
                sections[current_h2].append(line)
            continue

        if not h1_title:
            h1_match = _H1_RE.match(line)
            if h1_match:
                h1_title = h1_match.group(1).strip()
                continue

        h2_match = _H2_RE.match(line)
        if h2_match:
            heading_key = h2_match.group(1).strip().lower()
            current_h2 = heading_key
            sections.setdefault(heading_key, [])
            continue

        if current_h2 is not None:
            sections[current_h2].append(line)

    return h1_title, sections


def _extract_bullets(lines: list[str]) -> list[str]:
    """Extract Markdown bullet items from a section body."""
    items: list[str] = []
    for line in lines:
        match = _BULLET_RE.match(line)
        if match:
            item = match.group(1).strip()
            if item:
                items.append(item)
    return items


def _coerce_int(raw: Any, default: int = 0) -> int:
    """Safely coerce a frontmatter value to a non-negative integer."""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, str):
        try:
            return max(0, int(raw.strip()))
        except ValueError:
            return default
    return default


def _format_scalar(value: str) -> str:
    """Serialize a scalar string safely as plain or JSON-quoted."""
    if not value:
        return '""'
    if re.match(r"^[A-Za-z0-9._:/-]+$", value) and not value.startswith("-"):
        return value
    return json.dumps(value, ensure_ascii=False)


def format_session_markdown(record: SessionRecord) -> str:
    """Serialize a SessionRecord into Canonical Session Markdown Frontmatter v1."""
    surface = normalize_surface(record.surface)
    header_lines = [
        "---",
        "schema_version: 1",
        f"conversation_id: {_format_scalar(record.conversation_id)}",
        f"host_id: {_format_scalar(record.host_id)}",
        f"surface: {surface}",
        f"last_modified_time: {_format_scalar(record.last_modified_time)}",
        f"step_count: {max(0, int(record.step_count))}",
        f"title: {json.dumps(record.title, ensure_ascii=False)}",
        f"edited_files: {json.dumps(list(record.edited_files), ensure_ascii=False)}",
        f"touched_files: {json.dumps(list(record.touched_files), ensure_ascii=False)}",
        f"brainstorm_refs: {json.dumps(list(record.brainstorm_refs), ensure_ascii=False)}",
        "---",
    ]

    if record.body.strip():
        body_text = record.body.lstrip("\n")
    else:
        body_parts: list[str] = []
        heading = record.title.strip() or record.conversation_id
        body_parts.append(f"# {heading}")
        if record.recent_prompts:
            body_parts.append("")
            body_parts.append("## Recent Prompts")
            for prompt in record.recent_prompts:
                body_parts.append(f"- {prompt}")
        if record.last_agent_response.strip():
            body_parts.append("")
            body_parts.append("## Last Agent Response")
            body_parts.append(record.last_agent_response.strip())
        body_text = "\n".join(body_parts) + "\n"

    if not body_text.endswith("\n"):
        body_text += "\n"
    return "\n".join(header_lines) + "\n\n" + body_text


# @para-doc [#csa-session-frontmatter-parser]
def parse_session_markdown(
    content: str,
    *,
    fallback_conversation_id: str = "",
    fallback_host_id: str = "unknown-host",
    fallback_last_modified: str = "",
    file_path: Optional[str] = None,
) -> SessionRecord:
    """Parse Canonical Session Frontmatter v1 with graceful plain-Markdown v2 fallback."""
    meta, body = _split_frontmatter(content)
    h1_title, sections = _extract_markdown_sections(body)

    conversation_id = str(meta.get("conversation_id", "")).strip() or fallback_conversation_id
    if not conversation_id and file_path:
        conversation_id = Path(file_path).stem
    if not conversation_id:
        conversation_id = "session-unknown"

    host_id = str(meta.get("host_id", "")).strip() or fallback_host_id
    surface = normalize_surface(str(meta.get("surface", "generic")))
    last_modified_time = (
        str(meta.get("last_modified_time", "")).strip() or fallback_last_modified
    )
    step_count = _coerce_int(meta.get("step_count"), default=0)

    title = str(meta.get("title", "")).strip() or h1_title or conversation_id
    edited_files = _clean_str_list(meta.get("edited_files"))
    if not edited_files and "edited files" in sections:
        edited_files = _clean_str_list(_extract_bullets(sections["edited files"]))

    touched_files = _clean_str_list(meta.get("touched_files"))
    if not touched_files and "touched files" in sections:
        touched_files = _clean_str_list(_extract_bullets(sections["touched files"]))

    brainstorm_refs = _clean_str_list(meta.get("brainstorm_refs"))
    if not brainstorm_refs and "brainstorm refs" in sections:
        brainstorm_refs = _clean_str_list(_extract_bullets(sections["brainstorm refs"]))

    recent_prompts = _clean_str_list(meta.get("recent_prompts"))
    if not recent_prompts and "recent prompts" in sections:
        recent_prompts = _extract_bullets(sections["recent prompts"])

    last_agent_response = str(meta.get("last_agent_response", "")).strip()
    if not last_agent_response and "last agent response" in sections:
        last_agent_response = "\n".join(sections["last agent response"]).strip()

    return SessionRecord(
        conversation_id=conversation_id,
        host_id=host_id,
        surface=surface,
        last_modified_time=last_modified_time,
        step_count=step_count,
        title=title,
        edited_files=edited_files,
        touched_files=touched_files,
        recent_prompts=recent_prompts,
        last_agent_response=last_agent_response,
        brainstorm_refs=brainstorm_refs,
        body=body,
        file_path=file_path,
    )


def _validate_conversation_id(conversation_id: str) -> str:
    """Ensure conversation_id is a clean single-segment filename stem."""
    if not isinstance(conversation_id, str) or not _CONV_ID_RE.match(conversation_id):
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: invalid conversation_id {conversation_id!r}.",
        )
    if ".." in conversation_id:
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: traversal token in conversation_id {conversation_id!r}.",
        )
    return conversation_id


def _resolve_session_month(last_modified_time: str) -> str:
    """Resolve YYYY-MM partition directory from ISO timestamp or current UTC month."""
    match = _MONTH_RE.match(last_modified_time.strip())
    if match:
        return match.group(1)
    return _utc_timestamp_ms()[:7]


def write_session_record(
    vault_root: Union[str, Path],
    record: SessionRecord,
    *,
    actor_host: Optional[str] = None,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> Path:
    """Write a canonical session artifact into its host shard via S1 atomic redacted I/O."""
    host_id = validate_host_id(actor_host if actor_host is not None else record.host_id)
    conv_id = _validate_conversation_id(record.conversation_id)
    month = _resolve_session_month(record.last_modified_time)
    rel_path = f"hosts/{host_id}/sessions/{month}/{conv_id}.md"
    payload = format_session_markdown(record)
    return atomic_write_vault_file(
        vault_root,
        rel_path,
        payload,
        actor_host=host_id,
        home_dir=home_dir,
        logger=logger,
    )
