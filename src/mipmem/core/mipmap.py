"""4-Layer Mipmap token-budgeted renderer (L0/L1) and surgical L2 section slicer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Mapping, Optional, Sequence, Union

from mipmem.core.brainstorms import BrainstormShard, parse_markdown_ast
from mipmem.core.diagnostics import (
    CP_MIP_01,
    INVALID_SLICE_SELECTOR,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.salience import score_session_salience
from mipmem.core.sessions import SessionRecord
from mipmem.core.workstreams import WorkstreamCluster

DEFAULT_L0_MAX_TOKENS = 300
DEFAULT_L1_MAX_TOKENS = 450

_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_SECTION_SELECTOR_RE = re.compile(r"^§(\d+)$")
_LINE_RANGE_SELECTOR_RE = re.compile(r"^#?L(\d+)(?:-L?(\d+))?$")
_TRUNCATION_MARKER = "... [truncated]"


@dataclass(frozen=True)
class MipmapOutput:
    """Rendered L0 or L1 markdown payload with strict token-budget telemetry."""

    layer: str
    content: str
    est_tokens: int
    budget_tokens: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize mipmap output into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class SliceResult:
    """Surgical L2 section/line slice or full L3 brainstorm content."""

    bs_id: str
    selector: str
    layer: str
    start_line: int
    end_line: int
    total_lines: int
    est_tokens: int
    content: str
    section_title: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize slice result into a JSON-compatible dictionary."""
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Deterministic zero-dependency token estimate for Markdown and EN/VI text."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4), int(len(text.split()) * 1.35))


def _resolve_budget(
    explicit_max: Optional[int],
    config: Optional[Mapping[str, Any]],
    key: str,
    default_value: int,
) -> int:
    """Resolve positive integer token budget from explicit argument or config mapping."""
    if explicit_max is not None:
        return max(1, int(explicit_max))
    if isinstance(config, Mapping):
        budget_section = config.get("budget")
        if isinstance(budget_section, Mapping):
            raw = budget_section.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return default_value


def _fence_state_after_lines(lines: Sequence[str]) -> Optional[str]:
    """Return closing fence marker (e.g. '```') if lines leave a code fence open, else None."""
    fence_char: Optional[str] = None
    fence_len = 0
    for line in lines:
        if fence_char is None:
            open_match = _FENCE_OPEN_RE.match(line)
            if open_match:
                marker = open_match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
        else:
            close_match = _FENCE_CLOSE_RE.match(line)
            if close_match:
                marker = close_match.group(1)
                if marker[0] == fence_char and len(marker) >= fence_len:
                    fence_char = None
                    fence_len = 0
    if fence_char is None:
        return None
    return fence_char * fence_len


def _finalize_truncated_lines(kept: list[str], max_tokens: int) -> str:
    """Close any open code fence, append truncation marker, and guarantee <= max_tokens."""
    working = list(kept)
    while True:
        candidate = list(working)
        open_closer = _fence_state_after_lines(candidate)
        if open_closer is not None:
            # Avoid emitting an empty opened+closed fence when truncated right at opener
            if candidate and _FENCE_OPEN_RE.match(candidate[-1]):
                working.pop()
                continue
            candidate.append(open_closer)
        candidate.append(_TRUNCATION_MARKER)
        rendered = "\n".join(candidate).rstrip() + "\n"
        if estimate_tokens(rendered) <= max_tokens:
            return rendered
        if not working:
            break
        working.pop()

    # Extreme tiny budget fallback: trim marker cleanly to fit max_tokens
    if estimate_tokens(_TRUNCATION_MARKER) <= max_tokens:
        return _TRUNCATION_MARKER + "\n"
    char_cap = max(1, max_tokens * 3)
    return _TRUNCATION_MARKER[:char_cap]


def _truncate_to_token_budget(lines: Sequence[str], max_tokens: int) -> tuple[str, bool]:
    """Enforce hard token budget along line boundaries without leaving unclosed code fences."""
    full_text = "\n".join(lines).rstrip() + "\n" if lines else ""
    if not full_text:
        return "", False

    full_open = _fence_state_after_lines(lines)
    if full_open is None and estimate_tokens(full_text) <= max_tokens:
        return full_text, False

    kept: list[str] = []
    for line in lines:
        trial = kept + [line]
        closer = _fence_state_after_lines(trial)
        preview = list(trial)
        if closer is not None:
            preview.append(closer)
        preview.append(_TRUNCATION_MARKER)
        preview_text = "\n".join(preview).rstrip() + "\n"
        if estimate_tokens(preview_text) <= max_tokens:
            kept.append(line)
        else:
            break

    rendered = _finalize_truncated_lines(kept, max_tokens)
    return rendered, True


def _emit_mip_checkpoint(
    logger: Optional[RotatingJsonLogger],
    *,
    layer: str,
    est_tokens: int,
    budget_tokens: int,
    truncated: bool,
    conversation_id: str = "",
    novelty_score: float = 0.0,
    triggered: bool = True,
) -> None:
    """Emit structured CP-MIP-01 telemetry when a logger is supplied."""
    if logger is None:
        return
    logger.log_event(
        "INFO",
        "mipmap",
        CP_MIP_01,
        details={
            "layer": layer,
            "est_tokens": est_tokens,
            "budget_tokens": budget_tokens,
            "truncated": truncated,
            "conversation_id": conversation_id,
            "novelty_score": round(novelty_score, 4),
            "triggered": triggered,
        },
    )


# @para-doc [#csa-mipmap-l0-l1-renderer]
def render_l0_radar(
    sessions: Sequence[SessionRecord] = (),
    brainstorms: Sequence[BrainstormShard] = (),
    workstreams: Sequence[WorkstreamCluster] = (),
    *,
    max_tokens: Optional[int] = None,
    config: Optional[Mapping[str, Any]] = None,
    locale: str = "en",
    logger: Optional[RotatingJsonLogger] = None,
) -> MipmapOutput:
    """Render a compact L0 startup radar strictly bounded by l0_max_tokens (default 300)."""
    budget = _resolve_budget(max_tokens, config, "l0_max_tokens", DEFAULT_L0_MAX_TOKENS)
    lines: list[str] = ["# MipMem L0 Radar"]

    if workstreams:
        lines.append("## Workstreams")
        for cluster in workstreams:
            hosts = ",".join(cluster.hosts[:3])
            lines.append(
                f"- [{cluster.workstream_id}] {cluster.label} "
                f"(hosts={hosts}; primary={len(cluster.primary_session_ids)}; "
                f"score={cluster.top_salience_score:.0f})"
            )

    if sessions:
        scored = [
            (score_session_salience(rec, locale=locale), rec)
            for rec in sessions
        ]
        scored.sort(
            key=lambda pair: (pair[0].score, pair[1].last_modified_time, pair[1].conversation_id),
            reverse=True,
        )
        lines.append("## Sessions")
        for sal, rec in scored:
            ts_short = rec.last_modified_time[:10] if rec.last_modified_time else "n/a"
            lines.append(
                f"- `{rec.conversation_id}` [{sal.tier}/{sal.score:.0f}] "
                f"({rec.host_id}, {ts_short}) {rec.title}"
            )

    if brainstorms:
        ordered_bs = sorted(
            brainstorms,
            key=lambda shard: (shard.updated_at, shard.bs_id),
            reverse=True,
        )
        lines.append("## Brainstorms")
        for shard in ordered_bs:
            scent = shard.l0_scent[:90]
            lines.append(
                f"- `{shard.bs_id}` [{shard.status}] ({shard.origin_host}) "
                f"{shard.title}: {scent}"
            )

    rendered, truncated = _truncate_to_token_budget(lines, budget)
    est = estimate_tokens(rendered)
    _emit_mip_checkpoint(
        logger,
        layer="L0",
        est_tokens=est,
        budget_tokens=budget,
        truncated=truncated,
    )
    return MipmapOutput(
        layer="L0",
        content=rendered,
        est_tokens=est,
        budget_tokens=budget,
        truncated=truncated,
    )


def render_l1_skeleton(
    target: Union[BrainstormShard, SessionRecord],
    *,
    max_tokens: Optional[int] = None,
    config: Optional[Mapping[str, Any]] = None,
    locale: str = "en",
    effective_status: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> MipmapOutput:
    """Render an L1 structural card with #Lstart-Lend pointers bounded by l1_max_tokens (450)."""
    budget = _resolve_budget(max_tokens, config, "l1_max_tokens", DEFAULT_L1_MAX_TOKENS)
    lines: list[str] = []
    conv_id = ""

    if isinstance(target, BrainstormShard):
        status_label = effective_status if effective_status is not None else target.status
        lines.append(f"# [L1] {target.title}")
        lines.append(
            f"- id: `{target.bs_id}` | status: `{status_label}` | "
            f"host: `{target.origin_host}` | lines: {target.line_count} | "
            f"est_tokens: {target.est_tokens}"
        )
        if target.l0_scent:
            lines.append(f"- scent: {target.l0_scent}")
        if target.tags:
            lines.append(f"- tags: {', '.join(target.tags[:8])}")
        if target.decisions:
            lines.append("## Decisions")
            for decision in target.decisions[:6]:
                lines.append(f"- {decision}")
        if target.toc:
            lines.append("## Outline")
            for sec in target.toc:
                indent = "  " * max(0, sec.level - 2)
                lines.append(
                    f"{indent}- §{sec.index} {sec.title} (#L{sec.start_line}-L{sec.end_line})"
                )
    else:
        conv_id = target.conversation_id
        sal = score_session_salience(target, locale=locale)
        lines.append(f"# [L1] {target.title or target.conversation_id}")
        lines.append(
            f"- conversation_id: `{target.conversation_id}` | host: `{target.host_id}` | "
            f"surface: `{target.surface}` | tier: `{sal.tier}` ({sal.score:.1f}) | "
            f"steps: {target.step_count}"
        )
        if target.last_modified_time:
            lines.append(f"- updated: {target.last_modified_time}")
        if target.edited_files:
            lines.append(f"- edited_files: {', '.join(target.edited_files[:8])}")
        if target.touched_files:
            lines.append(f"- touched_files: {', '.join(target.touched_files[:6])}")
        if target.recent_prompts:
            lines.append("## Recent Prompts")
            for prompt in target.recent_prompts[:5]:
                lines.append(f"- {prompt}")
        if target.last_agent_response.strip():
            lines.append("## Last Agent Response")
            lines.extend(target.last_agent_response.strip().splitlines())
        if target.body.strip():
            lines.append("## Excerpt")
            lines.extend(target.body.strip().splitlines())

    rendered, truncated = _truncate_to_token_budget(lines, budget)
    est = estimate_tokens(rendered)
    _emit_mip_checkpoint(
        logger,
        layer="L1",
        est_tokens=est,
        budget_tokens=budget,
        truncated=truncated,
        conversation_id=conv_id,
    )
    return MipmapOutput(
        layer="L1",
        content=rendered,
        est_tokens=est,
        budget_tokens=budget,
        truncated=truncated,
    )


# @para-doc [#csa-mipmap-l2-section-slicer]
def slice_brainstorm_l2(
    content: str,
    *,
    selector: Optional[str] = None,
    bs_id: str = "brainstorm",
) -> SliceResult:
    """Slice a brainstorm by §k section or #Lstart-Lend range (L2), or return full text (L3)."""
    lines = content.splitlines()
    total_lines = len(lines)

    if selector is None or not selector.strip():
        return SliceResult(
            bs_id=bs_id,
            selector="L3",
            layer="L3",
            start_line=1 if total_lines > 0 else 0,
            end_line=total_lines,
            total_lines=total_lines,
            est_tokens=estimate_tokens(content),
            content=content,
            section_title=None,
        )

    cleaned = selector.strip()
    sec_match = _SECTION_SELECTOR_RE.match(cleaned)
    if sec_match:
        sec_idx = int(sec_match.group(1))
        ast = parse_markdown_ast(content)
        if sec_idx < 1 or sec_idx > len(ast.toc):
            raise MipmemError(
                INVALID_SLICE_SELECTOR,
                f"Invalid slice selector '{selector}' for '{bs_id}'.",
            )
        target_sec = ast.toc[sec_idx - 1]
        sliced_lines = lines[target_sec.start_line - 1:target_sec.end_line]
        sliced_text = "\n".join(sliced_lines) + ("\n" if sliced_lines else "")
        return SliceResult(
            bs_id=bs_id,
            selector=cleaned,
            layer="L2",
            start_line=target_sec.start_line,
            end_line=target_sec.end_line,
            total_lines=total_lines,
            est_tokens=estimate_tokens(sliced_text),
            content=sliced_text,
            section_title=target_sec.title,
        )

    line_match = _LINE_RANGE_SELECTOR_RE.match(cleaned)
    if line_match:
        start_line = int(line_match.group(1))
        end_line = int(line_match.group(2)) if line_match.group(2) is not None else start_line
        if total_lines == 0 or start_line < 1 or end_line < start_line or end_line > total_lines:
            raise MipmemError(
                INVALID_SLICE_SELECTOR,
                f"Invalid slice selector '{selector}' for '{bs_id}'.",
            )
        sliced_lines = lines[start_line - 1:end_line]
        sliced_text = "\n".join(sliced_lines) + ("\n" if sliced_lines else "")
        return SliceResult(
            bs_id=bs_id,
            selector=cleaned,
            layer="L2",
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            est_tokens=estimate_tokens(sliced_text),
            content=sliced_text,
            section_title=None,
        )

    raise MipmemError(
        INVALID_SLICE_SELECTOR,
        f"Invalid slice selector '{selector}' for '{bs_id}'.",
    )
