"""Conflict-free single-writer Git synchronization backend and detached Stop worker."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Optional, Union

from mipmem.core.diagnostics import (
    CP_SYN_01,
    SYNC_CONFLICT_DETECTED,
    SYNC_TRANSPORT_FAILED,
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
