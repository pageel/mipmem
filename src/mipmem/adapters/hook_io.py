"""Unified Antigravity Hook IO renderer, fail-open runner, and event handlers."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Optional, Union

from mipmem.core.diagnostics import (
    CP_HK_01,
    HOOK_INVALID_PAYLOAD,
    RotatingJsonLogger,
)

MAX_HOOK_STDIN_BYTES = 4 * 1024 * 1024


def _detect_surface(transcript_path: Optional[Any]) -> str:
    """Lightweight surface detector avoiding core.sessions import on fast skip paths."""
    if transcript_path is None:
        return "generic"
    raw = str(transcript_path).strip()
    if not raw:
        return "generic"
    segments = [part.strip().lower() for part in raw.replace("\\", "/").split("/") if part.strip()]
    for seg in segments:
        if seg == "antigravity-cli":
            return "cli"
    for seg in segments:
        if seg in ("antigravity", "antigravity-ide"):
            return "ide"
    return "generic"


# @para-doc [#csa-hook-unified-renderer]
def render_user_markdown_response(markdown_body: str) -> dict[str, Any]:
    """Build a 0-LLM-token terminated hook response rendered cleanly on CLI and IDE."""
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "",
                    "metadata": {
                        "user_markdown": markdown_body,
                    },
                }
            }
        ],
        "terminationBehavior": "terminate",
    }


def render_system_injection(message: str) -> dict[str, Any]:
    """Build a non-terminating systemMessage step injection for L0/L1 context."""
    if not isinstance(message, str) or not message.strip():
        return {}
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": message,
                }
            }
        ]
    }


def _emit_hook_checkpoint(
    logger: Optional[RotatingJsonLogger],
    *,
    level: str,
    event: str,
    surface: str,
    policy: str,
    action: str,
    tokens_injected: int = 0,
    duration_ms: float = 0.0,
    error_code: Optional[str] = None,
    details_extra: Optional[dict[str, Any]] = None,
) -> None:
    """Emit structured CP-HK-01 telemetry via RotatingJsonLogger."""
    if logger is None:
        return
    details: dict[str, Any] = {
        "event": event,
        "surface": surface,
        "policy": policy,
        "action": action,
        "tokens_injected": tokens_injected,
        "duration_ms": round(duration_ms, 3),
    }
    if details_extra:
        details.update(details_extra)
    logger.log_event(
        level,
        "hook_io",
        CP_HK_01,
        error_code=error_code,
        duration_ms=duration_ms,
        details=details,
    )


def _read_bounded_stdin(stream: Any, max_bytes: int) -> Optional[bytes]:
    """Read up to max_bytes from a text or binary stream; return None if exceeded."""
    chunk = stream.read(max_bytes + 1)
    if isinstance(chunk, str):
        raw_bytes = chunk.encode("utf-8", errors="replace")
    elif isinstance(chunk, (bytes, bytearray)):
        raw_bytes = bytes(chunk)
    else:
        return b""
    if len(raw_bytes) > max_bytes:
        return None
    return raw_bytes


def read_hook_payload(
    stdin_stream: Optional[Any] = None,
    *,
    preloaded_bytes: Optional[bytes] = None,
    max_bytes: int = MAX_HOOK_STDIN_BYTES,
    event: str = "unknown",
    logger: Optional[RotatingJsonLogger] = None,
) -> Optional[dict[str, Any]]:
    """Read and parse bounded JSON object payload from hook stdin."""
    started = time.perf_counter()
    if preloaded_bytes is not None:
        raw_bytes: Optional[bytes] = (
            None if len(preloaded_bytes) > max_bytes else preloaded_bytes
        )
    else:
        stream = stdin_stream
        if stream is None:
            stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw_bytes = _read_bounded_stdin(stream, max_bytes)
    if raw_bytes is None:
        _emit_hook_checkpoint(
            logger,
            level="WARN",
            event=event,
            surface="generic",
            policy="unknown",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=HOOK_INVALID_PAYLOAD,
            details_extra={"reason": "payload_exceeds_4mb"},
        )
        return None

    stripped = raw_bytes.strip()
    if not stripped:
        return {}

    try:
        decoded = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _emit_hook_checkpoint(
            logger,
            level="WARN",
            event=event,
            surface="generic",
            policy="unknown",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=HOOK_INVALID_PAYLOAD,
            details_extra={"reason": "invalid_json"},
        )
        return None

    if not isinstance(decoded, dict):
        _emit_hook_checkpoint(
            logger,
            level="WARN",
            event=event,
            surface="generic",
            policy="unknown",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=HOOK_INVALID_PAYLOAD,
            details_extra={"reason": "non_object_payload"},
        )
        return None
    return decoded


def _extract_workspace_paths(payload: dict[str, Any]) -> list[str]:
    """Extract normalized workspace path candidates from hook stdin payload."""
    raw_ws = payload.get("workspacePaths")
    paths: list[str] = []
    if isinstance(raw_ws, list):
        for item in raw_ws:
            text = str(item).strip()
            if text:
                paths.append(text)
    if not paths:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            paths.append(cwd.strip())
    return paths


def _resolve_hook_runtime(
    vault_root: Optional[Union[str, Path]],
    config_file: Optional[Path],
) -> tuple[Path, Path, dict[str, Any], str, str]:
    """Lazily resolve vault_dir, config_file, config dict, host_id, and locale."""
    from mipmem.config import default_config, load_config, resolve_paths
    from mipmem.core.vault import default_host_id

    paths = resolve_paths(configured_vault_dir=vault_root)
    cfg_path = config_file if config_file is not None else paths.config_file
    cfg = load_config(cfg_path) if cfg_path.is_file() else default_config()
    core_tbl = cfg.get("core", {}) if isinstance(cfg, dict) else {}

    if vault_root is not None:
        resolved_vault = Path(vault_root).expanduser()
    elif isinstance(core_tbl, dict) and isinstance(core_tbl.get("vault_path"), str):
        raw_v = str(core_tbl["vault_path"]).strip()
        resolved_vault = Path(raw_v).expanduser() if raw_v else paths.vault_dir
    else:
        resolved_vault = paths.vault_dir

    host_id = ""
    locale = "en"
    if isinstance(core_tbl, dict):
        raw_h = core_tbl.get("host_id")
        if isinstance(raw_h, str) and raw_h.strip():
            host_id = raw_h.strip()
        raw_l = core_tbl.get("locale")
        if isinstance(raw_l, str) and raw_l.strip():
            locale = raw_l.strip()
    if not host_id:
        host_id = default_host_id()
    return resolved_vault, cfg_path, cfg, host_id, locale


def _safe_invocation_num(raw: Any) -> int:
    """Coerce hook invocationNum into a positive integer defaulting to 1."""
    if isinstance(raw, bool) or raw is None:
        return 1
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except ValueError:
        return 1


# @para-doc [#csa-hook-session-start]
def handle_session_start(
    payload: dict[str, Any],
    *,
    vault_root: Optional[Union[str, Path]] = None,
    config_file: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Handle SessionStart: inject L0 Radar (<= 300 tokens) on active workspaces only."""
    started = time.perf_counter()
    surface = _detect_surface(payload.get("transcriptPath"))
    workspace_paths = _extract_workspace_paths(payload)

    from mipmem.config import resolve_workspace_policy

    vault_dir, _, cfg, _, locale = _resolve_hook_runtime(vault_root, config_file)
    policy = resolve_workspace_policy(workspace_paths, cfg)
    if policy in ("disabled", "passive"):
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="session-start",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return {}

    from mipmem.core.vault import is_vault_initialized

    if not is_vault_initialized(vault_dir):
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="session-start",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "vault_not_initialized"},
        )
        return {}

    from mipmem.adapters.slash_mem import load_recent_sessions
    from mipmem.config import resolve_paths
    from mipmem.core.brainstorms import BrainstormShard, load_brainstorm_shard
    from mipmem.core.fts_recall import list_indexed_brainstorms, sync_fts_index
    from mipmem.core.mipmap import render_l0_radar
    from mipmem.core.subapps import load_subapp_telemetry, read_sync_alert
    from mipmem.core.workstreams import thread_workstreams

    sync_fts_index(vault_dir, locale=locale, logger=logger)
    sessions = load_recent_sessions(vault_dir, limit=15)
    ws_result = thread_workstreams(sessions, locale=locale)

    indexed_bs = list_indexed_brainstorms(vault_dir, auto_sync=False, logger=logger)
    shards: list[BrainstormShard] = []
    for item in indexed_bs[:10]:
        try:
            shard, _ = load_brainstorm_shard(vault_dir, str(item["bs_id"]))
            shards.append(shard)
        except Exception:
            continue

    telemetry = load_subapp_telemetry(vault_dir, logger=logger)
    sync_alert = read_sync_alert(resolve_paths(configured_vault_dir=vault_dir).cache_dir)

    l0 = render_l0_radar(
        sessions=sessions,
        brainstorms=shards,
        workstreams=ws_result.clusters,
        config=cfg,
        locale=locale,
        logger=logger,
        subapp_telemetry=telemetry,
        sync_alert=sync_alert,
    )
    _emit_hook_checkpoint(
        logger,
        level="INFO",
        event="session-start",
        surface=surface,
        policy=policy,
        action="injected_l0",
        tokens_injected=l0.est_tokens,
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    return render_system_injection(l0.content)


# @para-doc [#csa-hook-pre-invocation]
def handle_pre_invocation(
    payload: dict[str, Any],
    *,
    vault_root: Optional[Union[str, Path]] = None,
    config_file: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Handle PreInvocation: /mem 0-token user_markdown intercept and active L1 paging."""
    started = time.perf_counter()
    surface = _detect_surface(payload.get("transcriptPath"))
    invocation_num = _safe_invocation_num(payload.get("invocationNum", 1))
    if invocation_num != 1:
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy="skipped_fast",
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"invocation_num": invocation_num},
        )
        return {}

    from mipmem.config import resolve_workspace_policy

    workspace_paths = _extract_workspace_paths(payload)
    vault_dir, cfg_path, cfg, host_id, locale = _resolve_hook_runtime(vault_root, config_file)
    policy = resolve_workspace_policy(workspace_paths, cfg)
    if policy == "disabled":
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return {}

    user_input = str(payload.get("lastUserInput") or "").strip()
    if user_input == "/mem" or user_input.startswith("/mem "):
        from mipmem.adapters.slash_mem import dispatch_slash_mem

        markdown_body = dispatch_slash_mem(
            user_input,
            surface=surface,
            vault_root=vault_dir,
            config_file=cfg_path,
            host_id=host_id,
            locale=locale,
            logger=logger,
        )
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy=policy,
            action="slash_mem",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return render_user_markdown_response(markdown_body)

    if policy == "passive" or not user_input:
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return {}

    from mipmem.core.vault import is_vault_initialized

    if not is_vault_initialized(vault_dir):
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "vault_not_initialized"},
        )
        return {}

    from mipmem.core.fts_recall import evaluate_topic_shift, recall_memory
    from mipmem.core.mipmap import (
        DEFAULT_L1_MAX_TOKENS,
        _resolve_budget,
        _truncate_to_token_budget,
        estimate_tokens,
    )

    conv_id = str(payload.get("conversationId") or "default").strip() or "default"
    decision = evaluate_topic_shift(
        vault_dir,
        conv_id,
        user_input,
        locale=locale,
        config=cfg,
        logger=logger,
    )
    if not decision.triggered:
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"novelty_score": decision.novelty_score},
        )
        return {}

    recall = recall_memory(
        vault_dir,
        user_input,
        locale=locale,
        limit=3,
        auto_sync=True,
        logger=logger,
    )
    if not recall.hits:
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="pre-invocation",
            surface=surface,
            policy=policy,
            action="skipped",
            tokens_injected=0,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "no_hits"},
        )
        return {}

    budget = _resolve_budget(None, cfg, "l1_max_tokens", DEFAULT_L1_MAX_TOKENS)
    lines: list[str] = ["# MipMem L1 Recall Context"]
    for hit in recall.hits:
        lines.extend(hit.l1_card.splitlines())
        lines.append("")
    rendered_l1, _ = _truncate_to_token_budget(lines, budget)
    est_tokens = estimate_tokens(rendered_l1)
    _emit_hook_checkpoint(
        logger,
        level="INFO",
        event="pre-invocation",
        surface=surface,
        policy=policy,
        action="injected_l1",
        tokens_injected=est_tokens,
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    return render_system_injection(rendered_l1)


def _is_payload_subagent(payload: dict[str, Any]) -> bool:
    """Return True if hook stdin payload explicitly marks a subagent conversation."""
    if bool(payload.get("isSubagent", False)):
        return True
    parent_id = str(
        payload.get("parentConversationId")
        or payload.get("parent_conversation_id")
        or ""
    ).strip()
    if parent_id:
        return True
    raw_depth = payload.get("nestingDepth", payload.get("nesting_depth", 0))
    if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) and raw_depth > 0:
        return True
    return False


def _render_latest_handoff(
    record: Any,
    *,
    salience_tier: str,
    salience_score: float,
) -> str:
    """Render hosts/<host_id>/latest_handoff.md for a PRIMARY_WORK session."""
    lines = [
        f"# Latest Primary Work Handoff (`@{record.host_id}`)",
        "",
        f"- **Conversation ID:** `{record.conversation_id}`",
        f"- **Surface:** `{record.surface}`",
        f"- **Updated:** `{record.last_modified_time}`",
        f"- **Salience:** `{salience_tier}` ({salience_score:.1f})",
        f"- **Title:** {record.title}",
    ]
    if record.edited_files:
        lines.append(f"- **Edited Files:** {', '.join(record.edited_files[:10])}")
    if record.recent_prompts:
        lines.append("")
        lines.append("## Recent Prompts")
        for prompt in record.recent_prompts[-5:]:
            lines.append(f"- {prompt}")
    if record.last_agent_response.strip():
        lines.append("")
        lines.append("## Last Agent Response")
        lines.append(record.last_agent_response.strip()[:1200])
    return "\n".join(lines).rstrip() + "\n"


# @para-doc [#csa-hook-stop-harvester]
def handle_stop(
    payload: dict[str, Any],
    *,
    vault_root: Optional[Union[str, Path]] = None,
    config_file: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Handle Stop hook: filter noise/subagents, harvest CLI/IDE, redact, and sync FTS5."""
    started = time.perf_counter()
    transcript_raw = payload.get("transcriptPath")
    surface = _detect_surface(transcript_raw)

    term_reason = str(payload.get("terminationReason") or "").strip()
    if term_reason == "TERMINAL_CUSTOM_HOOK":
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="stop",
            surface=surface,
            policy="skipped_fast",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "terminal_custom_hook"},
        )
        return {}

    if bool(payload.get("continuationActive", False)):
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="stop",
            surface=surface,
            policy="skipped_fast",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "continuation_active"},
        )
        return {}

    if _is_payload_subagent(payload):
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="stop",
            surface=surface,
            policy="skipped_fast",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "subagent"},
        )
        return {}

    from mipmem.config import resolve_workspace_policy

    workspace_paths = _extract_workspace_paths(payload)
    vault_dir, _, cfg, host_id, locale = _resolve_hook_runtime(vault_root, config_file)
    policy = resolve_workspace_policy(workspace_paths, cfg)
    if policy == "disabled":
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="stop",
            surface=surface,
            policy=policy,
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return {}

    from mipmem.core.vault import atomic_write_vault_file, is_vault_initialized, validate_host_id

    if not is_vault_initialized(vault_dir):
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="stop",
            surface=surface,
            policy=policy,
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "vault_not_initialized"},
        )
        return {}

    conv_id = str(payload.get("conversationId") or "").strip()
    db_path_raw = payload.get("dbPath")
    conv = None
    if surface == "cli" or db_path_raw is not None:
        from mipmem.adapters.agy_cli import extract_cli_conversation

        conv = extract_cli_conversation(
            conv_id,
            db_path=db_path_raw,
            transcript_path=transcript_raw,
            logger=logger,
        )
    elif transcript_raw is not None and str(transcript_raw).strip():
        from mipmem.adapters.agy_ide import extract_ide_conversation

        conv = extract_ide_conversation(
            str(transcript_raw),
            conversation_id=conv_id or None,
            workspace_paths=workspace_paths,
            logger=logger,
        )

    if conv is None:
        _emit_hook_checkpoint(
            logger,
            level="INFO",
            event="stop",
            surface=surface,
            policy=policy,
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"reason": "adapter_empty_or_skipped"},
        )
        return {}

    from mipmem.core.fts_recall import sync_fts_index
    from mipmem.core.redactor import redact_text
    from mipmem.core.salience import TIER_PRIMARY_WORK, score_session_salience
    from mipmem.core.sessions import (
        SessionRecord,
        _resolve_session_month,
        _validate_conversation_id,
        parse_session_markdown,
        write_session_record,
    )

    safe_host = validate_host_id(host_id)
    safe_conv_id = _validate_conversation_id(conv.conversation_id)
    month = _resolve_session_month(conv.last_modified_time)
    target_path = vault_dir / "hosts" / safe_host / "sessions" / month / f"{safe_conv_id}.md"
    if target_path.is_file() and not target_path.is_symlink():
        try:
            existing = parse_session_markdown(
                target_path.read_text(encoding="utf-8"),
                fallback_conversation_id=safe_conv_id,
                fallback_host_id=safe_host,
            )
            if (
                conv.step_count <= existing.step_count
                and (conv.last_modified_time or "") <= (existing.last_modified_time or "")
            ):
                _emit_hook_checkpoint(
                    logger,
                    level="INFO",
                    event="stop",
                    surface=conv.surface,
                    policy=policy,
                    action="skipped",
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    details_extra={"reason": "unchanged_idempotent"},
                )
                return {}
        except OSError:
            pass

    raw_rec = conv.to_session_record(safe_host)
    redacted_record = SessionRecord(
        conversation_id=raw_rec.conversation_id,
        host_id=raw_rec.host_id,
        surface=raw_rec.surface,
        last_modified_time=raw_rec.last_modified_time,
        step_count=raw_rec.step_count,
        title=redact_text(raw_rec.title, logger=logger),
        edited_files=[redact_text(p, logger=logger) for p in raw_rec.edited_files],
        touched_files=[redact_text(p, logger=logger) for p in raw_rec.touched_files],
        recent_prompts=[redact_text(p, logger=logger) for p in raw_rec.recent_prompts],
        last_agent_response=redact_text(raw_rec.last_agent_response, logger=logger),
        brainstorm_refs=list(raw_rec.brainstorm_refs),
        body=redact_text(raw_rec.body, logger=logger),
    )

    write_session_record(vault_dir, redacted_record, actor_host=safe_host, logger=logger)
    sal = score_session_salience(redacted_record, locale=locale)
    if sal.tier == TIER_PRIMARY_WORK:
        handoff_md = _render_latest_handoff(
            redacted_record,
            salience_tier=sal.tier,
            salience_score=sal.score,
        )
        atomic_write_vault_file(
            vault_dir,
            f"hosts/{safe_host}/latest_handoff.md",
            handoff_md,
            actor_host=safe_host,
            logger=logger,
        )

    sync_fts_index(vault_dir, locale=locale, logger=logger)
    _emit_hook_checkpoint(
        logger,
        level="INFO",
        event="stop",
        surface=redacted_record.surface,
        policy=policy,
        action="harvested",
        tokens_injected=0,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        details_extra={"salience_tier": sal.tier, "step_count": redacted_record.step_count},
    )
    return {}


def _default_event_handler(
    event: str,
    *,
    logger: Optional[RotatingJsonLogger],
) -> Optional[Callable[[dict[str, Any]], dict[str, Any]]]:
    """Resolve the default hook event handler lazily by event name."""
    if event == "session-start":
        return lambda payload: handle_session_start(payload, logger=logger)
    if event == "pre-invocation":
        return lambda payload: handle_pre_invocation(payload, logger=logger)
    if event == "stop":
        return lambda payload: handle_stop(payload, logger=logger)
    return None


def run_hook_safe(
    event: str,
    *,
    stdin_stream: Optional[Any] = None,
    stdout_stream: Optional[Any] = None,
    preloaded_bytes: Optional[bytes] = None,
    handler: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> int:
    """Execute a hook handler with strict fail-open guarantee: always write JSON and exit 0."""
    started = time.perf_counter()
    out = stdout_stream if stdout_stream is not None else sys.stdout
    active_logger = logger if logger is not None else RotatingJsonLogger()

    response: dict[str, Any] = {}
    try:
        payload = read_hook_payload(
            stdin_stream=stdin_stream,
            preloaded_bytes=preloaded_bytes,
            event=event,
            logger=active_logger,
        )
        active_handler = (
            handler
            if handler is not None
            else _default_event_handler(event, logger=active_logger)
        )
        if payload is not None and active_handler is not None:
            result = active_handler(payload)
            if isinstance(result, dict):
                response = result
    except Exception as exc:
        _emit_hook_checkpoint(
            active_logger,
            level="WARN",
            event=event,
            surface="generic",
            policy="unknown",
            action="skipped",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details_extra={"exception": f"{type(exc).__name__}: {exc}"},
        )
        response = {}

    out.write(json.dumps(response, ensure_ascii=False) + "\n")
    if hasattr(out, "flush"):
        out.flush()
    return 0
