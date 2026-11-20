"""Fence-Aware Markdown Brainstorm AST parser and sharded Vault storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Optional, Sequence, Union
import uuid

from mipmem.core.diagnostics import (
    BRAINSTORM_NOT_FOUND,
    CLI_INVALID_ARGUMENT,
    INVALID_BRAINSTORM_STATUS,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    RotatingJsonLogger,
    _utc_timestamp_ms,
)
from mipmem.core.vault import (
    append_mutation_event,
    atomic_write_vault_file,
    validate_host_id,
)

VALID_BRAINSTORM_STATUSES: frozenset[str] = frozenset(
    {"open", "decided", "implementing", "shipped", "archived"}
)
MAX_BRAINSTORM_BYTES = 5 * 1024 * 1024

_BS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DECISION_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+|>\s*)?(?:\*{0,2})(decision|decided|ch[oố]t|quy[eế]t\s+[dđ][iị]nh)"
    r"(?:\*{0,2})\s*:\s*(.+)$",
    re.IGNORECASE,
)
_TAGS_LINE_RE = re.compile(r"^\s*(?:>\s*)?(?:\*{0,2})tags(?:\*{0,2})\s*:\s*(.+)$", re.IGNORECASE)
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-z][a-z0-9_-]{1,31})\b")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class TocSection:
    """One navigable Markdown heading section with 1-based inclusive line bounds."""

    index: int
    level: int
    title: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize TOC section into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class BrainstormAst:
    """Lossless structural summary extracted from a Markdown brainstorm document."""

    title: str
    line_count: int
    est_tokens: int
    l0_scent: str
    decisions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    toc: list[TocSection] = field(default_factory=list)
    skipped_document_h1: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize AST into a JSON-compatible dictionary."""
        return {
            "title": self.title,
            "line_count": self.line_count,
            "est_tokens": self.est_tokens,
            "l0_scent": self.l0_scent,
            "decisions": list(self.decisions),
            "tags": list(self.tags),
            "toc": [sec.to_dict() for sec in self.toc],
            "skipped_document_h1": self.skipped_document_h1,
        }


@dataclass(frozen=True)
class BrainstormShard:
    """Canonical representation of a sharded brainstorm note and its sidecar metadata."""

    bs_id: str
    title: str
    origin_host: str
    status: str
    line_count: int
    est_tokens: int
    sha256: str
    created_at: str
    updated_at: str
    l0_scent: str
    decisions: list[str]
    tags: list[str]
    toc: list[TocSection]
    md_rel_path: str
    meta_rel_path: str
    md_path: Path
    meta_path: Path

    def to_meta_dict(self) -> dict[str, Any]:
        """Serialize extended v1 .meta.json payload preserving all 9 S1 base fields."""
        return {
            "schema_version": 1,
            "bs_id": self.bs_id,
            "title": self.title,
            "origin_host": self.origin_host,
            "status": self.status,
            "line_count": self.line_count,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "est_tokens": self.est_tokens,
            "l0_scent": self.l0_scent,
            "decisions": list(self.decisions),
            "tags": list(self.tags),
            "toc": [sec.to_dict() for sec in self.toc],
        }


def _estimate_text_tokens(text: str) -> int:
    """Deterministic zero-dependency token estimate aligned with mipmap.estimate_tokens."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4), int(len(text.split()) * 1.35))


def validate_brainstorm_status(status: str) -> str:
    """Validate and normalize brainstorm status or raise INVALID_BRAINSTORM_STATUS."""
    cleaned = status.strip().lower() if isinstance(status, str) else ""
    if cleaned not in VALID_BRAINSTORM_STATUSES:
        raise MipmemError(
            INVALID_BRAINSTORM_STATUS,
            f"Invalid brainstorm status '{status}'. "
            "Allowed: open, decided, implementing, shipped, archived.",
        )
    return cleaned


def validate_brainstorm_id(bs_id: str) -> str:
    """Validate brainstorm identifier to prevent path traversal and invalid stems."""
    if not isinstance(bs_id, str) or not _BS_ID_RE.match(bs_id) or ".." in bs_id:
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: invalid brainstorm id {bs_id!r}.",
        )
    return bs_id


def _slugify_title(title: str) -> str:
    """Build a compact ASCII kebab-case slug from a brainstorm title."""
    lowered = title.strip().lower()
    slug = _SLUG_STRIP_RE.sub("-", lowered).strip("-")
    return (slug[:40].strip("-")) or "note"


# @para-doc [#csa-brainstorm-ast-parser]
def parse_markdown_ast(
    content: str,
    *,
    fallback_title: str = "",
) -> BrainstormAst:
    """Parse Markdown headings outside code fences, skipping a single H1 doc title for §1."""
    lines = content.splitlines()
    total_lines = len(lines)

    raw_headings: list[tuple[int, str, int]] = []
    decisions: list[str] = []
    tags: list[str] = []
    seen_tags: set[str] = set()
    first_prose_line = ""

    fence_char: Optional[str] = None
    fence_len = 0

    for line_no, raw_line in enumerate(lines, start=1):
        if fence_char is None:
            open_match = _FENCE_OPEN_RE.match(raw_line)
            if open_match:
                marker = open_match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
                continue
        else:
            close_match = _FENCE_CLOSE_RE.match(raw_line)
            if close_match:
                marker = close_match.group(1)
                if marker[0] == fence_char and len(marker) >= fence_len:
                    fence_char = None
                    fence_len = 0
            continue

        heading_match = _HEADING_RE.match(raw_line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_title = heading_match.group(2).strip()
            raw_headings.append((level, heading_title, line_no))
            continue

        stripped = raw_line.strip()
        if not stripped:
            continue

        decision_match = _DECISION_LINE_RE.match(raw_line)
        if decision_match:
            decision_text = decision_match.group(2).strip()
            if decision_text:
                decisions.append(decision_text)

        tags_match = _TAGS_LINE_RE.match(raw_line)
        if tags_match:
            for raw_tag in tags_match.group(1).split(","):
                cleaned_tag = raw_tag.strip().lstrip("#").lower()
                if cleaned_tag and cleaned_tag not in seen_tags:
                    seen_tags.add(cleaned_tag)
                    tags.append(cleaned_tag)

        for inline_tag in _INLINE_TAG_RE.findall(raw_line):
            lowered_tag = inline_tag.lower()
            if (
                not lowered_tag.startswith("csa-")
                and not re.match(r"^l\d+$", lowered_tag)
                and lowered_tag not in seen_tags
            ):
                seen_tags.add(lowered_tag)
                tags.append(lowered_tag)

        if not first_prose_line and not stripped.startswith(("---", "===", "|")):
            cleaned_prose = re.sub(r"^[>*\-\s]+", "", stripped).strip()
            if cleaned_prose:
                first_prose_line = cleaned_prose

    h1_headings = [item for item in raw_headings if item[0] == 1]
    if h1_headings:
        doc_title = h1_headings[0][1]
    elif raw_headings:
        doc_title = raw_headings[0][1]
    else:
        doc_title = fallback_title.strip() or "Untitled Brainstorm"

    skip_single_h1 = (
        len(h1_headings) == 1 and len(raw_headings) > 1 and raw_headings[0][0] == 1
    )
    toc_raw = raw_headings[1:] if skip_single_h1 else raw_headings

    toc_sections: list[TocSection] = []
    for idx, (level, title, start_line) in enumerate(toc_raw):
        end_line = total_lines
        for next_level, _next_title, next_start in toc_raw[idx + 1:]:
            if next_level <= level:
                end_line = next_start - 1
                break
        if end_line < start_line:
            end_line = start_line
        toc_sections.append(
            TocSection(
                index=idx + 1,
                level=level,
                title=title,
                start_line=start_line,
                end_line=end_line,
            )
        )

    if first_prose_line:
        l0_scent = first_prose_line[:180].rstrip()
    elif toc_sections:
        l0_scent = "; ".join(sec.title for sec in toc_sections[:4])[:180]
    else:
        l0_scent = doc_title[:180]

    return BrainstormAst(
        title=doc_title,
        line_count=total_lines,
        est_tokens=_estimate_text_tokens(content),
        l0_scent=l0_scent,
        decisions=decisions,
        tags=tags,
        toc=toc_sections,
        skipped_document_h1=skip_single_h1,
    )


def _normalize_loaded_status(raw_status: Any) -> str:
    """Normalize persisted or migrated status values into the S2 lifecycle set."""
    cleaned = str(raw_status).strip().lower() if raw_status is not None else "open"
    if cleaned == "superseded":
        return "archived"
    return cleaned if cleaned in VALID_BRAINSTORM_STATUSES else "open"


def _parse_toc_from_meta(raw_toc: Any) -> Optional[list[TocSection]]:
    """Decode TocSection list from .meta.json if present and well-formed."""
    if not isinstance(raw_toc, list):
        return None
    parsed: list[TocSection] = []
    for item in raw_toc:
        if not isinstance(item, dict):
            return None
        try:
            parsed.append(
                TocSection(
                    index=int(item["index"]),
                    level=int(item["level"]),
                    title=str(item["title"]),
                    start_line=int(item["start_line"]),
                    end_line=int(item["end_line"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return parsed


def _build_shard_from_disk(
    vault_resolved: Path,
    md_path: Path,
    origin_host: str,
) -> tuple[BrainstormShard, str]:
    """Read one brainstorm .md and optional .meta.json, deriving missing S2 fields on-the-fly."""
    md_text = md_path.read_text(encoding="utf-8")
    meta_path = md_path.with_suffix(".meta.json")
    raw_meta: dict[str, Any] = {}
    if meta_path.is_file() and not meta_path.is_symlink():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_meta = loaded
        except (OSError, json.JSONDecodeError):
            raw_meta = {}

    ast = parse_markdown_ast(md_text, fallback_title=md_path.stem)
    parsed_toc = _parse_toc_from_meta(raw_meta.get("toc"))
    toc = parsed_toc if parsed_toc is not None else ast.toc

    raw_bs_id = str(raw_meta.get("bs_id", "")).strip() or md_path.stem
    title = str(raw_meta.get("title", "")).strip() or ast.title
    status = _normalize_loaded_status(raw_meta.get("status", "open"))
    line_count = (
        int(raw_meta["line_count"])
        if isinstance(raw_meta.get("line_count"), int)
        else ast.line_count
    )
    est_tokens = (
        int(raw_meta["est_tokens"])
        if isinstance(raw_meta.get("est_tokens"), int)
        else ast.est_tokens
    )
    sha256_hex = (
        str(raw_meta.get("sha256", "")).strip()
        or hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    )
    now_fallback = _utc_timestamp_ms()
    created_at = str(raw_meta.get("created_at", "")).strip() or now_fallback
    updated_at = str(raw_meta.get("updated_at", "")).strip() or created_at
    l0_scent = str(raw_meta.get("l0_scent", "")).strip() or ast.l0_scent

    raw_decisions = raw_meta.get("decisions")
    decisions = (
        [str(item) for item in raw_decisions if str(item).strip()]
        if isinstance(raw_decisions, list)
        else ast.decisions
    )
    raw_tags = raw_meta.get("tags")
    tags = (
        [str(item).strip().lower() for item in raw_tags if str(item).strip()]
        if isinstance(raw_tags, list)
        else ast.tags
    )

    md_rel = md_path.relative_to(vault_resolved).as_posix()
    meta_rel = meta_path.relative_to(vault_resolved).as_posix()

    shard = BrainstormShard(
        bs_id=raw_bs_id,
        title=title,
        origin_host=str(raw_meta.get("origin_host", "")).strip() or origin_host,
        status=status,
        line_count=line_count,
        est_tokens=est_tokens,
        sha256=sha256_hex,
        created_at=created_at,
        updated_at=updated_at,
        l0_scent=l0_scent,
        decisions=decisions,
        tags=tags,
        toc=toc,
        md_rel_path=md_rel,
        meta_rel_path=meta_rel,
        md_path=md_path,
        meta_path=meta_path,
    )
    return shard, md_text


def discover_brainstorm_shards(vault_root: Union[str, Path]) -> list[BrainstormShard]:
    """Scan all host shards under shared/brainstorms/by-host/ safely without following symlinks."""
    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    vault_resolved = root_candidate.resolve()
    by_host_dir = vault_resolved / "shared" / "brainstorms" / "by-host"
    if not by_host_dir.exists() or not by_host_dir.is_dir() or by_host_dir.is_symlink():
        return []

    shards: list[BrainstormShard] = []
    for host_dir in sorted(by_host_dir.iterdir()):
        if not host_dir.is_dir() or host_dir.is_symlink():
            continue
        origin_host = host_dir.name
        for md_path in sorted(host_dir.glob("*.md")):
            if not md_path.is_file() or md_path.is_symlink():
                continue
            resolved_md = md_path.resolve()
            if not resolved_md.is_relative_to(vault_resolved):
                continue
            shard, _ = _build_shard_from_disk(vault_resolved, resolved_md, origin_host)
            shards.append(shard)

    shards.sort(key=lambda item: (item.updated_at, item.bs_id), reverse=True)
    return shards


def load_brainstorm_shard(
    vault_root: Union[str, Path],
    bs_id: str,
) -> tuple[BrainstormShard, str]:
    """Load one brainstorm shard and its L3 Markdown content by bs_id across all host shards."""
    clean_id = validate_brainstorm_id(bs_id)
    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    vault_resolved = root_candidate.resolve()
    by_host_dir = vault_resolved / "shared" / "brainstorms" / "by-host"
    if by_host_dir.is_dir() and not by_host_dir.is_symlink():
        for host_dir in sorted(by_host_dir.iterdir()):
            if not host_dir.is_dir() or host_dir.is_symlink():
                continue
            for md_path in sorted(host_dir.glob("*.md")):
                if not md_path.is_file() or md_path.is_symlink():
                    continue
                resolved_md = md_path.resolve()
                if not resolved_md.is_relative_to(vault_resolved):
                    continue
                shard, md_text = _build_shard_from_disk(
                    vault_resolved, resolved_md, host_dir.name
                )
                if shard.bs_id == clean_id or md_path.stem == clean_id:
                    return shard, md_text

    raise MipmemError(
        BRAINSTORM_NOT_FOUND,
        f"Brainstorm '{clean_id}' not found.",
    )


# @para-doc [#csa-brainstorm-shard-store]
def save_brainstorm_shard(
    vault_root: Union[str, Path],
    title: str,
    content: str,
    *,
    host_id: str,
    status: str = "open",
    bs_id: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> BrainstormShard:
    """Save a lossless L3 brainstorm and extended .meta.json into the host shard via S1 I/O."""
    validated_host = validate_host_id(host_id)
    validated_status = validate_brainstorm_status(status)

    encoded_size = len(content.encode("utf-8"))
    if encoded_size > MAX_BRAINSTORM_BYTES:
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Brainstorm content exceeds {MAX_BRAINSTORM_BYTES} bytes limit.",
        )

    now_utc = _utc_timestamp_ms()
    date_prefix = now_utc[:10]
    if bs_id is not None:
        resolved_id = validate_brainstorm_id(bs_id)
    else:
        slug = _slugify_title(title)
        resolved_id = f"{date_prefix}_{slug}_{uuid.uuid4().hex[:8]}"

    md_rel = f"shared/brainstorms/by-host/{validated_host}/{resolved_id}.md"
    meta_rel = f"shared/brainstorms/by-host/{validated_host}/{resolved_id}.meta.json"

    md_path = atomic_write_vault_file(
        vault_root,
        md_rel,
        content,
        actor_host=validated_host,
        home_dir=home_dir,
        logger=logger,
    )
    redacted_md = md_path.read_text(encoding="utf-8")
    ast = parse_markdown_ast(redacted_md, fallback_title=title)
    resolved_title = title.strip() or ast.title

    merged_tags: list[str] = []
    seen_tags: set[str] = set()
    for candidate in (*(tags or ()), *ast.tags):
        cleaned_tag = str(candidate).strip().lstrip("#").lower()
        if cleaned_tag and cleaned_tag not in seen_tags:
            seen_tags.add(cleaned_tag)
            merged_tags.append(cleaned_tag)

    sha256_hex = hashlib.sha256(redacted_md.encode("utf-8")).hexdigest()
    meta_path_preview = Path(vault_root).expanduser().resolve() / meta_rel
    shard = BrainstormShard(
        bs_id=resolved_id,
        title=resolved_title,
        origin_host=validated_host,
        status=validated_status,
        line_count=ast.line_count,
        est_tokens=ast.est_tokens,
        sha256=sha256_hex,
        created_at=now_utc,
        updated_at=now_utc,
        l0_scent=ast.l0_scent,
        decisions=ast.decisions,
        tags=merged_tags,
        toc=ast.toc,
        md_rel_path=md_rel,
        meta_rel_path=meta_rel,
        md_path=md_path,
        meta_path=meta_path_preview,
    )

    written_meta = atomic_write_vault_file(
        vault_root,
        meta_rel,
        json.dumps(shard.to_meta_dict(), indent=2, ensure_ascii=False) + "\n",
        actor_host=validated_host,
        home_dir=home_dir,
        logger=logger,
    )
    return BrainstormShard(
        bs_id=shard.bs_id,
        title=shard.title,
        origin_host=shard.origin_host,
        status=shard.status,
        line_count=shard.line_count,
        est_tokens=shard.est_tokens,
        sha256=shard.sha256,
        created_at=shard.created_at,
        updated_at=shard.updated_at,
        l0_scent=shard.l0_scent,
        decisions=shard.decisions,
        tags=shard.tags,
        toc=shard.toc,
        md_rel_path=shard.md_rel_path,
        meta_rel_path=shard.meta_rel_path,
        md_path=md_path,
        meta_path=written_meta,
    )


def update_brainstorm_status(
    vault_root: Union[str, Path],
    bs_id: str,
    new_status: str,
    *,
    actor_host: str,
    reason: str = "",
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Record a conflict-free brainstorm status mutation in actor_host's journal."""
    validated_host = validate_host_id(actor_host)
    validated_status = validate_brainstorm_status(new_status)
    shard, _ = load_brainstorm_shard(vault_root, bs_id)

    payload: dict[str, Any] = {"status": validated_status}
    if reason.strip():
        payload["reason"] = reason.strip()

    return append_mutation_event(
        vault_root,
        validated_host,
        "brainstorm_status",
        shard.bs_id,
        payload,
        home_dir=home_dir,
        logger=logger,
    )
