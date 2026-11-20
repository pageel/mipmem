"""Incremental SQLite FTS5 indexer, CRDT-lite projection, bigram recall, and topic-shift gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Any, Mapping, Optional, Protocol, Union, runtime_checkable

from mipmem.core.brainstorms import (
    _build_shard_from_disk,
    validate_brainstorm_status,
)
from mipmem.core.diagnostics import (
    CP_FTS_01,
    CP_IDX_01,
    CP_MIP_01,
    FTS_INDEX_CORRUPT,
    FTS_UNAVAILABLE,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    RotatingJsonLogger,
    _best_effort_chmod,
    _utc_timestamp_ms,
)
from mipmem.core.salience import TIER_PRIMARY_WORK, score_session_salience
from mipmem.core.sessions import parse_session_markdown
from mipmem.i18n import extract_anchor_tokens, extract_bigrams, get_lexicon, has_cue_match
from mipmem.i18n.vi import fold_vietnamese_d, normalize_vi_text

try:
    import fcntl as _imported_fcntl
except ImportError:  # pragma: no cover
    _imported_fcntl = None

_fcntl: Any = _imported_fcntl

FTS_DB_REL_PATH = ".cache/global_fts.db"
TOPIC_SHIFT_REL_PATH = ".cache/topic_shift_state.json"
SCHEMA_USER_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 3000
DEFAULT_TOKENIZER = "unicode61 remove_diacritics 2"
FALLBACK_TOKENIZER = "unicode61"

_EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "vault_sync_cursor",
        "session_meta",
        "brainstorm_meta",
        "mutation_events",
        "subapp_telemetry",
        "session_fts",
        "brainstorm_fts",
    }
)

_SYNC_THREAD_LOCK = threading.Lock()
_TOPIC_STATE_LOCK = threading.Lock()
DEFAULT_TOPIC_SHIFT_NOVELTY = 0.70
DEFAULT_TOPIC_SHIFT_LRU = 64
_SLOW_RECALL_WARN_MS = 20.0


@dataclass(frozen=True)
class IndexSyncReport:
    """Telemetry summary returned by sync_fts_index()."""

    db_path: str
    files_scanned: int
    files_indexed: int
    files_removed: int
    mutations_applied: int
    indexed_sessions: int
    indexed_brainstorms: int
    self_healed: bool
    tokenizer_mode: str
    journal_mode: str
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize index sync report into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class _VaultFileEntry:
    """Fast scandir descriptor for one vault file tracked by vault_sync_cursor."""

    kind: str  # "session" | "brainstorm" | "mutations" | "telemetry"
    rel_path: str
    abs_path: str
    host_id: str
    mtime_ns: int
    size_bytes: int


def _prepare_fts_text(text: str, tokenizer_mode: str) -> str:
    """Fold Vietnamese d-stroke (or full accents on unicode61 fallback) before FTS5 writes."""
    if not text:
        return ""
    if "remove_diacritics" in tokenizer_mode:
        return fold_vietnamese_d(text)
    return normalize_vi_text(text)


def _enable_wal_mode(conn: sqlite3.Connection) -> str:
    """Attempt to activate SQLite WAL mode and return the active journal mode."""
    row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    return str(row[0]).lower() if row and row[0] else "delete"


def _configure_connection(conn: sqlite3.Connection, *, is_new: bool = False) -> str:
    """Configure busy_timeout=3000 and WAL with safe fallback to DELETE journal mode."""
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    if not is_new:
        current_row = conn.execute("PRAGMA journal_mode").fetchone()
        current_mode = str(current_row[0]).lower() if current_row and current_row[0] else ""
        if current_mode == "wal":
            try:
                conn.execute("PRAGMA synchronous = OFF")
            except sqlite3.OperationalError:
                pass
            return "wal"
    try:
        journal_mode = _enable_wal_mode(conn)
    except sqlite3.OperationalError:
        row = conn.execute("PRAGMA journal_mode = DELETE").fetchone()
        journal_mode = str(row[0]).lower() if row and row[0] else "delete"
    if journal_mode == "wal":
        try:
            conn.execute("PRAGMA synchronous = OFF")
        except sqlite3.OperationalError:
            pass
    return journal_mode


def _create_base_tables_and_indexes(conn: sqlite3.Connection) -> None:
    """Create the 5 regular Sysdesign §3 tables and 3 secondary indexes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vault_sync_cursor (
            file_path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            last_indexed_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_meta (
            conversation_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            surface TEXT NOT NULL,
            last_modified_time TEXT NOT NULL,
            step_count INTEGER DEFAULT 0,
            salience_score INTEGER DEFAULT 0,
            session_tier TEXT DEFAULT 'UTILITY_MICRO',
            title TEXT NOT NULL,
            archive_path TEXT NOT NULL,
            touched_files TEXT DEFAULT '[]',
            edited_files TEXT DEFAULT '[]',
            recent_prompts TEXT DEFAULT '[]',
            last_agent_response TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS brainstorm_meta (
            bs_id TEXT PRIMARY KEY,
            origin_host TEXT NOT NULL,
            effective_status TEXT NOT NULL,
            status_updated_utc TEXT NOT NULL,
            title TEXT NOT NULL,
            vault_path TEXT NOT NULL,
            line_count INTEGER DEFAULT 0,
            est_tokens INTEGER DEFAULT 0,
            l0_scent TEXT DEFAULT '',
            decisions_json TEXT DEFAULT '[]',
            toc_json TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS mutation_events (
            event_id TEXT PRIMARY KEY,
            actor_host TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subapp_telemetry (
            host_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            status TEXT NOT NULL,
            badge TEXT NOT NULL,
            l0_summary TEXT NOT NULL,
            alert_on_l0 INTEGER DEFAULT 0,
            detail_path TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (host_id, app_id)
        );

        CREATE INDEX IF NOT EXISTS idx_session_host_time
            ON session_meta(host_id, last_modified_time DESC);
        CREATE INDEX IF NOT EXISTS idx_bs_status_updated
            ON brainstorm_meta(effective_status, status_updated_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_mutation_target_ts
            ON mutation_events(target_id, ts_utc DESC);
        """
    )


def _create_fts_virtual_tables(conn: sqlite3.Connection, tokenizer: str) -> None:
    """Create the 2 Sysdesign §3 FTS5 virtual tables using the requested tokenizer."""
    safe_tok = tokenizer.replace("'", "")
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
            conversation_id UNINDEXED,
            host_id UNINDEXED,
            title,
            prompts,
            responses,
            files,
            tokenize='{safe_tok}'
        )
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS brainstorm_fts USING fts5(
            bs_id UNINDEXED,
            origin_host UNINDEXED,
            title,
            scent_and_decisions,
            headings,
            body,
            tokenize='{safe_tok}'
        )
        """
    )


def _initialize_schema(conn: sqlite3.Connection, preferred_tokenizer: str) -> str:
    """Initialize all tables/indexes and return the active FTS5 tokenizer mode."""
    _create_base_tables_and_indexes(conn)
    active_tokenizer = preferred_tokenizer
    try:
        _create_fts_virtual_tables(conn, preferred_tokenizer)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "no such module" in msg and "fts5" in msg:
            raise MipmemError(
                FTS_UNAVAILABLE,
                "SQLite FTS5 extension is unavailable in this Python runtime.",
            ) from exc
        if preferred_tokenizer == FALLBACK_TOKENIZER:
            raise MipmemError(
                FTS_UNAVAILABLE,
                "SQLite FTS5 extension is unavailable in this Python runtime.",
            ) from exc
        active_tokenizer = FALLBACK_TOKENIZER
        try:
            _create_fts_virtual_tables(conn, FALLBACK_TOKENIZER)
        except sqlite3.OperationalError as exc2:
            raise MipmemError(
                FTS_UNAVAILABLE,
                "SQLite FTS5 extension is unavailable in this Python runtime.",
            ) from exc2

    conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
    return active_tokenizer


def _remove_db_files(db_path: Path) -> None:
    """Remove local SQLite cache file and WAL/SHM sidecars cleanly."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        try:
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _inspect_existing_db(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Fast single-query header, table, and tokenizer inspection for an existing FTS5 DB."""
    user_ver = conn.execute("PRAGMA user_version").fetchone()
    if not user_ver or int(user_ver[0]) != SCHEMA_USER_VERSION:
        return False, DEFAULT_TOKENIZER
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables: set[str] = set()
    session_fts_ddl = ""
    for name, ddl in rows:
        name_str = str(name)
        tables.add(name_str)
        if name_str == "session_fts" and ddl:
            session_fts_ddl = str(ddl)
    if not _EXPECTED_TABLES.issubset(tables):
        return False, DEFAULT_TOKENIZER
    tok = DEFAULT_TOKENIZER if "remove_diacritics 2" in session_fts_ddl else FALLBACK_TOKENIZER
    return True, tok


def _open_or_heal_db(
    db_path: Path,
    *,
    rebuild: bool,
    preferred_tokenizer: str,
) -> tuple[sqlite3.Connection, str, str, bool]:
    """Open local FTS5 SQLite DB, self-healing automatically on corruption or version drift."""
    self_healed = False
    existed = db_path.exists()
    if rebuild and existed:
        _remove_db_files(db_path)
        existed = False

    if existed:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=3.0)
            journal_mode = _configure_connection(conn, is_new=False)
            healthy, tokenizer_mode = _inspect_existing_db(conn)
            if healthy:
                return conn, tokenizer_mode, journal_mode, False
            conn.close()
            conn = None
            _remove_db_files(db_path)
            self_healed = True
        except sqlite3.DatabaseError:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            _remove_db_files(db_path)
            self_healed = True

    db_path.parent.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(db_path.parent, 0o700)
    conn_new: Optional[sqlite3.Connection] = None
    try:
        conn_new = sqlite3.connect(str(db_path), timeout=3.0)
        journal_mode = _configure_connection(conn_new, is_new=True)
        tokenizer_mode = _initialize_schema(conn_new, preferred_tokenizer)
        conn_new.commit()
        _best_effort_chmod(db_path, 0o600)
        return conn_new, tokenizer_mode, journal_mode, self_healed
    except MipmemError:
        if conn_new is not None:
            conn_new.close()
        raise
    except sqlite3.DatabaseError as exc:
        if conn_new is not None:
            try:
                conn_new.close()
            except sqlite3.Error:
                pass
        raise MipmemError(
            FTS_INDEX_CORRUPT,
            f"Local search index is corrupted and could not be rebuilt at {db_path}.",
        ) from exc


_DIR_CURSOR_PREFIX = "dir::"
_RACY_DIR_WINDOW_NS = 50_000_000  # 50ms racy-filesystem guard


def _scan_vault_entries(
    vault_resolved: Path,
    *,
    cursor_map: Optional[dict[str, tuple[int, int]]] = None,
    use_dir_prune: bool = True,
) -> tuple[list[_VaultFileEntry], dict[str, tuple[int, int]], set[str]]:
    """Fast single-stat scandir with racy-safe YYYY-MM directory cursor pruning."""
    entries: list[_VaultFileEntry] = []
    dir_cursors: dict[str, tuple[int, int]] = {}
    pruned_prefixes: set[str] = set()
    s_isreg = stat.S_ISREG
    s_isdir = stat.S_ISDIR
    root_str = str(vault_resolved)
    now_ns = time.time_ns()
    active_cursor = cursor_map if (use_dir_prune and cursor_map) else None

    hosts_dir = os.path.join(root_str, "hosts")
    try:
        hosts_st = os.lstat(hosts_dir)
    except OSError:
        hosts_st = None

    if hosts_st is not None and s_isdir(hosts_st.st_mode):
        with os.scandir(hosts_dir) as host_it:
            for host_ent in host_it:
                try:
                    hst = host_ent.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not s_isdir(hst.st_mode):
                    continue
                host_id = host_ent.name
                host_path = host_ent.path

                sessions_root = os.path.join(host_path, "sessions")
                try:
                    sr_st = os.lstat(sessions_root)
                except OSError:
                    sr_st = None
                if sr_st is not None and s_isdir(sr_st.st_mode):
                    with os.scandir(sessions_root) as month_it:
                        for month_ent in month_it:
                            try:
                                mst = month_ent.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if not s_isdir(mst.st_mode):
                                continue
                            month_prefix = f"hosts/{host_id}/sessions/{month_ent.name}/"
                            dir_key = f"{_DIR_CURSOR_PREFIX}{month_prefix}"
                            dir_sig = (mst.st_mtime_ns, mst.st_ctime_ns)
                            dir_cursors[dir_key] = dir_sig

                            if (
                                active_cursor is not None
                                and active_cursor.get(dir_key) == dir_sig
                                and (now_ns - mst.st_mtime_ns) > _RACY_DIR_WINDOW_NS
                            ):
                                pruned_prefixes.add(month_prefix)
                                continue

                            with os.scandir(month_ent.path) as sess_it:
                                for sess_ent in sess_it:
                                    name = sess_ent.name
                                    if not name.endswith(".md"):
                                        continue
                                    try:
                                        st = sess_ent.stat(follow_symlinks=False)
                                    except OSError:
                                        continue
                                    if not s_isreg(st.st_mode):
                                        continue
                                    entries.append(
                                        _VaultFileEntry(
                                            kind="session",
                                            rel_path=month_prefix + name,
                                            abs_path=sess_ent.path,
                                            host_id=host_id,
                                            mtime_ns=st.st_mtime_ns,
                                            size_bytes=st.st_size,
                                        )
                                    )

                mut_path = os.path.join(host_path, "events", "mutations.jsonl")
                try:
                    mut_st = os.lstat(mut_path)
                except OSError:
                    mut_st = None
                if mut_st is not None and s_isreg(mut_st.st_mode):
                    entries.append(
                        _VaultFileEntry(
                            kind="mutations",
                            rel_path=f"hosts/{host_id}/events/mutations.jsonl",
                            abs_path=mut_path,
                            host_id=host_id,
                            mtime_ns=mut_st.st_mtime_ns,
                            size_bytes=mut_st.st_size,
                        )
                    )

                tel_dir = os.path.join(host_path, "telemetry")
                try:
                    tel_st = os.lstat(tel_dir)
                except OSError:
                    tel_st = None
                if tel_st is not None and s_isdir(tel_st.st_mode):
                    tel_prefix = f"hosts/{host_id}/telemetry/"
                    with os.scandir(tel_dir) as tel_it:
                        for tel_ent in tel_it:
                            name = tel_ent.name
                            if not name.endswith(".status.json"):
                                continue
                            try:
                                st = tel_ent.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if not s_isreg(st.st_mode):
                                continue
                            entries.append(
                                _VaultFileEntry(
                                    kind="telemetry",
                                    rel_path=tel_prefix + name,
                                    abs_path=tel_ent.path,
                                    host_id=host_id,
                                    mtime_ns=st.st_mtime_ns,
                                    size_bytes=st.st_size,
                                )
                            )

    by_host_dir = os.path.join(root_str, "shared", "brainstorms", "by-host")
    try:
        bh_st = os.lstat(by_host_dir)
    except OSError:
        bh_st = None
    if bh_st is not None and s_isdir(bh_st.st_mode):
        with os.scandir(by_host_dir) as bs_host_it:
            for bs_host_ent in bs_host_it:
                try:
                    bhs = bs_host_ent.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not s_isdir(bhs.st_mode):
                    continue
                origin_host = bs_host_ent.name
                bs_prefix = f"shared/brainstorms/by-host/{origin_host}/"
                with os.scandir(bs_host_ent.path) as bs_it:
                    for bs_ent in bs_it:
                        name = bs_ent.name
                        if not name.endswith(".md"):
                            continue
                        try:
                            st_md = bs_ent.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if not s_isreg(st_md.st_mode):
                            continue
                        mtime_ns = st_md.st_mtime_ns
                        size_bytes = st_md.st_size
                        meta_str = bs_ent.path[:-3] + ".meta.json"
                        try:
                            st_meta = os.lstat(meta_str)
                            if s_isreg(st_meta.st_mode):
                                if st_meta.st_mtime_ns > mtime_ns:
                                    mtime_ns = st_meta.st_mtime_ns
                                size_bytes += st_meta.st_size
                        except OSError:
                            pass
                        entries.append(
                            _VaultFileEntry(
                                kind="brainstorm",
                                rel_path=bs_prefix + name,
                                abs_path=bs_ent.path,
                                host_id=origin_host,
                                mtime_ns=mtime_ns,
                                size_bytes=size_bytes,
                            )
                        )

    return entries, dir_cursors, pruned_prefixes


def _remove_orphan_records(
    conn: sqlite3.Connection,
    orphan_rel_paths: set[str],
) -> tuple[int, bool]:
    """Delete stale rows from metadata, FTS5, and cursor tables for removed vault files."""
    removed = 0
    mutations_affected = False

    for rel_path in orphan_rel_paths:
        if "/sessions/" in rel_path:
            rows = conn.execute(
                "SELECT rowid, conversation_id FROM session_meta WHERE archive_path = ?",
                (rel_path,),
            ).fetchall()
            for rowid, conv_id in rows:
                conn.execute("DELETE FROM session_fts WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM session_meta WHERE conversation_id = ?", (conv_id,))
        elif rel_path.startswith("shared/brainstorms/by-host/"):
            rows = conn.execute(
                "SELECT rowid, bs_id FROM brainstorm_meta WHERE vault_path = ?",
                (rel_path,),
            ).fetchall()
            for rowid, bs_id in rows:
                conn.execute("DELETE FROM brainstorm_fts WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM brainstorm_meta WHERE bs_id = ?", (bs_id,))
        elif rel_path.endswith("/events/mutations.jsonl"):
            parts = rel_path.split("/")
            if len(parts) >= 2:
                conn.execute("DELETE FROM mutation_events WHERE actor_host = ?", (parts[1],))
            mutations_affected = True
        elif "/telemetry/" in rel_path and rel_path.endswith(".status.json"):
            parts = rel_path.split("/")
            if len(parts) >= 4:
                app_id = parts[-1][: -len(".status.json")]
                conn.execute(
                    "DELETE FROM subapp_telemetry WHERE host_id = ? AND app_id = ?",
                    (parts[1], app_id),
                )

        conn.execute("DELETE FROM vault_sync_cursor WHERE file_path = ?", (rel_path,))
        removed += 1

    return removed, mutations_affected


def _index_session_entry(
    conn: sqlite3.Connection,
    entry: _VaultFileEntry,
    *,
    locale: str,
    tokenizer_mode: str,
) -> None:
    """Parse, score, and upsert one host session into session_meta and session_fts."""
    abs_file = Path(entry.abs_path)
    raw_text = abs_file.read_text(encoding="utf-8")
    fallback_conv = abs_file.stem
    fallback_ts = _utc_timestamp_ms()
    rec = parse_session_markdown(
        raw_text,
        fallback_conversation_id=fallback_conv,
        fallback_host_id=entry.host_id,
        fallback_last_modified=fallback_ts,
        file_path=entry.rel_path,
    )
    sal = score_session_salience(rec, locale=locale)

    old_row = conn.execute(
        "SELECT rowid FROM session_meta WHERE conversation_id = ?",
        (rec.conversation_id,),
    ).fetchone()
    touched_json = json.dumps(rec.touched_files, ensure_ascii=False)
    edited_json = json.dumps(rec.edited_files, ensure_ascii=False)
    prompts_json = json.dumps(rec.recent_prompts, ensure_ascii=False)
    sal_int = int(round(sal.score))

    if old_row is not None:
        new_rowid = old_row[0]
        conn.execute("DELETE FROM session_fts WHERE rowid = ?", (new_rowid,))
        conn.execute(
            """
            UPDATE session_meta SET
                host_id = ?, surface = ?, last_modified_time = ?, step_count = ?,
                salience_score = ?, session_tier = ?, title = ?, archive_path = ?,
                touched_files = ?, edited_files = ?, recent_prompts = ?, last_agent_response = ?
            WHERE rowid = ?
            """,
            (
                rec.host_id,
                rec.surface,
                rec.last_modified_time,
                rec.step_count,
                sal_int,
                sal.tier,
                rec.title,
                entry.rel_path,
                touched_json,
                edited_json,
                prompts_json,
                rec.last_agent_response,
                new_rowid,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO session_meta (
                conversation_id, host_id, surface, last_modified_time, step_count,
                salience_score, session_tier, title, archive_path,
                touched_files, edited_files, recent_prompts, last_agent_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.conversation_id,
                rec.host_id,
                rec.surface,
                rec.last_modified_time,
                rec.step_count,
                sal_int,
                sal.tier,
                rec.title,
                entry.rel_path,
                touched_json,
                edited_json,
                prompts_json,
                rec.last_agent_response,
            ),
        )
        new_rowid = cur.lastrowid

    prompts_text = "\n".join(rec.recent_prompts)
    responses_text = "\n".join(part for part in (rec.last_agent_response, rec.body) if part)
    files_text = " ".join((*rec.edited_files, *rec.touched_files))

    conn.execute(
        """
        INSERT INTO session_fts (
            rowid, conversation_id, host_id, title, prompts, responses, files
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_rowid,
            rec.conversation_id,
            rec.host_id,
            _prepare_fts_text(rec.title, tokenizer_mode),
            _prepare_fts_text(prompts_text, tokenizer_mode),
            _prepare_fts_text(responses_text, tokenizer_mode),
            _prepare_fts_text(files_text, tokenizer_mode),
        ),
    )


def _index_brainstorm_entry(
    conn: sqlite3.Connection,
    vault_resolved: Path,
    entry: _VaultFileEntry,
    *,
    tokenizer_mode: str,
) -> None:
    """Parse and upsert one brainstorm shard into brainstorm_meta and brainstorm_fts."""
    shard, md_text = _build_shard_from_disk(vault_resolved, Path(entry.abs_path), entry.host_id)
    old_row = conn.execute(
        "SELECT rowid FROM brainstorm_meta WHERE bs_id = ?",
        (shard.bs_id,),
    ).fetchone()
    if old_row is not None:
        conn.execute("DELETE FROM brainstorm_fts WHERE rowid = ?", (old_row[0],))

    cur = conn.execute(
        """
        INSERT OR REPLACE INTO brainstorm_meta (
            bs_id, origin_host, effective_status, status_updated_utc, title,
            vault_path, line_count, est_tokens, l0_scent, decisions_json, toc_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shard.bs_id,
            shard.origin_host,
            shard.status,
            shard.updated_at,
            shard.title,
            entry.rel_path,
            shard.line_count,
            shard.est_tokens,
            shard.l0_scent,
            json.dumps(shard.decisions, ensure_ascii=False),
            json.dumps([sec.to_dict() for sec in shard.toc], ensure_ascii=False),
        ),
    )
    new_rowid = cur.lastrowid

    scent_and_decisions = "\n".join([shard.l0_scent, *shard.decisions, *shard.tags])
    headings_text = "\n".join(sec.title for sec in shard.toc)
    conn.execute(
        """
        INSERT INTO brainstorm_fts (
            rowid, bs_id, origin_host, title, scent_and_decisions, headings, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_rowid,
            shard.bs_id,
            shard.origin_host,
            _prepare_fts_text(shard.title, tokenizer_mode),
            _prepare_fts_text(scent_and_decisions, tokenizer_mode),
            _prepare_fts_text(headings_text, tokenizer_mode),
            _prepare_fts_text(md_text, tokenizer_mode),
        ),
    )


def _index_mutations_entry(conn: sqlite3.Connection, entry: _VaultFileEntry) -> int:
    """Sync one host's mutations.jsonl file into mutation_events and return event count."""
    count = 0
    conn.execute("DELETE FROM mutation_events WHERE actor_host = ?", (entry.host_id,))
    raw_lines = Path(entry.abs_path).read_text(encoding="utf-8").splitlines()
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        event_id = str(evt.get("event_id", "")).strip()
        actor_host = str(evt.get("actor_host", "")).strip() or entry.host_id
        target_type = str(evt.get("target_type", "")).strip()
        target_id = str(evt.get("target_id", "")).strip()
        ts_utc = str(evt.get("ts_utc", "")).strip()
        payload = evt.get("payload", {})
        if not (event_id and target_type and target_id and ts_utc):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO mutation_events (
                event_id, actor_host, target_type, target_id, payload_json, ts_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                actor_host,
                target_type,
                target_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ts_utc,
            ),
        )
        count += 1
    return count


def _index_telemetry_entry(conn: sqlite3.Connection, entry: _VaultFileEntry) -> None:
    """Upsert one per-host private sub-app telemetry status JSON into subapp_telemetry."""
    abs_file = Path(entry.abs_path)
    try:
        data = json.loads(abs_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    app_id = str(data.get("app_id", "")).strip() or abs_file.name[: -len(".status.json")]
    status = str(data.get("status", "ok")).strip().lower()
    badge = str(data.get("badge", "")).strip()
    l0_summary = str(data.get("l0_summary", "")).strip()
    alert_on_l0 = 1 if bool(data.get("alert_on_l0", False)) else 0
    detail_path = data.get("detail_path")
    updated_at = str(data.get("updated_at", "")).strip() or _utc_timestamp_ms()
    conn.execute(
        """
        INSERT OR REPLACE INTO subapp_telemetry (
            host_id, app_id, status, badge, l0_summary, alert_on_l0, detail_path, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.host_id,
            app_id,
            status,
            badge,
            l0_summary,
            alert_on_l0,
            str(detail_path) if detail_path is not None else None,
            updated_at,
        ),
    )


def _apply_crdt_brainstorm_lww(conn: sqlite3.Connection, vault_resolved: Path) -> int:
    """Project deterministic MAX((ts_utc, actor_host, event_id)) status onto brainstorm_meta."""
    rows = conn.execute(
        """
        SELECT event_id, actor_host, target_id, payload_json, ts_utc
        FROM mutation_events
        WHERE target_type = 'brainstorm_status'
        """
    ).fetchall()
    if not rows:
        return 0

    winners: dict[str, tuple[tuple[str, str, str], str]] = {}
    for event_id, actor_host, target_id, payload_json, ts_utc in rows:
        try:
            payload = json.loads(str(payload_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "status" not in payload:
            continue
        try:
            clean_status = validate_brainstorm_status(str(payload["status"]))
        except MipmemError:
            continue

        order_key = (str(ts_utc), str(actor_host), str(event_id))
        current = winners.get(str(target_id))
        if current is None or order_key > current[0]:
            winners[str(target_id)] = (order_key, clean_status)

    applied = 0
    for bs_id, ((win_ts, win_host, _win_evt), win_status) in winners.items():
        meta_row = conn.execute(
            "SELECT origin_host, vault_path FROM brainstorm_meta WHERE bs_id = ?",
            (bs_id,),
        ).fetchone()
        if meta_row is None:
            continue
        origin_host, vault_rel = str(meta_row[0]), str(meta_row[1])
        base_ts = ""
        base_status = ""
        md_abs = vault_resolved / vault_rel
        meta_abs = md_abs.with_suffix(".meta.json")
        if meta_abs.is_file() and not meta_abs.is_symlink():
            try:
                raw_meta = json.loads(meta_abs.read_text(encoding="utf-8"))
                if isinstance(raw_meta, dict):
                    base_ts = str(raw_meta.get("updated_at", "")).strip()
                    base_status = str(raw_meta.get("status", "")).strip()
            except (OSError, json.JSONDecodeError):
                pass

        base_key = (base_ts, origin_host, "")
        win_key = (win_ts, win_host, _win_evt)
        if not base_ts or win_key >= base_key:
            conn.execute(
                """
                UPDATE brainstorm_meta
                SET effective_status = ?, status_updated_utc = ?
                WHERE bs_id = ?
                """,
                (win_status, win_ts, bs_id),
            )
            applied += 1
        elif base_status:
            conn.execute(
                """
                UPDATE brainstorm_meta
                SET effective_status = ?, status_updated_utc = ?
                WHERE bs_id = ?
                """,
                (base_status, base_ts, bs_id),
            )
    return applied


def probe_fts5_capabilities() -> tuple[bool, str]:
    """Probe runtime SQLite FTS5 support and preferred tokenizer without touching disk."""
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE _probe USING fts5(c, tokenize='{DEFAULT_TOKENIZER}')"
            )
            return True, DEFAULT_TOKENIZER
        except sqlite3.OperationalError as exc:
            if "no such module" in str(exc).lower():
                return False, "unavailable"
            try:
                conn.execute(
                    f"CREATE VIRTUAL TABLE _probe2 USING fts5(c, tokenize='{FALLBACK_TOKENIZER}')"
                )
                return True, FALLBACK_TOKENIZER
            except sqlite3.OperationalError:
                return False, "unavailable"
    finally:
        conn.close()


# @para-doc [#csa-fts-incremental-indexer]
def sync_fts_index(
    vault_root: Union[str, Path],
    *,
    locale: str = "en",
    rebuild: bool = False,
    preferred_tokenizer: str = DEFAULT_TOKENIZER,
    logger: Optional[RotatingJsonLogger] = None,
    _conn: Optional[sqlite3.Connection] = None,
    _cached_tokenizer: Optional[str] = None,
    _cached_journal: Optional[str] = None,
) -> IndexSyncReport:
    """Incrementally sync Sharded Vault artifacts into .cache/global_fts.db with self-healing."""
    started = time.perf_counter()
    if _conn is not None and not rebuild:
        vault_resolved = Path(vault_root)
    else:
        root_candidate = Path(vault_root).expanduser()
        if root_candidate.is_symlink():
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                "Security block: vault root cannot be a symlink.",
            )
        vault_resolved = root_candidate.resolve()
    db_path = vault_resolved / FTS_DB_REL_PATH

    with _SYNC_THREAD_LOCK:
        force_rebuild = rebuild
        self_healed = False
        tokenizer_mode = _cached_tokenizer or preferred_tokenizer
        journal_mode = _cached_journal or "wal"
        files_scanned = 0
        files_indexed = 0
        files_removed = 0
        mutations_loaded = 0
        indexed_sessions = 0
        indexed_brainstorms = 0

        for attempt in range(2):
            owns_conn = _conn is None or force_rebuild or attempt > 0
            if owns_conn:
                conn, tokenizer_mode, journal_mode, opened_healed = _open_or_heal_db(
                    db_path,
                    rebuild=force_rebuild,
                    preferred_tokenizer=preferred_tokenizer,
                )
            else:
                assert _conn is not None
                conn = _conn
                opened_healed = False
            self_healed = self_healed or opened_healed
            files_indexed = 0
            files_removed = 0
            mutations_loaded = 0
            try:
                use_prune = not (force_rebuild or self_healed)
                dir_cursor_map: dict[str, tuple[int, int]] = {}
                if use_prune:
                    dir_cursor_map = {
                        str(row[0]): (int(row[1]), int(row[2]))
                        for row in conn.execute(
                            "SELECT file_path, mtime_ns, size_bytes "
                            "FROM vault_sync_cursor "
                            "WHERE file_path >= 'dir::' AND file_path < 'dir:;'"
                        ).fetchall()
                    }
                entries, dir_cursors, pruned_prefixes = _scan_vault_entries(
                    vault_resolved,
                    cursor_map=dir_cursor_map,
                    use_dir_prune=use_prune,
                )
                files_scanned = len(entries)

                changed_dir_cursors = [
                    (dk, sig)
                    for dk, sig in dir_cursors.items()
                    if dir_cursor_map.get(dk) != sig
                ]
                stale_dir_keys = {
                    dk for dk in dir_cursor_map if dk not in dir_cursors
                }

                # Load only unpruned file cursors via a single B-tree range scan query
                if pruned_prefixes and not stale_dir_keys:
                    cursor_map: dict[str, tuple[int, int]] = {}
                    active_prefixes = [
                        dk[len(_DIR_CURSOR_PREFIX):]
                        for dk in dir_cursors
                        if dk[len(_DIR_CURSOR_PREFIX):] not in pruned_prefixes
                    ]
                    range_prefixes = list(active_prefixes)
                    range_prefixes.append("shared/")
                    all_hosts = {
                        dk[len(_DIR_CURSOR_PREFIX) + 6:].split("/", 1)[0]
                        for dk in dir_cursors
                        if dk.startswith(f"{_DIR_CURSOR_PREFIX}hosts/")
                    } | {e.host_id for e in entries if e.kind != "brainstorm"}
                    for entry_host in all_hosts:
                        range_prefixes.append(f"hosts/{entry_host}/events/")
                        range_prefixes.append(f"hosts/{entry_host}/telemetry/")
                    uniq_prefixes = sorted(set(range_prefixes))
                    or_clauses = " OR ".join(
                        "(file_path >= ? AND file_path < ?)" for _ in uniq_prefixes
                    )
                    range_params: list[str] = []
                    for pref in uniq_prefixes:
                        range_params.append(pref)
                        range_params.append(pref + "\uffff")
                    for row in conn.execute(
                        "SELECT file_path, mtime_ns, size_bytes FROM vault_sync_cursor "
                        f"WHERE {or_clauses}",
                        range_params,
                    ).fetchall():
                        cursor_map[str(row[0])] = (int(row[1]), int(row[2]))
                else:
                    cursor_map = {
                        str(row[0]): (int(row[1]), int(row[2]))
                        for row in conn.execute(
                            "SELECT file_path, mtime_ns, size_bytes "
                            "FROM vault_sync_cursor "
                            "WHERE file_path < 'dir::' OR file_path >= 'dir:;'"
                        ).fetchall()
                    }

                seen_paths = {entry.rel_path for entry in entries}
                pruned_tuple = tuple(pruned_prefixes)
                orphan_paths: set[str] = {
                    tracked_path
                    for tracked_path in cursor_map
                    if tracked_path not in seen_paths
                    and not (pruned_tuple and tracked_path.startswith(pruned_tuple))
                }

                changed_entries = [
                    entry
                    for entry in entries
                    if cursor_map.get(entry.rel_path) != (entry.mtime_ns, entry.size_bytes)
                ]

                needs_lww = self_healed or force_rebuild

                if orphan_paths or stale_dir_keys or changed_entries or changed_dir_cursors:
                    now_utc = _utc_timestamp_ms()
                    with conn:
                        if orphan_paths:
                            removed_cnt, mut_orphan = _remove_orphan_records(conn, orphan_paths)
                            files_removed += removed_cnt
                            if mut_orphan:
                                needs_lww = True

                        for stale_dk in stale_dir_keys:
                            conn.execute(
                                "DELETE FROM vault_sync_cursor WHERE file_path = ?",
                                (stale_dk,),
                            )

                        for entry in changed_entries:
                            if entry.kind == "session":
                                _index_session_entry(
                                    conn,
                                    entry,
                                    locale=locale,
                                    tokenizer_mode=tokenizer_mode,
                                )
                            elif entry.kind == "brainstorm":
                                _index_brainstorm_entry(
                                    conn,
                                    vault_resolved,
                                    entry,
                                    tokenizer_mode=tokenizer_mode,
                                )
                                needs_lww = True
                            elif entry.kind == "mutations":
                                mutations_loaded += _index_mutations_entry(conn, entry)
                                needs_lww = True
                            elif entry.kind == "telemetry":
                                _index_telemetry_entry(conn, entry)

                            conn.execute(
                                """
                                INSERT OR REPLACE INTO vault_sync_cursor (
                                    file_path, mtime_ns, size_bytes, last_indexed_utc
                                ) VALUES (?, ?, ?, ?)
                                """,
                                (entry.rel_path, entry.mtime_ns, entry.size_bytes, now_utc),
                            )
                            files_indexed += 1

                        for dk, (d_mtime, d_ctime) in changed_dir_cursors:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO vault_sync_cursor (
                                    file_path, mtime_ns, size_bytes, last_indexed_utc
                                ) VALUES (?, ?, ?, ?)
                                """,
                                (dk, d_mtime, d_ctime, now_utc),
                            )

                        if needs_lww:
                            applied_lww = _apply_crdt_brainstorm_lww(conn, vault_resolved)
                            mutations_loaded = max(mutations_loaded, applied_lww)

                counts_row = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM session_meta), "
                    "(SELECT COUNT(*) FROM brainstorm_meta)"
                ).fetchone()
                indexed_sessions = int(counts_row[0]) if counts_row else 0
                indexed_brainstorms = int(counts_row[1]) if counts_row else 0
                break
            except sqlite3.DatabaseError as exc:
                if owns_conn:
                    conn.close()
                if attempt == 0:
                    _remove_db_files(db_path)
                    force_rebuild = True
                    self_healed = True
                    continue
                raise MipmemError(
                    FTS_INDEX_CORRUPT,
                    f"Local search index is corrupted and could not be rebuilt at {db_path}.",
                ) from exc
            finally:
                if owns_conn:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    if logger is not None:
        level = (
            "WARN"
            if (self_healed or tokenizer_mode != DEFAULT_TOKENIZER or journal_mode != "wal")
            else "INFO"
        )
        logger.log_event(
            level,
            "fts_indexer",
            CP_IDX_01,
            duration_ms=elapsed_ms,
            details={
                "files_scanned": files_scanned,
                "files_indexed": files_indexed,
                "files_removed": files_removed,
                "mutations_applied": mutations_loaded,
                "self_healed": self_healed,
                "tokenizer_mode": tokenizer_mode,
                "journal_mode": journal_mode,
                "duration_ms": elapsed_ms,
            },
        )

    return IndexSyncReport(
        db_path=str(db_path),
        files_scanned=files_scanned,
        files_indexed=files_indexed,
        files_removed=files_removed,
        mutations_applied=mutations_loaded,
        indexed_sessions=indexed_sessions,
        indexed_brainstorms=indexed_brainstorms,
        self_healed=self_healed,
        tokenizer_mode=tokenizer_mode,
        journal_mode=journal_mode,
        duration_ms=elapsed_ms,
    )


@dataclass(frozen=True)
class RecallHit:
    """Single ranked memory match returned from sessions or brainstorms."""

    kind: str  # "session" | "brainstorm"
    item_id: str
    host_id: str
    title: str
    score: float
    bm25_raw: float
    bigram_matched: bool
    timestamp: str
    vault_path: str
    tier: Optional[str] = None
    status: Optional[str] = None
    l1_card: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize recall hit into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class RecallResult:
    """Aggregated FTS5 recall response with telemetry and L1 summaries."""

    locale: str
    token_count: int
    bigram_count: int
    used_prefix_fallback: bool
    duration_ms: float
    hits: list[RecallHit]

    def to_dict(self) -> dict[str, Any]:
        """Serialize recall result into a JSON-compatible dictionary."""
        return {
            "locale": self.locale,
            "token_count": self.token_count,
            "bigram_count": self.bigram_count,
            "used_prefix_fallback": self.used_prefix_fallback,
            "duration_ms": self.duration_ms,
            "hits": [hit.to_dict() for hit in self.hits],
        }


@dataclass(frozen=True)
class TopicShiftDecision:
    """Decision returned by the per-conversation Topic-Shift Novelty Gate."""

    conversation_id: str
    novelty_score: float
    threshold: float
    intent_matched: bool
    triggered: bool
    active_tokens: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialize topic-shift decision into a JSON-compatible dictionary."""
        return asdict(self)


@runtime_checkable
class RecallBackend(Protocol):
    """Agent-agnostic retrieval protocol ready for Local MCP and future extensions."""

    def sync(self, *, rebuild: bool = False) -> IndexSyncReport:
        """Synchronize the derived search index from the Sharded Vault."""
        ...

    def recall(
        self,
        query: str,
        *,
        locale: str = "en",
        host_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 5,
        scope: str = "all",
    ) -> RecallResult:
        """Query local memory and return ranked L1 recall hits."""
        ...


def _recency_multiplier(timestamp_iso: str, now_utc: datetime) -> float:
    """Compute a gentle recency decay factor in [0.85, 1.0] from an ISO-8601 date."""
    if len(timestamp_iso) < 10:
        return 0.90
    try:
        dt = datetime.strptime(timestamp_iso[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.90
    age_days = max(0.0, (now_utc - dt).total_seconds() / 86400.0)
    decay = 1.0 / (1.0 + age_days / 90.0)
    return 0.85 + 0.15 * decay


def _build_or_match_expr(tokens: list[str], *, prefix: bool = False) -> str:
    """Build a safe FTS5 OR expression from pre-sanitized [a-z0-9_]+ tokens."""
    if prefix:
        parts = [f'"{tok}"*' for tok in tokens if len(tok) >= 3]
        return " OR ".join(parts)
    return " OR ".join(f'"{tok}"' for tok in tokens)


def _build_bigram_near_expr(bigrams: list[tuple[str, str]]) -> str:
    """Build a safe FTS5 NEAR expression for adjacent token bigrams."""
    return " OR ".join(f'NEAR("{left}" "{right}", 2)' for left, right in bigrams[:6])


def _format_session_l1_card(
    conv_id: str,
    host_id: str,
    surface: str,
    tier: str,
    salience_score: int,
    title: str,
    ts: str,
    edited_json: str,
) -> str:
    """Build a compact L1 summary line/card directly from session_meta columns."""
    lines = [
        f"# [L1] {title}",
        f"- id: `{conv_id}` | host: `{host_id}` | surface: `{surface}` | "
        f"tier: `{tier}` ({salience_score}) | updated: {ts[:10] if ts else 'n/a'}",
    ]
    try:
        edited = json.loads(edited_json)
        if isinstance(edited, list) and edited:
            lines.append(f"- edited_files: {', '.join(str(x) for x in edited[:5])}")
    except json.JSONDecodeError:
        pass
    return "\n".join(lines)


def _format_brainstorm_l1_card(
    bs_id: str,
    origin_host: str,
    status: str,
    title: str,
    line_count: int,
    l0_scent: str,
    toc_json: str,
) -> str:
    """Build a compact L1 structural card with #Lstart-Lend anchors from brainstorm_meta."""
    lines = [
        f"# [L1] {title}",
        f"- id: `{bs_id}` | status: `{status}` | host: `{origin_host}` | lines: {line_count}",
    ]
    if l0_scent:
        lines.append(f"- scent: {l0_scent[:160]}")
    try:
        toc = json.loads(toc_json)
        if isinstance(toc, list) and toc:
            toc_parts = [
                f"§{item.get('index')} {item.get('title')} "
                f"(#L{item.get('start_line')}-L{item.get('end_line')})"
                for item in toc[:5]
                if isinstance(item, dict)
            ]
            if toc_parts:
                lines.append(f"- sections: {'; '.join(toc_parts)}")
    except json.JSONDecodeError:
        pass
    return "\n".join(lines)


def _query_fts_connection(
    conn: sqlite3.Connection,
    *,
    anchor_tokens: list[str],
    bigrams: list[tuple[str, str]],
    bigram_boost: float,
    host_id: Optional[str],
    status: Optional[str],
    limit: int,
    scope: str,
) -> tuple[list[RecallHit], bool]:
    """Execute 6-column bm25() + bigram NEAR boost + salience/recency re-ranking."""
    candidate_limit = max(limit * 4, 24)
    now_utc = datetime.now(timezone.utc)
    used_prefix = False

    include_sessions = scope in ("all", "sessions") and status is None
    include_brainstorms = scope in ("all", "brainstorms")

    or_expr = _build_or_match_expr(anchor_tokens, prefix=False)
    near_expr = _build_bigram_near_expr(bigrams) if bigrams else ""

    def _run_pass(match_expr: str, allow_bigram: bool) -> list[RecallHit]:
        hits: list[RecallHit] = []
        if not match_expr:
            return hits

        if include_sessions:
            sql_sess = (
                "SELECT m.conversation_id, m.host_id, m.surface, m.last_modified_time, "
                "m.salience_score, m.session_tier, m.title, m.archive_path, m.edited_files, "
                "bm25(session_fts, 0.0, 0.0, 6.0, 3.0, 1.5, 4.0) AS rank "
                "FROM session_fts "
                "JOIN session_meta AS m ON m.rowid = session_fts.rowid "
                "WHERE session_fts MATCH ? "
            )
            params_sess: list[Any] = [match_expr]
            if host_id is not None:
                sql_sess += "AND m.host_id = ? "
                params_sess.append(host_id)
            sql_sess += "ORDER BY rank LIMIT ?"
            params_sess.append(candidate_limit)

            sess_rows = conn.execute(sql_sess, params_sess).fetchall()
            bigram_session_ids: set[str] = set()
            if sess_rows and allow_bigram and near_expr:
                try:
                    for (cid,) in conn.execute(
                        "SELECT conversation_id FROM session_fts "
                        "WHERE session_fts MATCH ? LIMIT ?",
                        (near_expr, candidate_limit),
                    ).fetchall():
                        bigram_session_ids.add(str(cid))
                except sqlite3.OperationalError:
                    pass

            for row in sess_rows:
                cid = str(row[0])
                h_id = str(row[1])
                surface = str(row[2])
                ts = str(row[3])
                sal_score = int(row[4])
                tier = str(row[5])
                title = str(row[6])
                archive_path = str(row[7])
                edited_json = str(row[8])
                raw_bm25 = float(row[9])

                base_score = max(0.01, -raw_bm25)
                bi_hit = cid in bigram_session_ids
                if bi_hit:
                    base_score *= bigram_boost

                tier_mult = 1.35 if tier == TIER_PRIMARY_WORK else 0.75
                sal_mult = 1.0 + min(0.25, max(0, sal_score) / 400.0)
                rec_mult = _recency_multiplier(ts, now_utc)
                final_score = round(base_score * tier_mult * sal_mult * rec_mult, 4)

                hits.append(
                    RecallHit(
                        kind="session",
                        item_id=cid,
                        host_id=h_id,
                        title=title,
                        score=final_score,
                        bm25_raw=round(raw_bm25, 4),
                        bigram_matched=bi_hit,
                        timestamp=ts,
                        vault_path=archive_path,
                        tier=tier,
                        status=None,
                        l1_card=_format_session_l1_card(
                            cid, h_id, surface, tier, sal_score, title, ts, edited_json
                        ),
                    )
                )

        if include_brainstorms:
            sql_bs = (
                "SELECT m.bs_id, m.origin_host, m.effective_status, m.status_updated_utc, "
                "m.title, m.vault_path, m.line_count, m.l0_scent, m.toc_json, "
                "bm25(brainstorm_fts, 0.0, 0.0, 6.0, 4.0, 3.0, 1.0) AS rank "
                "FROM brainstorm_fts "
                "JOIN brainstorm_meta AS m ON m.rowid = brainstorm_fts.rowid "
                "WHERE brainstorm_fts MATCH ? "
            )
            params_bs: list[Any] = [match_expr]
            if host_id is not None:
                sql_bs += "AND m.origin_host = ? "
                params_bs.append(host_id)
            if status is not None:
                sql_bs += "AND m.effective_status = ? "
                params_bs.append(status)
            sql_bs += "ORDER BY rank LIMIT ?"
            params_bs.append(candidate_limit)

            bs_rows = conn.execute(sql_bs, params_bs).fetchall()
            bigram_bs_ids: set[str] = set()
            if bs_rows and allow_bigram and near_expr:
                try:
                    for (bid,) in conn.execute(
                        "SELECT bs_id FROM brainstorm_fts "
                        "WHERE brainstorm_fts MATCH ? LIMIT ?",
                        (near_expr, candidate_limit),
                    ).fetchall():
                        bigram_bs_ids.add(str(bid))
                except sqlite3.OperationalError:
                    pass

            for row in bs_rows:
                bid = str(row[0])
                origin_host = str(row[1])
                eff_status = str(row[2])
                updated_utc = str(row[3])
                title = str(row[4])
                vault_rel = str(row[5])
                line_count = int(row[6])
                l0_scent = str(row[7])
                toc_json = str(row[8])
                raw_bm25 = float(row[9])

                base_score = max(0.01, -raw_bm25)
                bi_hit = bid in bigram_bs_ids
                if bi_hit:
                    base_score *= bigram_boost

                if eff_status in ("open", "decided", "implementing"):
                    status_mult = 1.15
                elif eff_status == "shipped":
                    status_mult = 1.05
                else:
                    status_mult = 0.85
                rec_mult = _recency_multiplier(updated_utc, now_utc)
                final_score = round(base_score * status_mult * rec_mult, 4)

                hits.append(
                    RecallHit(
                        kind="brainstorm",
                        item_id=bid,
                        host_id=origin_host,
                        title=title,
                        score=final_score,
                        bm25_raw=round(raw_bm25, 4),
                        bigram_matched=bi_hit,
                        timestamp=updated_utc,
                        vault_path=vault_rel,
                        tier=None,
                        status=eff_status,
                        l1_card=_format_brainstorm_l1_card(
                            bid, origin_host, eff_status, title, line_count, l0_scent, toc_json
                        ),
                    )
                )

        return hits

    hits = _run_pass(or_expr, allow_bigram=True)
    if not hits:
        prefix_expr = _build_or_match_expr(anchor_tokens, prefix=True)
        if prefix_expr:
            used_prefix = True
            hits = _run_pass(prefix_expr, allow_bigram=False)

    hits.sort(key=lambda item: (item.score, item.timestamp, item.item_id), reverse=True)
    return hits[: max(1, int(limit))], used_prefix


# @para-doc [#csa-fts-bigram-recall]
def recall_memory(
    vault_root: Union[str, Path],
    query: str,
    *,
    locale: str = "en",
    host_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 5,
    scope: str = "all",
    auto_sync: bool = False,
    logger: Optional[RotatingJsonLogger] = None,
    _conn: Optional[sqlite3.Connection] = None,
) -> RecallResult:
    """Query SQLite FTS5 with sanitizer, Anchor Gate, 6-col bm25(), and Bigram x3.5 boost."""
    started = time.perf_counter()
    lexicon = get_lexicon(locale)
    folded_query = fold_vietnamese_d(query if isinstance(query, str) else "")
    anchor_tokens = extract_anchor_tokens(folded_query, lexicon)
    bigrams = extract_bigrams(anchor_tokens)

    if status is not None:
        status = validate_brainstorm_status(status)

    if not anchor_tokens:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        if logger is not None:
            logger.log_event(
                "INFO",
                "fts_recall",
                CP_FTS_01,
                duration_ms=elapsed_ms,
                details={
                    "locale": lexicon.locale,
                    "token_count": 0,
                    "bigram_count": 0,
                    "hits_count": 0,
                    "used_prefix_fallback": False,
                    "duration_ms": elapsed_ms,
                },
            )
        return RecallResult(
            locale=lexicon.locale,
            token_count=0,
            bigram_count=0,
            used_prefix_fallback=False,
            duration_ms=elapsed_ms,
            hits=[],
        )

    hits: list[RecallHit] = []
    used_prefix = False

    if _conn is not None and not auto_sync:
        hits, used_prefix = _query_fts_connection(
            _conn,
            anchor_tokens=anchor_tokens,
            bigrams=bigrams,
            bigram_boost=lexicon.bigram_boost,
            host_id=host_id,
            status=status,
            limit=limit,
            scope=scope,
        )
    else:
        vault_resolved = Path(vault_root).expanduser().resolve()
        db_path = vault_resolved / FTS_DB_REL_PATH
        if auto_sync or not db_path.is_file():
            sync_fts_index(vault_resolved, locale=lexicon.locale, logger=logger)
        for attempt in range(2):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(str(db_path), timeout=3.0)
                conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
                hits, used_prefix = _query_fts_connection(
                    conn,
                    anchor_tokens=anchor_tokens,
                    bigrams=bigrams,
                    bigram_boost=lexicon.bigram_boost,
                    host_id=host_id,
                    status=status,
                    limit=limit,
                    scope=scope,
                )
                break
            except sqlite3.OperationalError:
                hits = []
                used_prefix = False
                break
            except sqlite3.DatabaseError:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    conn = None
                if attempt == 0:
                    sync_fts_index(
                        vault_resolved, locale=lexicon.locale, rebuild=True, logger=logger
                    )
                    continue
                raise MipmemError(
                    FTS_INDEX_CORRUPT,
                    f"Local search index is corrupted and could not be rebuilt at {db_path}.",
                )
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    if logger is not None:
        level = "WARN" if elapsed_ms > _SLOW_RECALL_WARN_MS else "INFO"
        logger.log_event(
            level,
            "fts_recall",
            CP_FTS_01,
            duration_ms=elapsed_ms,
            details={
                "locale": lexicon.locale,
                "token_count": len(anchor_tokens),
                "bigram_count": len(bigrams),
                "hits_count": len(hits),
                "used_prefix_fallback": used_prefix,
                "duration_ms": elapsed_ms,
            },
        )

    return RecallResult(
        locale=lexicon.locale,
        token_count=len(anchor_tokens),
        bigram_count=len(bigrams),
        used_prefix_fallback=used_prefix,
        duration_ms=elapsed_ms,
        hits=hits,
    )


class SqliteFtsRecallBackend:
    """Default Zero-Dependency SQLite FTS5 implementation of RecallBackend."""

    def __init__(
        self,
        vault_root: Union[str, Path],
        *,
        locale: str = "en",
        logger: Optional[RotatingJsonLogger] = None,
    ) -> None:
        self._vault_root = Path(vault_root).expanduser().resolve()
        self._db_path = self._vault_root / FTS_DB_REL_PATH
        self._locale = locale
        self._logger = logger
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_inode: Optional[tuple[int, int]] = None
        self._conn_tokenizer: str = DEFAULT_TOKENIZER
        self._conn_journal: str = "wal"

    def close(self) -> None:
        """Close any cached SQLite read/sync connection cleanly."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
            self._conn_inode = None

    def __del__(self) -> None:
        self.close()

    def _get_read_conn(self) -> Optional[sqlite3.Connection]:
        """Return a cached connection, reopening automatically if DB was replaced."""
        try:
            st = os.stat(self._db_path)
            sig = (st.st_dev, st.st_ino)
        except OSError:
            self.close()
            return None

        if self._conn is not None and self._conn_inode == sig:
            return self._conn

        self.close()
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=3.0)
            journal = _configure_connection(conn, is_new=False)
            healthy, tok = _inspect_existing_db(conn)
            if not healthy:
                conn.close()
                return None
            self._conn = conn
            self._conn_inode = sig
            self._conn_tokenizer = tok
            self._conn_journal = journal
            return conn
        except sqlite3.Error:
            return None

    def sync(self, *, rebuild: bool = False) -> IndexSyncReport:
        """Synchronize .cache/global_fts.db from the Sharded Vault."""
        if rebuild:
            self.close()
            return sync_fts_index(
                self._vault_root,
                locale=self._locale,
                rebuild=True,
                logger=self._logger,
            )
        cached_conn = self._get_read_conn()
        report = sync_fts_index(
            self._vault_root,
            locale=self._locale,
            rebuild=False,
            logger=self._logger,
            _conn=cached_conn,
            _cached_tokenizer=self._conn_tokenizer,
            _cached_journal=self._conn_journal,
        )
        if report.self_healed:
            self.close()
        return report

    def recall(
        self,
        query: str,
        *,
        locale: str = "en",
        host_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 5,
        scope: str = "all",
    ) -> RecallResult:
        """Run bigram FTS5 recall against .cache/global_fts.db reusing read connection."""
        active_locale = locale or self._locale
        cached_conn = self._get_read_conn()
        try:
            return recall_memory(
                self._vault_root,
                query,
                locale=active_locale,
                host_id=host_id,
                status=status,
                limit=limit,
                scope=scope,
                logger=self._logger,
                _conn=cached_conn,
            )
        except sqlite3.DatabaseError:
            self.close()
            return recall_memory(
                self._vault_root,
                query,
                locale=active_locale,
                host_id=host_id,
                status=status,
                limit=limit,
                scope=scope,
                logger=self._logger,
            )


def list_indexed_brainstorms(
    vault_root: Union[str, Path],
    *,
    status: Optional[str] = None,
    host_id: Optional[str] = None,
    auto_sync: bool = True,
    logger: Optional[RotatingJsonLogger] = None,
) -> list[dict[str, Any]]:
    """List brainstorms from brainstorm_meta with LWW-converged effective_status."""
    if status is not None:
        status = validate_brainstorm_status(status)
    vault_resolved = Path(vault_root).expanduser().resolve()
    db_path = vault_resolved / FTS_DB_REL_PATH
    if auto_sync or not db_path.is_file():
        sync_fts_index(vault_resolved, logger=logger)

    conn = sqlite3.connect(str(db_path), timeout=3.0)
    try:
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        sql = (
            "SELECT bs_id, origin_host, effective_status, status_updated_utc, title, "
            "vault_path, line_count, est_tokens, l0_scent "
            "FROM brainstorm_meta WHERE 1=1 "
        )
        params: list[Any] = []
        if status is not None:
            sql += "AND effective_status = ? "
            params.append(status)
        if host_id is not None:
            sql += "AND origin_host = ? "
            params.append(host_id)
        sql += "ORDER BY status_updated_utc DESC, bs_id ASC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [
        {
            "bs_id": str(r[0]),
            "origin_host": str(r[1]),
            "effective_status": str(r[2]),
            "status_updated_utc": str(r[3]),
            "title": str(r[4]),
            "vault_path": str(r[5]),
            "line_count": int(r[6]),
            "est_tokens": int(r[7]),
            "l0_scent": str(r[8]),
        }
        for r in rows
    ]


def _resolve_novelty_threshold(
    explicit: Optional[float],
    config: Optional[Mapping[str, Any]],
) -> float:
    """Resolve Topic-Shift novelty threshold in [0.0, 1.0] from arg or config."""
    if explicit is not None:
        return max(0.0, min(1.0, float(explicit)))
    if isinstance(config, Mapping):
        budget = config.get("budget")
        if isinstance(budget, Mapping):
            raw = budget.get("topic_shift_novelty")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return max(0.0, min(1.0, float(raw)))
    return DEFAULT_TOPIC_SHIFT_NOVELTY


def _read_topic_state(state_path: Path) -> dict[str, dict[str, Any]]:
    """Read persisted per-conversation LRU state safely, returning {} on corruption."""
    if not state_path.is_file() or state_path.is_symlink():
        return {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    convs = loaded.get("conversations")
    if not isinstance(convs, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for cid, entry in convs.items():
        if isinstance(cid, str) and isinstance(entry, dict):
            tokens = entry.get("tokens")
            if isinstance(tokens, list):
                clean[cid] = {
                    "tokens": [str(t) for t in tokens if isinstance(t, str)],
                    "updated_at": str(entry.get("updated_at", "")),
                }
    return clean


def _write_topic_state_atomic(state_path: Path, conversations: dict[str, dict[str, Any]]) -> None:
    """Atomically persist .cache/topic_shift_state.json using temp file + os.replace."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(state_path.parent, 0o700)
    payload = json.dumps(
        {"schema_version": 1, "conversations": conversations},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fd, tmp_name = tempfile.mkstemp(dir=str(state_path.parent), prefix=".tmp_topic_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        _best_effort_chmod(Path(tmp_name), 0o600)
        os.replace(tmp_name, state_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


# @para-doc [#csa-fts-topic-shift-gate]
def evaluate_topic_shift(
    vault_root: Union[str, Path],
    conversation_id: str,
    query_text: str,
    *,
    locale: str = "en",
    novelty_threshold: Optional[float] = None,
    config: Optional[Mapping[str, Any]] = None,
    max_conversations: int = DEFAULT_TOPIC_SHIFT_LRU,
    logger: Optional[RotatingJsonLogger] = None,
) -> TopicShiftDecision:
    """Evaluate per-conversation Jaccard novelty with LRU(64) atomic state persistence."""
    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    vault_resolved = root_candidate.resolve()
    state_path = vault_resolved / TOPIC_SHIFT_REL_PATH
    lock_path = state_path.parent / ".topic_shift_state.lock"

    lexicon = get_lexicon(locale)
    threshold = _resolve_novelty_threshold(novelty_threshold, config)
    conv_key = str(conversation_id).strip() or "default"
    new_tokens = sorted(set(extract_anchor_tokens(query_text, lexicon)))
    new_set = set(new_tokens)
    intent_matched = has_cue_match(query_text, lexicon.intent_cues, lexicon)

    with _TOPIC_STATE_LOCK:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd: Optional[int] = None
        if _fcntl is not None:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
            except OSError:
                lock_fd = None

        try:
            conversations = _read_topic_state(state_path)
            prev_entry = conversations.get(conv_key)

            if prev_entry is None:
                novelty = 1.0 if (new_set or intent_matched) else 0.0
                triggered = bool(new_set) or intent_matched
                active_tokens = new_tokens
            else:
                prev_set = set(prev_entry.get("tokens", []))
                union = new_set | prev_set
                if not union:
                    novelty = 0.0
                else:
                    jaccard = len(new_set & prev_set) / len(union)
                    novelty = round(1.0 - jaccard, 4)
                triggered = (novelty >= threshold and bool(new_set)) or intent_matched
                active_tokens = new_tokens if (triggered and new_set) else sorted(prev_set)

            # Refresh LRU order by reinserting conv_key at the end
            if conv_key in conversations:
                del conversations[conv_key]
            conversations[conv_key] = {
                "tokens": active_tokens,
                "updated_at": _utc_timestamp_ms(),
            }

            cap = max(1, int(max_conversations))
            while len(conversations) > cap:
                oldest_key = next(iter(conversations))
                del conversations[oldest_key]

            _write_topic_state_atomic(state_path, conversations)
        finally:
            if lock_fd is not None:
                try:
                    if _fcntl is not None:
                        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass

    if logger is not None:
        logger.log_event(
            "INFO",
            "mipmap",
            CP_MIP_01,
            details={
                "layer": "L1",
                "est_tokens": 0,
                "budget_tokens": 0,
                "truncated": False,
                "conversation_id": conv_key,
                "novelty_score": novelty,
                "triggered": triggered,
            },
        )

    return TopicShiftDecision(
        conversation_id=conv_key,
        novelty_score=novelty,
        threshold=threshold,
        intent_matched=intent_matched,
        triggered=triggered,
        active_tokens=active_tokens,
    )
