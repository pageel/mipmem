"""Local shell-free /mem slash command dispatcher returning clean GFM Markdown."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
from typing import Any, Optional, Union

from mipmem.config import default_config, load_config, resolve_paths, save_config
from mipmem.core.brainstorms import (
    MAX_BRAINSTORM_BYTES,
    load_brainstorm_shard,
    save_brainstorm_shard,
    update_brainstorm_status,
)
from mipmem.core.diagnostics import (
    CP_HK_01,
    SLASH_COMMAND_INVALID,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.fts_recall import (
    list_indexed_brainstorms,
    probe_fts5_capabilities,
    recall_memory,
    sync_fts_index,
)
from mipmem.core.mipmap import render_l1_skeleton, slice_brainstorm_l2
from mipmem.core.salience import TIER_PRIMARY_WORK, score_session_salience
from mipmem.core.sessions import SessionRecord, parse_session_markdown
from mipmem.core.vault import default_host_id, is_vault_initialized, validate_host_id

_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def read_safe_text_file(
    raw_path: str,
    *,
    error_code: str = SLASH_COMMAND_INVALID,
) -> str:
    """Safely read a regular UTF-8 text file (non-symlink, non-binary, <= 2 MB)."""
    cleaned = str(raw_path or "").strip()
    if not cleaned:
        raise MipmemError(error_code, "File path must not be empty.")
    candidate = Path(cleaned).expanduser()
    if candidate.is_symlink():
        raise MipmemError(
            error_code,
            f"Refusing to read symlink file: {cleaned!r}.",
        )
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise MipmemError(error_code, f"Unable to resolve file path: {exc}") from exc
    if not resolved.is_file():
        raise MipmemError(error_code, f"File not found: {cleaned!r}.")
    size = resolved.stat().st_size
    if size > MAX_BRAINSTORM_BYTES:
        raise MipmemError(
            error_code,
            f"File exceeds {MAX_BRAINSTORM_BYTES} bytes limit ({size} bytes).",
        )
    raw_bytes = resolved.read_bytes()
    if b"\x00" in raw_bytes:
        raise MipmemError(
            error_code,
            f"Refusing to read binary file: {cleaned!r}.",
        )
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MipmemError(
            error_code,
            f"Refusing to read non-UTF-8 binary file: {cleaned!r}.",
        ) from exc


def _gfm_cell(text: str, *, max_len: int) -> str:
    """Escape pipes/newlines and clamp cell width for GFM tables."""
    single = " ".join(str(text or "").split()).replace("|", "\\|")
    if len(single) <= max_len:
        return single
    return single[: max(1, max_len - 3)].rstrip() + "..."


def _title_width(surface: str) -> int:
    """Return surface-aware GFM title column width (compact on CLI, wider on IDE)."""
    return 36 if surface == "cli" else 72


def load_recent_sessions(
    vault_root: Path,
    *,
    host_id: Optional[str] = None,
    limit: int = 50,
) -> list[SessionRecord]:
    """Load recent SessionRecord entries from hosts/*/sessions/**/*.md."""
    hosts_dir = vault_root / "hosts"
    if not hosts_dir.is_dir():
        return []

    records: list[SessionRecord] = []
    target_hosts: list[Path]
    if host_id is not None:
        safe_host = validate_host_id(host_id)
        candidate = hosts_dir / safe_host
        target_hosts = [candidate] if candidate.is_dir() else []
    else:
        target_hosts = [p for p in sorted(hosts_dir.iterdir()) if p.is_dir()]

    for host_dir in target_hosts:
        sessions_dir = host_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for md_file in sessions_dir.rglob("*.md"):
            if md_file.is_symlink() or not md_file.is_file():
                continue
            try:
                raw = md_file.read_text(encoding="utf-8")
                rel = md_file.relative_to(vault_root).as_posix()
                rec = parse_session_markdown(
                    raw,
                    fallback_conversation_id=md_file.stem,
                    fallback_host_id=host_dir.name,
                    file_path=rel,
                )
                records.append(rec)
            except OSError:
                continue

    records.sort(
        key=lambda item: (item.last_modified_time, item.conversation_id),
        reverse=True,
    )
    return records[: max(1, limit)]


def read_host_handoff(
    vault_root: Path,
    host_id: str,
    *,
    locale: str = "en",
) -> str:
    """Read latest_handoff.md for host_id or synthesize one from latest PRIMARY_WORK."""
    safe_host = validate_host_id(host_id)
    handoff_file = vault_root / "hosts" / safe_host / "latest_handoff.md"
    if handoff_file.is_file() and not handoff_file.is_symlink():
        content = handoff_file.read_text(encoding="utf-8").strip()
        if content:
            return content

    sessions = load_recent_sessions(vault_root, host_id=safe_host, limit=25)
    for rec in sessions:
        sal = score_session_salience(rec, locale=locale)
        if sal.tier == TIER_PRIMARY_WORK:
            lines = [
                f"# Latest Primary Work Handoff (`@{safe_host}`)",
                "",
                f"- **Session:** `{rec.conversation_id}` ({rec.surface}, {rec.last_modified_time})",
                f"- **Title:** {rec.title}",
                f"- **Salience:** `{sal.tier}` ({sal.score:.1f})",
            ]
            if rec.edited_files:
                lines.append(f"- **Edited Files:** {', '.join(rec.edited_files[:8])}")
            if rec.last_agent_response.strip():
                lines.append("")
                lines.append("## Summary")
                lines.append(rec.last_agent_response.strip()[:800])
            return "\n".join(lines)

    return f"No primary work handoff recorded yet for host `@{safe_host}`."


def _resolve_runtime_context(
    vault_root: Optional[Union[str, Path]],
    config_file: Optional[Path],
    host_id: Optional[str],
    locale: Optional[str],
) -> tuple[Path, Path, dict[str, Any], str, str]:
    """Resolve vault directory, config file, loaded config dict, host_id, and locale."""
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

    resolved_host = host_id
    if not resolved_host and isinstance(core_tbl, dict):
        raw_h = core_tbl.get("host_id")
        if isinstance(raw_h, str) and raw_h.strip():
            resolved_host = raw_h.strip()
    if not resolved_host:
        resolved_host = default_host_id()

    resolved_locale = locale
    if not resolved_locale and isinstance(core_tbl, dict):
        raw_l = core_tbl.get("locale")
        if isinstance(raw_l, str) and raw_l.strip():
            resolved_locale = raw_l.strip()
    if not resolved_locale:
        resolved_locale = "en"

    return resolved_vault, cfg_path, cfg, resolved_host, resolved_locale


def _render_help() -> str:
    """Render concise GFM help table for all /mem subcommands."""
    return "\n".join(
        [
            "### MipMem `/mem` Commands (0 LLM tokens)",
            "",
            "| Command | Description |",
            "| :--- | :--- |",
            "| `/mem help` | Show this command reference |",
            "| `/mem status` | Show vault, host, and FTS5 index status |",
            "| `/mem recall <query> [--host <h>] [--limit <n>]` | Search sessions & brainstorms |",
            "| `/mem list [start-end]` | List recent sessions (default `1-10`) |",
            "| `/mem bs list [--status <s>] [--host <h>]` | List lossless brainstorm shards |",
            "| `/mem bs save <title> [--file <p>] [--tags a,b]` | Save brainstorm/artifact |",
            "| `/mem bs view <id> [§k \\| #L1-L20 \\| --l1]` | View L1 card or L2 slice |",
            "| `/mem bs status <id> <status>` | Update brainstorm status via host journal |",
            "| `/mem config [show \\| get <k> \\| set <k> <v>]` | View or update config.toml |",
            "| `/mem handoff [@<host_id>]` | Show latest Primary Work Anchor handoff |",
        ]
    )


def _cmd_status(vault_dir: Path, host_id: str, locale: str, surface: str) -> str:
    """Render vault, host, and FTS5 status summary."""
    initialized = is_vault_initialized(vault_dir)
    fts_ok, fts_tok = probe_fts5_capabilities()
    hosts_dir = vault_dir / "hosts"
    host_names = (
        sorted(p.name for p in hosts_dir.iterdir() if p.is_dir())
        if hosts_dir.is_dir()
        else []
    )
    sessions_count = len(load_recent_sessions(vault_dir, limit=1000)) if initialized else 0
    bs_count = (
        len(list_indexed_brainstorms(vault_dir, auto_sync=True))
        if initialized and fts_ok
        else 0
    )
    state_label = "initialized" if initialized else "uninitialized"
    return "\n".join(
        [
            "### MipMem Status",
            "",
            "| Field | Value |",
            "| :--- | :--- |",
            f"| Vault | `{state_label}` (`{vault_dir}`) |",
            f"| Active Host | `{host_id}` (surface: `{surface}`, locale: `{locale}`) |",
            f"| Known Hosts | {', '.join(f'`{h}`' for h in host_names) or 'none'} |",
            f"| Sessions / Brainstorms | `{sessions_count}` sessions / `{bs_count}` brainstorms |",
            f"| SQLite FTS5 | `{'ready' if fts_ok else 'unavailable'}` (`{fts_tok}`) |",
        ]
    )


def _cmd_recall(
    tokens: list[str],
    vault_dir: Path,
    locale: str,
    surface: str,
) -> str:
    """Parse and run `/mem recall <query> [--host ...] [--status ...] [--scope ...] [--limit N]`."""
    if not is_vault_initialized(vault_dir):
        return f"Vault is not initialized at `{vault_dir}`. Run `mipmem init --host <name>`."

    query_parts: list[str] = []
    filter_host: Optional[str] = None
    filter_status: Optional[str] = None
    scope = "all"
    limit = 5

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--host" and idx + 1 < len(tokens):
            filter_host = tokens[idx + 1]
            idx += 2
        elif tok == "--status" and idx + 1 < len(tokens):
            filter_status = tokens[idx + 1]
            idx += 2
        elif tok == "--scope" and idx + 1 < len(tokens):
            scope = tokens[idx + 1]
            idx += 2
        elif tok == "--limit" and idx + 1 < len(tokens):
            try:
                limit = max(1, min(50, int(tokens[idx + 1])))
            except ValueError as exc:
                raise MipmemError(
                    SLASH_COMMAND_INVALID,
                    f"Invalid --limit value: {tokens[idx + 1]!r}.",
                ) from exc
            idx += 2
        elif tok.startswith("--"):
            raise MipmemError(SLASH_COMMAND_INVALID, f"Unknown recall option: {tok!r}.")
        else:
            query_parts.append(tok)
            idx += 1

    query = " ".join(query_parts).strip()
    if not query:
        raise MipmemError(
            SLASH_COMMAND_INVALID,
            "Usage: `/mem recall <query> [--host <h>] [--limit <n>]`",
        )

    result = recall_memory(
        vault_dir,
        query,
        locale=locale,
        host_id=filter_host,
        status=filter_status,
        limit=limit,
        scope=scope,
        auto_sync=True,
    )
    if not result.hits:
        return f"No memory hits found for `{query}`."

    width = _title_width(surface)
    lines = [
        f"### Recall: `{query}` ({len(result.hits)} hit(s), {result.duration_ms:.1f}ms)",
        "",
        "| Kind | ID | Host | Score | Title |",
        "| :--- | :--- | :--- | ---: | :--- |",
    ]
    for hit in result.hits:
        lines.append(
            f"| {hit.kind} | `{hit.item_id}` | `{hit.host_id}` | "
            f"{hit.score:.2f} | {_gfm_cell(hit.title, max_len=width)} |"
        )
    return "\n".join(lines)


def _cmd_list(tokens: list[str], vault_dir: Path, surface: str) -> str:
    """List recent sessions sliced by 1-based [start-end] range."""
    if not is_vault_initialized(vault_dir):
        return f"Vault is not initialized at `{vault_dir}`."

    start_idx = 1
    end_idx = 10
    if tokens:
        match = _RANGE_RE.match(tokens[0].strip())
        if not match:
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                "Usage: `/mem list [start-end]` (e.g. `/mem list 1-10`)",
            )
        start_idx = max(1, int(match.group(1)))
        end_idx = int(match.group(2)) if match.group(2) else start_idx + 9
        if end_idx < start_idx:
            raise MipmemError(SLASH_COMMAND_INVALID, "List range end must be >= start.")

    sessions = load_recent_sessions(vault_dir, limit=end_idx)
    sliced = sessions[start_idx - 1: end_idx]
    if not sliced:
        return f"No sessions in range `{start_idx}-{end_idx}`."

    width = _title_width(surface)
    lines = [
        f"### Recent Sessions (`{start_idx}-{start_idx + len(sliced) - 1}`)",
        "",
        "| # | Date | Host | Surface | Title | ID |",
        "| ---: | :--- | :--- | :--- | :--- | :--- |",
    ]
    for offset, rec in enumerate(sliced, start=start_idx):
        date_str = rec.last_modified_time[:10] if rec.last_modified_time else "n/a"
        lines.append(
            f"| {offset} | {date_str} | `{rec.host_id}` | `{rec.surface}` | "
            f"{_gfm_cell(rec.title, max_len=width)} | `{rec.conversation_id}` |"
        )
    return "\n".join(lines)


def _cmd_bs(
    tokens: list[str],
    vault_dir: Path,
    host_id: str,
    locale: str,
    surface: str,
) -> str:
    """Handle `/mem bs [list|save|view|status]` commands."""
    if not is_vault_initialized(vault_dir):
        return f"Vault is not initialized at `{vault_dir}`."

    action = tokens[0].lower() if tokens else "list"
    rest = tokens[1:] if tokens else []

    if action == "list":
        status_filter: Optional[str] = None
        host_filter: Optional[str] = None
        idx = 0
        while idx < len(rest):
            if rest[idx] == "--status" and idx + 1 < len(rest):
                status_filter = rest[idx + 1]
                idx += 2
            elif rest[idx] == "--host" and idx + 1 < len(rest):
                host_filter = rest[idx + 1]
                idx += 2
            else:
                raise MipmemError(SLASH_COMMAND_INVALID, f"Unknown bs list arg: {rest[idx]!r}.")
        items = list_indexed_brainstorms(
            vault_dir,
            status=status_filter,
            host_id=host_filter,
            auto_sync=True,
        )
        if not items:
            return "No brainstorm shards found."
        width = _title_width(surface)
        lines = [
            f"### Brainstorm Vault ({len(items)} shard(s))",
            "",
            "| ID | Status | Host | Lines | Title |",
            "| :--- | :--- | :--- | ---: | :--- |",
        ]
        for item in items:
            lines.append(
                f"| `{item['bs_id']}` | `{item['effective_status']}` | "
                f"`{item['origin_host']}` | {item['line_count']} | "
                f"{_gfm_cell(str(item['title']), max_len=width)} |"
            )
        return "\n".join(lines)

    if action == "save":
        title_parts: list[str] = []
        file_arg: Optional[str] = None
        content_arg: Optional[str] = None
        tags_raw: Optional[str] = None
        status_arg = "open"

        idx = 0
        while idx < len(rest):
            tok = rest[idx]
            if tok == "--file" and idx + 1 < len(rest):
                file_arg = rest[idx + 1]
                idx += 2
            elif tok == "--content" and idx + 1 < len(rest):
                content_arg = rest[idx + 1]
                idx += 2
            elif tok == "--tags" and idx + 1 < len(rest):
                tags_raw = rest[idx + 1]
                idx += 2
            elif tok == "--status" and idx + 1 < len(rest):
                status_arg = rest[idx + 1]
                idx += 2
            elif tok.startswith("--"):
                raise MipmemError(SLASH_COMMAND_INVALID, f"Unknown bs save option: {tok!r}.")
            else:
                title_parts.append(tok)
                idx += 1

        title = " ".join(title_parts).strip()
        if not title:
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                "Usage: `/mem bs save <title> [--file <path>] [--tags a,b]`",
            )
        if file_arg is not None:
            content = read_safe_text_file(file_arg, error_code=SLASH_COMMAND_INVALID)
        elif content_arg is not None:
            content = content_arg
        else:
            content = f"# {title}\n"

        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else None
        shard = save_brainstorm_shard(
            vault_dir,
            title,
            content,
            host_id=host_id,
            status=status_arg,
            tags=tags,
        )
        sync_fts_index(vault_dir, locale=locale)
        return (
            f"Saved brainstorm `{shard.bs_id}` ({shard.line_count} lines, "
            f"status=`{shard.status}`) at `{shard.md_rel_path}`."
        )

    if action == "view":
        if not rest:
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                "Usage: `/mem bs view <bs_id> [§k | #Lstart-Lend | --l1]`",
            )
        bs_id = rest[0]
        l1_mode = False
        selector: Optional[str] = None
        for tok in rest[1:]:
            if tok == "--l1":
                l1_mode = True
            elif not selector:
                selector = tok
            else:
                raise MipmemError(SLASH_COMMAND_INVALID, f"Unexpected argument: {tok!r}.")

        shard, content = load_brainstorm_shard(vault_dir, bs_id)
        if l1_mode and not selector:
            return render_l1_skeleton(shard).content.strip()
        sliced = slice_brainstorm_l2(content, selector=selector, bs_id=shard.bs_id)
        header = (
            f"### `{shard.bs_id}` ({sliced.layer}, lines "
            f"`#L{sliced.start_line}-L{sliced.end_line}` / {sliced.total_lines})"
        )
        return f"{header}\n\n{sliced.content.strip()}"

    if action == "status":
        if len(rest) < 2:
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                "Usage: `/mem bs status <bs_id> <open|decided|implementing|shipped|archived>`",
            )
        bs_id = rest[0]
        new_status = rest[1]
        reason = " ".join(rest[2:]).strip()
        event = update_brainstorm_status(
            vault_dir,
            bs_id,
            new_status,
            actor_host=host_id,
            reason=reason,
        )
        sync_fts_index(vault_dir, locale=locale)
        return (
            f"Updated brainstorm `{bs_id}` to status `{new_status}` "
            f"(actor=`{host_id}`, event=`{event['event_id']}`)."
        )

    raise MipmemError(
        SLASH_COMMAND_INVALID,
        f"Unsupported `/mem bs` action {action!r}. Expected: list, save, view, status.",
    )


def _cmd_config(tokens: list[str], cfg_path: Path, cfg: dict[str, Any]) -> str:
    """Handle `/mem config [show|get <section.key>|set <section.key> <value>]`."""
    from mipmem.cli_commands import _coerce_config_value

    action = tokens[0].lower() if tokens else "show"
    if action == "show":
        return "```json\n" + json.dumps(cfg, indent=2, ensure_ascii=False) + "\n```"

    if action == "get":
        if len(tokens) < 2 or "." not in tokens[1]:
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                "Usage: `/mem config get <section>.<key>`",
            )
        section, key = tokens[1].split(".", 1)
        schema = default_config()
        schema_sec = schema.get(section)
        cfg_sec = cfg.get(section)
        if (
            not isinstance(schema_sec, dict)
            or key not in schema_sec
            or not isinstance(cfg_sec, dict)
            or key not in cfg_sec
        ):
            raise MipmemError(SLASH_COMMAND_INVALID, f"Unknown config key: `{tokens[1]}`.")
        return f"`{tokens[1]}` = `{json.dumps(cfg_sec[key], ensure_ascii=False)}`"

    if action == "set":
        if len(tokens) < 3 or "." not in tokens[1]:
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                "Usage: `/mem config set <section>.<key> <value>`",
            )
        section, key = tokens[1].split(".", 1)
        raw_val = " ".join(tokens[2:])
        coerced = _coerce_config_value(section, key, raw_val, default_config())
        sec_tbl = cfg.setdefault(section, {})
        if not isinstance(sec_tbl, dict):
            raise MipmemError(SLASH_COMMAND_INVALID, f"Invalid config section: `{section}`.")
        sec_tbl[key] = coerced
        save_config(cfg, cfg_path)
        return f"Updated `{tokens[1]}` = `{json.dumps(coerced, ensure_ascii=False)}`."

    raise MipmemError(
        SLASH_COMMAND_INVALID,
        f"Unsupported `/mem config` action {action!r}. Expected: show, get, set.",
    )


def _cmd_handoff(tokens: list[str], vault_dir: Path, default_host: str, locale: str) -> str:
    """Handle `/mem handoff [@<host_id>]` safely with validate_host_id()."""
    if not is_vault_initialized(vault_dir):
        return f"Vault is not initialized at `{vault_dir}`."
    target_host = default_host
    if tokens:
        raw_target = tokens[0].strip()
        target_host = raw_target[1:] if raw_target.startswith("@") else raw_target
    try:
        safe_host = validate_host_id(target_host)
    except Exception as exc:
        if type(exc).__name__ == "MipmemError":
            msg = getattr(exc, "message", str(exc))
            raise MipmemError(
                SLASH_COMMAND_INVALID,
                f"Invalid host identifier {target_host!r}: {msg}",
            ) from exc
        raise
    return read_host_handoff(vault_dir, safe_host, locale=locale)


# @para-doc [#csa-hook-slash-mem-dispatcher]
def dispatch_slash_mem(
    raw_command: str,
    *,
    surface: str = "generic",
    vault_root: Optional[Union[str, Path]] = None,
    config_file: Optional[Path] = None,
    host_id: Optional[str] = None,
    locale: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> str:
    """Parse and execute an in-chat `/mem ...` command locally without any shell."""
    stripped = str(raw_command or "").strip()
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        if logger is not None:
            logger.log_event(
                "INFO",
                "slash_mem",
                CP_HK_01,
                error_code=SLASH_COMMAND_INVALID,
                details={"reason": "shlex_error", "surface": surface},
            )
        return f"Invalid `/mem` command syntax: {exc}\n\n{_render_help()}"

    if tokens and tokens[0].lower() == "/mem":
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() in ("help", "-h", "--help"):
        return _render_help()

    subcmd = tokens[0].lower()
    args = tokens[1:]

    try:
        vault_dir, cfg_path, cfg, active_host, active_locale = _resolve_runtime_context(
            vault_root, config_file, host_id, locale
        )
        if subcmd == "status":
            return _cmd_status(vault_dir, active_host, active_locale, surface)
        if subcmd == "recall":
            return _cmd_recall(args, vault_dir, active_locale, surface)
        if subcmd == "list":
            return _cmd_list(args, vault_dir, surface)
        if subcmd == "bs":
            return _cmd_bs(args, vault_dir, active_host, active_locale, surface)
        if subcmd == "config":
            return _cmd_config(args, cfg_path, cfg)
        if subcmd == "handoff":
            return _cmd_handoff(args, vault_dir, active_host, active_locale)

        raise MipmemError(
            SLASH_COMMAND_INVALID,
            f"Unknown `/mem` subcommand `{subcmd}`.",
        )
    except Exception as exc:
        if type(exc).__name__ != "MipmemError":
            raise
        err_code = str(getattr(exc, "code", SLASH_COMMAND_INVALID))
        err_msg = str(getattr(exc, "message", str(exc)))
        if logger is not None:
            logger.log_event(
                "INFO",
                "slash_mem",
                CP_HK_01,
                error_code=SLASH_COMMAND_INVALID if err_code != SLASH_COMMAND_INVALID else err_code,
                details={"subcmd": subcmd, "surface": surface, "source_code": err_code},
            )
        return f"**Invalid `/mem` command (`{err_code}`):** {err_msg}"
