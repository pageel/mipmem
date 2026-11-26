"""Conflict-free single-writer Git synchronization backend and detached Stop worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Optional, Union

from mipmem.core.diagnostics import (
    CLI_INVALID_ARGUMENT,
    CP_SYN_01,
    JOIN_HOST_CONFLICT,
    JOIN_VAULT_OCCUPIED,
    SYNC_CONFLICT_DETECTED,
    SYNC_TRANSPORT_FAILED,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    RotatingJsonLogger,
    _utc_timestamp_ms,
)
from mipmem.core.redactor import scrub_without_logging
from mipmem.sync.base import (
    SyncResult,
    acquire_sync_lock as _sync_lock,
    is_sync_configured,
    write_sync_status,
)


_REQUIRED_GITIGNORE_LINES: tuple[str, ...] = (
    ".cache/",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.lock",
)


def _git_env() -> dict[str, str]:
    """Build non-interactive subprocess environment enforcing GIT_TERMINAL_PROMPT=0."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LC_ALL", "C")
    return env


def _run_git(
    vault_dir: Path,
    args: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run one git CLI command inside vault_dir with shell=False and prompt disabled."""
    return subprocess.run(
        ["git", "-C", str(vault_dir), *args],
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_git_env(),
    )


def _has_rebase_state(vault_dir: Path) -> bool:
    """Return True if git left an active rebase directory inside .git/."""
    git_dir = vault_dir / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


# @para-doc [#csa-sync-git-backend]
class GitSyncBackend:
    """Conflict-free Git vault sync enforcing single-writer staging and rebase safety."""

    def __init__(
        self,
        *,
        vault_dir: Union[str, Path],
        cache_dir: Union[str, Path],
        remote_url: str,
        host_id: str,
        locale: str = "en",
        logger: Optional[RotatingJsonLogger] = None,
    ) -> None:
        self.vault_dir = Path(vault_dir).expanduser()
        self.cache_dir = Path(cache_dir).expanduser()
        self.remote_url = str(remote_url).strip()
        self.host_id = host_id
        self.locale = locale
        self.logger = logger

    def _emit(self, level: str, result: SyncResult) -> None:
        """Persist sync_status.json and emit CP-SYN-01 telemetry safely."""
        write_sync_status(self.cache_dir, result)
        if self.logger is not None:
            self.logger.log_event(
                level,
                "sync",
                CP_SYN_01,
                error_code=result.error_code,
                duration_ms=result.duration_ms,
                details={
                    "backend": result.backend,
                    "direction": result.direction,
                    "files_changed": result.files_changed,
                    "conflict": result.conflict,
                    "ok": result.ok,
                    "host_id": self.host_id,
                },
            )

    def _ensure_gitignore(self) -> None:
        """Ensure .gitignore excludes .cache/ and transient SQLite/lock files."""
        gi_path = self.vault_dir / ".gitignore"
        existing = gi_path.read_text(encoding="utf-8") if gi_path.is_file() else ""
        lines = [line.rstrip("\r\n") for line in existing.splitlines()]
        missing = [req for req in _REQUIRED_GITIGNORE_LINES if req not in lines]
        if missing:
            updated = lines + missing
            gi_path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")

    def _ensure_repo(self) -> str:
        """Initialize vault git repository, core.filemode=false, origin remote, and branch."""
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        if not (self.vault_dir / ".git").exists():
            init_proc = _run_git(self.vault_dir, ["init"])
            if init_proc.returncode != 0:
                raise RuntimeError(init_proc.stderr.strip() or "git init failed")

        _run_git(self.vault_dir, ["config", "core.filemode", "false"])
        name_proc = _run_git(self.vault_dir, ["config", "--get", "user.name"])
        if not name_proc.stdout.strip():
            _run_git(self.vault_dir, ["config", "user.name", "MipMem Sync"])
        email_proc = _run_git(self.vault_dir, ["config", "--get", "user.email"])
        if not email_proc.stdout.strip():
            _run_git(self.vault_dir, ["config", "user.email", "mipmem@localhost.invalid"])

        self._ensure_gitignore()

        remote_proc = _run_git(self.vault_dir, ["remote", "get-url", "origin"])
        if remote_proc.returncode != 0:
            _run_git(self.vault_dir, ["remote", "add", "origin", self.remote_url])
        elif remote_proc.stdout.strip() != self.remote_url:
            _run_git(self.vault_dir, ["remote", "set-url", "origin", self.remote_url])

        sym = _run_git(self.vault_dir, ["symbolic-ref", "--short", "HEAD"])
        branch = sym.stdout.strip() if sym.returncode == 0 else ""
        if not branch:
            rev = _run_git(self.vault_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
            cand = rev.stdout.strip() if rev.returncode == 0 else ""
            if cand and cand != "HEAD":
                branch = cand
        return branch or "main"

    def _head_sha(self) -> str:
        """Return HEAD SHA if commits exist, otherwise empty string."""
        proc = _run_git(self.vault_dir, ["rev-parse", "HEAD"])
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _stage_and_commit_single_writer(self) -> int:
        """Stage and commit ONLY this host's single-writer shards and shared manifests."""
        candidates = [
            ".gitignore",
            "AGENTS.md",
            "README.md",
            "vault_manifest.json",
            "shared/pinned_global.md",
            f"hosts/{self.host_id}",
            f"shared/brainstorms/by-host/{self.host_id}",
        ]
        existing = [rel for rel in candidates if (self.vault_dir / rel).exists()]
        if not existing:
            return 0
        _run_git(self.vault_dir, ["add", "--", *existing])
        _run_git(self.vault_dir, ["reset", "-q", "--", ".cache"])
        staged = _run_git(self.vault_dir, ["diff", "--cached", "--name-only"])
        changed_files = [line for line in staged.stdout.splitlines() if line.strip()]
        if not changed_files:
            return 0
        commit_proc = _run_git(
            self.vault_dir,
            ["commit", "-m", f"sync({self.host_id}): update host shards"],
        )
        if commit_proc.returncode != 0:
            detail = scrub_without_logging(commit_proc.stderr or commit_proc.stdout)
            raise RuntimeError(detail.strip() or "git commit failed")
        return len(changed_files)

    def _resolve_remote_branch(self, branch: str) -> Optional[str]:
        """Fetch origin and return active remote branch name, or None when remote is empty."""
        fetch_proc = _run_git(self.vault_dir, ["fetch", "origin"])
        if fetch_proc.returncode != 0:
            err_msg = scrub_without_logging(fetch_proc.stderr or fetch_proc.stdout).strip()
            raise ConnectionError(err_msg or "git fetch origin failed")

        ref_check = _run_git(
            self.vault_dir,
            ["show-ref", "--verify", f"refs/remotes/origin/{branch}"],
        )
        if ref_check.returncode == 0:
            return branch

        heads = _run_git(self.vault_dir, ["ls-remote", "--heads", "origin"])
        if heads.returncode != 0:
            raise ConnectionError(scrub_without_logging(heads.stderr).strip() or "ls-remote failed")
        remote_branches = [
            line.split("refs/heads/", 1)[1].strip()
            for line in heads.stdout.splitlines()
            if "refs/heads/" in line
        ]
        if not remote_branches:
            return None
        return remote_branches[0]

    def _count_diff_files(self, before_sha: str, after_sha: str) -> int:
        """Count changed files between two revisions safely."""
        if not after_sha:
            return 0
        if not before_sha:
            ls_proc = _run_git(self.vault_dir, ["ls-tree", "-r", "--name-only", after_sha])
            return len([line for line in ls_proc.stdout.splitlines() if line.strip()])
        if before_sha == after_sha:
            return 0
        diff_proc = _run_git(self.vault_dir, ["diff", "--name-only", before_sha, after_sha])
        return len([line for line in diff_proc.stdout.splitlines() if line.strip()])

    def _refresh_fts_if_needed(self, changed: int) -> None:
        """Trigger incremental SQLite FTS5 sync after pulling remote changes."""
        if changed <= 0:
            return
        try:
            from mipmem.core.fts_recall import sync_fts_index

            sync_fts_index(self.vault_dir, locale=self.locale, logger=self.logger)
        except Exception:
            pass

    def _make_result(
        self,
        *,
        direction: str,
        ok: bool,
        files_changed: int = 0,
        conflict: bool = False,
        error_code: Optional[str] = None,
        message: str = "",
        started: float,
    ) -> SyncResult:
        """Build SyncResult with redacted message, UTC timestamp, and elapsed ms."""
        return SyncResult(
            backend="git",
            direction=direction,
            ok=ok,
            files_changed=files_changed,
            conflict=conflict,
            error_code=error_code,
            message=scrub_without_logging(message),
            host_id=self.host_id,
            last_sync_utc=_utc_timestamp_ms(),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )

    def _pull_unlocked(self, *, direction: str, started: float) -> SyncResult:
        """Execute git fetch + pull --rebase --autostash while holding _sync_lock."""
        branch = self._ensure_repo()
        head_before = self._head_sha()

        try:
            remote_branch = self._resolve_remote_branch(branch)
        except ConnectionError as exc:
            res = self._make_result(
                direction=direction,
                ok=False,
                error_code=SYNC_TRANSPORT_FAILED,
                message=str(exc),
                started=started,
            )
            self._emit("WARN", res)
            return res

        if not head_before:
            if remote_branch is None:
                res = self._make_result(
                    direction=direction,
                    ok=True,
                    files_changed=0,
                    message="Remote repository has no commits yet.",
                    started=started,
                )
                self._emit("INFO", res)
                return res

            co_proc = _run_git(
                self.vault_dir,
                ["checkout", "-f", "-B", remote_branch, f"origin/{remote_branch}"],
            )
            if co_proc.returncode != 0:
                res = self._make_result(
                    direction=direction,
                    ok=False,
                    error_code=SYNC_TRANSPORT_FAILED,
                    message=co_proc.stderr or co_proc.stdout,
                    started=started,
                )
                self._emit("WARN", res)
                return res
            pulled_count = self._count_diff_files("", self._head_sha())
            self._refresh_fts_if_needed(pulled_count)
            res = self._make_result(
                direction=direction,
                ok=True,
                files_changed=pulled_count,
                message=f"Initialized local vault from origin/{remote_branch}.",
                started=started,
            )
            self._emit("INFO", res)
            return res

        local_commits = self._stage_and_commit_single_writer()
        head_before = self._head_sha()
        if remote_branch is None:
            res = self._make_result(
                direction=direction,
                ok=True,
                files_changed=local_commits,
                message="Remote repository has no commits yet.",
                started=started,
            )
            self._emit("INFO", res)
            return res

        pull_proc = _run_git(
            self.vault_dir,
            ["pull", "--rebase", "--autostash", "origin", remote_branch],
        )
        if pull_proc.returncode != 0:
            combined = f"{pull_proc.stdout}\n{pull_proc.stderr}".strip()
            if _has_rebase_state(self.vault_dir) or "CONFLICT" in combined:
                _run_git(self.vault_dir, ["rebase", "--abort"])
                res = self._make_result(
                    direction=direction,
                    ok=False,
                    conflict=True,
                    error_code=SYNC_CONFLICT_DETECTED,
                    message="Manual merge conflict detected; aborted rebase safely.",
                    started=started,
                )
                self._emit("ERROR", res)
                return res

            res = self._make_result(
                direction=direction,
                ok=False,
                error_code=SYNC_TRANSPORT_FAILED,
                message=combined or "git pull --rebase failed",
                started=started,
            )
            self._emit("WARN", res)
            return res

        head_after = self._head_sha()
        pulled_count = self._count_diff_files(head_before, head_after)
        self._refresh_fts_if_needed(pulled_count)
        res = self._make_result(
            direction=direction,
            ok=True,
            files_changed=pulled_count + local_commits,
            message=f"Pulled {pulled_count} remote file change(s).",
            started=started,
        )
        self._emit("INFO", res)
        return res

    def _push_unlocked(self, *, direction: str, started: float) -> SyncResult:
        """Stage single-writer shards, push to origin, and handle non-fast-forward."""
        branch = self._ensure_repo()
        committed = self._stage_and_commit_single_writer()
        if not self._head_sha():
            res = self._make_result(
                direction=direction,
                ok=True,
                files_changed=0,
                message="Nothing to push.",
                started=started,
            )
            self._emit("INFO", res)
            return res

        push_proc = _run_git(self.vault_dir, ["push", "-u", "origin", branch])
        if push_proc.returncode != 0:
            combined = f"{push_proc.stdout}\n{push_proc.stderr}".lower()
            if (
                "non-fast-forward" in combined
                or "fetch first" in combined
                or "rejected" in combined
            ):
                pull_res = self._pull_unlocked(direction=direction, started=started)
                if not pull_res.ok:
                    return pull_res
                push_proc = _run_git(self.vault_dir, ["push", "-u", "origin", branch])
                committed += pull_res.files_changed

        if push_proc.returncode != 0:
            err_msg = (push_proc.stderr or push_proc.stdout).strip() or "git push failed"
            res = self._make_result(
                direction=direction,
                ok=False,
                error_code=SYNC_TRANSPORT_FAILED,
                message=err_msg,
                started=started,
            )
            self._emit("WARN", res)
            return res

        res = self._make_result(
            direction=direction,
            ok=True,
            files_changed=committed,
            message=f"Synced host '{self.host_id}' with origin/{branch}.",
            started=started,
        )
        self._emit("INFO", res)
        return res

    def pull(self) -> SyncResult:
        """Pull remote host shards and shared files cleanly under sync.lock."""
        started = time.perf_counter()
        with _sync_lock(self.cache_dir):
            return self._pull_unlocked(direction="pull", started=started)

    def push(self) -> SyncResult:
        """Push active host single-writer shards under sync.lock."""
        started = time.perf_counter()
        with _sync_lock(self.cache_dir):
            return self._push_unlocked(direction="push", started=started)

    def sync(self) -> SyncResult:
        """Run full commit -> pull --rebase -> push cycle under sync.lock."""
        started = time.perf_counter()
        with _sync_lock(self.cache_dir):
            pull_res = self._pull_unlocked(direction="sync", started=started)
            if not pull_res.ok:
                return pull_res
            push_res = self._push_unlocked(direction="sync", started=started)
            if not push_res.ok:
                return push_res
            total_changed = max(pull_res.files_changed, push_res.files_changed)
            final = self._make_result(
                direction="sync",
                ok=True,
                files_changed=total_changed,
                message=push_res.message or pull_res.message,
                started=started,
            )
            self._emit("INFO", final)
            return final


_TBD_MARKER = "[" + "TBD]"


@dataclass(frozen=True)
class JoinResult:
    """Result returned by join_git_vault for CLI JSON envelopes and diagnostics."""

    ok: bool
    host_id: str
    existing_hosts: list[str]
    vault_dir: str
    config_file: str
    remote_url: str
    adopt_host: bool
    indexed_sessions: int
    indexed_brainstorms: int
    guides_written: list[str]
    sync_result: Optional[SyncResult] = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize JoinResult into a JSON-compatible dictionary."""
        payload = asdict(self)
        payload["sync_result"] = (
            self.sync_result.to_dict() if self.sync_result is not None else None
        )
        return payload


def validate_git_remote_url(remote_url: str) -> str:
    """Validate a Git remote URL/path while rejecting CLI flag injection and placeholders."""
    cleaned = str(remote_url or "").strip() if isinstance(remote_url, str) else ""
    if (
        not cleaned
        or cleaned == _TBD_MARKER
        or cleaned.startswith("-")
        or "\x00" in cleaned
        or "\n" in cleaned
        or "\r" in cleaned
    ):
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Invalid Git remote URL {remote_url!r}: cannot be empty or start with '-'.",
        )
    return cleaned


def _normalize_remote_url(raw_url: str) -> str:
    """Normalize local filesystem paths and Git remote URLs for origin comparison."""
    cleaned = raw_url.strip().rstrip("/")
    if "://" not in cleaned and not cleaned.startswith("git@"):
        try:
            cleaned = str(Path(cleaned).expanduser().resolve()).rstrip("/")
        except OSError:
            pass
    if len(cleaned) > 4 and cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned


def _scan_existing_vault_hosts(vault_dir: Path) -> list[str]:
    """Return sorted host_id directory names under vault/hosts/ without following symlinks."""
    from mipmem.core.vault import HOST_ID_RE

    hosts_dir = vault_dir / "hosts"
    if not hosts_dir.is_dir() or hosts_dir.is_symlink():
        return []
    root_resolved = vault_dir.resolve()
    found: list[str] = []
    for entry in sorted(hosts_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not entry.resolve().is_relative_to(root_resolved):
            continue
        if HOST_ID_RE.match(entry.name):
            found.append(entry.name)
    return found


def _prepare_git_clone_or_verify_existing(
    clean_url: str,
    vault_path: Path,
) -> None:
    """Verify existing vault_path is safe/matching or clone clean_url with shell=False."""
    if vault_path.exists():
        if not vault_path.is_dir():
            raise MipmemError(
                JOIN_VAULT_OCCUPIED,
                f"Target vault path '{vault_path}' exists and is not a directory. "
                "Pass --vault <new_dir> instead.",
            )
        git_entry = vault_path / ".git"
        if git_entry.exists():
            origin_proc = _run_git(vault_path, ["remote", "get-url", "origin"])
            origin_url = origin_proc.stdout.strip() if origin_proc.returncode == 0 else ""
            if not origin_url or (
                _normalize_remote_url(origin_url) != _normalize_remote_url(clean_url)
            ):
                raise MipmemError(
                    JOIN_VAULT_OCCUPIED,
                    f"Target vault directory '{vault_path}' is already occupied by a different "
                    "repository or files. Pass --vault <new_dir> instead.",
                )
            pull_proc = _run_git(vault_path, ["fetch", "origin"])
            if pull_proc.returncode != 0:
                detail = scrub_without_logging(pull_proc.stderr or pull_proc.stdout).strip()
                raise MipmemError(
                    SYNC_TRANSPORT_FAILED,
                    f"Git join transport failed: {detail or 'git fetch failed'}",
                )
            return
        if any(vault_path.iterdir()):
            raise MipmemError(
                JOIN_VAULT_OCCUPIED,
                f"Target vault directory '{vault_path}' is already occupied by a different "
                "repository or files. Pass --vault <new_dir> instead.",
            )

    vault_path.parent.mkdir(parents=True, exist_ok=True)
    clone_proc = subprocess.run(
        ["git", "clone", "--", clean_url, str(vault_path)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
        env=_git_env(),
    )
    if clone_proc.returncode != 0:
        detail = scrub_without_logging(clone_proc.stderr or clone_proc.stdout).strip()
        raise MipmemError(
            SYNC_TRANSPORT_FAILED,
            f"Git join transport failed: {detail or 'git clone failed'}",
        )


# @para-doc [#csa-sync-git-join-engine]
def join_git_vault(
    remote_url: str,
    *,
    vault_dir: Union[str, Path],
    cache_dir: Union[str, Path],
    config_file: Union[str, Path],
    host_id: Optional[str] = None,
    adopt_host: bool = False,
    locale: Optional[str] = None,
    auto_sync_on_stop: bool = True,
    skip_sync: bool = False,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> JoinResult:
    """Clone a private Git vault, enforce collision guards, configure host, index FTS, and sync."""
    from mipmem.config import default_config, load_config, save_config
    from mipmem.core.fts_recall import sync_fts_index
    from mipmem.core.vault import default_host_id, init_vault, validate_host_id

    started = time.perf_counter()
    clean_url = validate_git_remote_url(remote_url)

    vault_candidate = Path(vault_dir).expanduser()
    if vault_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    vault_path = vault_candidate.resolve()

    cfg_candidate = Path(config_file).expanduser()
    if cfg_candidate.is_symlink() or cfg_candidate.is_dir():
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Invalid config path: {config_file!r}.",
        )

    cache_path = Path(cache_dir).expanduser()
    active_logger = logger if logger is not None else RotatingJsonLogger(
        log_path=cache_path / "mipmem.log"
    )
    target_host = default_host_id() if host_id is None else validate_host_id(host_id)

    try:
        _prepare_git_clone_or_verify_existing(clean_url, vault_path)
    except MipmemError as exc:
        active_logger.log_event(
            "ERROR",
            "sync",
            CP_SYN_01,
            error_code=exc.code,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            details={
                "direction": "join",
                "host_id": target_host,
                "adopt_host": adopt_host,
                "existing_hosts_count": 0,
                "ok": False,
            },
        )
        raise

    _run_git(vault_path, ["config", "core.filemode", "false"])
    name_proc = _run_git(vault_path, ["config", "--get", "user.name"])
    if not name_proc.stdout.strip():
        _run_git(vault_path, ["config", "user.name", "MipMem Sync"])
    email_proc = _run_git(vault_path, ["config", "--get", "user.email"])
    if not email_proc.stdout.strip():
        _run_git(vault_path, ["config", "user.email", "mipmem@localhost.invalid"])

    existing_hosts = _scan_existing_vault_hosts(vault_path)
    if target_host in existing_hosts and not adopt_host:
        active_logger.log_event(
            "WARN",
            "sync",
            CP_SYN_01,
            error_code=JOIN_HOST_CONFLICT,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            details={
                "direction": "join",
                "host_id": target_host,
                "adopt_host": adopt_host,
                "existing_hosts_count": len(existing_hosts),
                "ok": False,
            },
        )
        raise MipmemError(
            JOIN_HOST_CONFLICT,
            f"Host ID '{target_host}' already exists in vault "
            f"(existing: {', '.join(existing_hosts)}). "
            "Pick another --host or pass --adopt-host to reclaim it.",
        )

    had_agents = (vault_path / "AGENTS.md").exists()
    had_readme = (vault_path / "README.md").exists()
    init_vault(vault_path, host_id=target_host, home_dir=home_dir, logger=active_logger)
    guides_written: list[str] = []
    if not had_agents and (vault_path / "AGENTS.md").exists():
        guides_written.append("AGENTS.md")
    if not had_readme and (vault_path / "README.md").exists():
        guides_written.append("README.md")

    cfg = load_config(cfg_candidate) if cfg_candidate.is_file() else default_config()
    core_tbl = cfg.setdefault("core", {})
    if isinstance(core_tbl, dict):
        core_tbl["host_id"] = target_host
        core_tbl["vault_path"] = str(vault_path)
        if locale is not None and str(locale).strip():
            core_tbl["locale"] = str(locale).strip().lower()
    sync_tbl = cfg.setdefault("sync", {})
    if isinstance(sync_tbl, dict):
        sync_tbl["backend"] = "git"
        sync_tbl["vault_remote_url"] = clean_url
        sync_tbl["auto_sync_on_stop"] = bool(auto_sync_on_stop)
    saved_cfg = save_config(cfg, cfg_candidate)

    resolved_locale = str(cfg.get("core", {}).get("locale", "en")).strip().lower() or "en"
    fts_report = sync_fts_index(
        vault_path,
        locale=resolved_locale,
        rebuild=True,
        logger=active_logger,
    )

    sync_res: Optional[SyncResult] = None
    if not skip_sync:
        backend = GitSyncBackend(
            vault_dir=vault_path,
            cache_dir=cache_path,
            remote_url=clean_url,
            host_id=target_host,
            locale=resolved_locale,
            logger=active_logger,
        )
        sync_res = backend.sync()
        if not sync_res.ok:
            raise MipmemError(
                sync_res.error_code or SYNC_TRANSPORT_FAILED,
                sync_res.message or "Git sync failed after join.",
            )

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    active_logger.log_event(
        "INFO",
        "sync",
        CP_SYN_01,
        duration_ms=elapsed_ms,
        details={
            "direction": "join",
            "host_id": target_host,
            "adopt_host": adopt_host,
            "existing_hosts_count": len(existing_hosts),
            "ok": True,
        },
    )
    return JoinResult(
        ok=True,
        host_id=target_host,
        existing_hosts=existing_hosts,
        vault_dir=str(vault_path),
        config_file=str(saved_cfg),
        remote_url=clean_url,
        adopt_host=adopt_host,
        indexed_sessions=fts_report.indexed_sessions,
        indexed_brainstorms=fts_report.indexed_brainstorms,
        guides_written=guides_written,
        sync_result=sync_res,
        duration_ms=elapsed_ms,
    )


def _detect_running_pyz() -> Optional[Path]:
    """Return the enclosing .pyz path when executing inside a zipapp bundle, else None."""
    current = Path(__file__).resolve()
    for candidate in (current, *current.parents):
        if candidate.name.endswith(".pyz") and candidate.is_file():
            return candidate
    if sys.argv and sys.argv[0].endswith(".pyz"):
        arg_path = Path(sys.argv[0]).expanduser().resolve()
        if arg_path.is_file():
            return arg_path
    return None


def _build_detached_sync_argv(
    vault_dir: Path,
    config_file: Optional[Path],
) -> tuple[list[str], bool]:
    """Build subprocess argv for detached `mipmem sync --detached` invocation."""
    pyz_path = _detect_running_pyz()
    if pyz_path is not None:
        argv = [sys.executable, str(pyz_path), "sync", "--detached", "--vault", str(vault_dir)]
        uses_pyz = True
    else:
        argv = [sys.executable, "-m", "mipmem", "sync", "--detached", "--vault", str(vault_dir)]
        uses_pyz = False
    if config_file is not None:
        argv.extend(["--config", str(config_file)])
    return argv, uses_pyz


# @para-doc [#csa-sync-detached-stop-worker]
def spawn_detached_sync(
    *,
    vault_dir: Union[str, Path],
    config_file: Optional[Union[str, Path]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> bool:
    """Spawn a detached background `mipmem sync --detached` worker (0ms UI wait on Stop)."""
    started = time.perf_counter()
    if not isinstance(cfg, Mapping):
        return False
    sync_tbl = cfg.get("sync", {})
    if not isinstance(sync_tbl, Mapping) or not bool(sync_tbl.get("auto_sync_on_stop", True)):
        return False

    configured, backend_name, _ = is_sync_configured(cfg)
    if not configured:
        return False

    resolved_vault = Path(vault_dir).expanduser()
    resolved_cfg = Path(config_file).expanduser() if config_file is not None else None
    argv, uses_pyz = _build_detached_sync_argv(resolved_vault, resolved_cfg)
    env = _git_env()
    if not uses_pyz and "PYTHONPATH" not in env:
        src_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src_root

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except OSError as exc:
        if logger is not None:
            logger.log_event(
                "WARN",
                "sync",
                CP_SYN_01,
                error_code=SYNC_TRANSPORT_FAILED,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
                details={
                    "backend": backend_name,
                    "direction": "detached_spawn",
                    "ok": False,
                    "reason": str(exc),
                },
            )
        return False

    if logger is not None:
        logger.log_event(
            "INFO",
            "sync",
            CP_SYN_01,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            details={
                "backend": backend_name,
                "direction": "detached_spawn",
                "ok": True,
            },
        )
    return True
