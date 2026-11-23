"""Single-writer Folder / Syncthing synchronization backend for MipMem."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from typing import Optional, Union

from mipmem.core.diagnostics import (
    CP_SYN_01,
    SYNC_TRANSPORT_FAILED,
    MipmemError,
    RotatingJsonLogger,
    _best_effort_chmod,
    _utc_timestamp_ms,
)
from mipmem.core.redactor import scrub_without_logging
from mipmem.sync.base import SyncResult, acquire_sync_lock, write_sync_status

_IGNORED_DIR_NAMES: frozenset[str] = frozenset({".cache", ".git"})


# @para-doc [#csa-sync-folder-backend]
class FolderSyncBackend:
    """Single-writer folder mirror backend for Syncthing, Tailscale Drive, or local dirs."""

    def __init__(
        self,
        *,
        vault_dir: Union[str, Path],
        cache_dir: Union[str, Path],
        folder_path: Union[str, Path],
        host_id: str,
        locale: str = "en",
        logger: Optional[RotatingJsonLogger] = None,
    ) -> None:
        self.vault_dir = Path(vault_dir).expanduser()
        self.cache_dir = Path(cache_dir).expanduser()
        self.folder_path = Path(str(folder_path).strip()).expanduser()
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

    def _make_result(
        self,
        *,
        direction: str,
        ok: bool,
        files_changed: int = 0,
        error_code: Optional[str] = None,
        message: str = "",
        started: float,
    ) -> SyncResult:
        """Build SyncResult with redacted message and UTC timestamp."""
        return SyncResult(
            backend="folder",
            direction=direction,
            ok=ok,
            files_changed=files_changed,
            conflict=False,
            error_code=error_code,
            message=scrub_without_logging(message),
            host_id=self.host_id,
            last_sync_utc=_utc_timestamp_ms(),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )

    def _validate_folder_root(self, *, direction: str, started: float, create: bool) -> Path:
        """Validate sync folder is non-symlink and neither nested in nor containing vault_dir."""
        try:
            if not str(self.folder_path):
                raise MipmemError(SYNC_TRANSPORT_FAILED, "Folder sync path is empty.")
            if self.folder_path.is_symlink():
                raise MipmemError(
                    SYNC_TRANSPORT_FAILED,
                    f"Refusing symlink sync folder: {self.folder_path.name}.",
                )
            resolved_vault = self.vault_dir.resolve()
            resolved_folder = self.folder_path.resolve()
            if (
                resolved_folder == resolved_vault
                or resolved_folder.is_relative_to(resolved_vault)
                or resolved_vault.is_relative_to(resolved_folder)
            ):
                raise MipmemError(
                    SYNC_TRANSPORT_FAILED,
                    "Sync folder_path cannot match, nest inside, or contain vault_dir.",
                )
            if os.path.lexists(resolved_folder) and not resolved_folder.is_dir():
                raise MipmemError(
                    SYNC_TRANSPORT_FAILED,
                    "Sync folder_path must be a directory.",
                )
            if create:
                resolved_folder.mkdir(parents=True, exist_ok=True)
            return resolved_folder
        except MipmemError as exc:
            err_res = self._make_result(
                direction=direction,
                ok=False,
                error_code=exc.code,
                message=exc.message,
                started=started,
            )
            self._emit("WARN", err_res)
            raise

    @staticmethod
    def _atomic_copy_file(
        src_file: Path,
        dst_file: Path,
        *,
        only_if_newer: bool = False,
    ) -> bool:
        """Atomically copy src_file to dst_file when missing or differing; return True if wrote."""
        raw_bytes = src_file.read_bytes()
        if dst_file.exists():
            if dst_file.is_symlink() or not dst_file.is_file():
                return False
            if dst_file.read_bytes() == raw_bytes:
                return False
            if only_if_newer and src_file.stat().st_mtime_ns <= dst_file.stat().st_mtime_ns:
                return False

        dst_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(dst_file.parent), prefix=".tmp_fsync_")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw_bytes)
            _best_effort_chmod(Path(tmp_name), 0o600)
            os.replace(tmp_name, dst_file)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return True

    def _copy_subtree(
        self,
        src_dir: Path,
        dst_dir: Path,
        *,
        allow_root: Path,
        only_if_newer: bool = False,
    ) -> int:
        """Copy regular non-symlink files from src_dir to dst_dir, skipping .cache and .git."""
        if not src_dir.is_dir() or src_dir.is_symlink():
            return 0
        changed = 0
        resolved_root = allow_root.resolve()
        for root_str, dirnames, filenames in os.walk(src_dir, followlinks=False):
            current_root = Path(root_str)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in _IGNORED_DIR_NAMES and not (current_root / d).is_symlink()
            )
            rel_base = current_root.relative_to(src_dir)
            for fname in sorted(filenames):
                src_file = current_root / fname
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                try:
                    if not src_file.resolve().is_relative_to(resolved_root):
                        continue
                except OSError:
                    continue
                target_file = dst_dir / rel_base / fname
                if self._atomic_copy_file(src_file, target_file, only_if_newer=only_if_newer):
                    changed += 1
        return changed

    def _push_unlocked(self, *, direction: str, started: float) -> SyncResult:
        """Push active host's single-writer shards to folder_path."""
        folder_root = self._validate_folder_root(direction=direction, started=started, create=True)
        vault_resolved = self.vault_dir.expanduser().resolve()
        changed = 0

        host_src = self.vault_dir / "hosts" / self.host_id
        host_dst = folder_root / "hosts" / self.host_id
        changed += self._copy_subtree(host_src, host_dst, allow_root=vault_resolved)

        bs_src = self.vault_dir / "shared" / "brainstorms" / "by-host" / self.host_id
        bs_dst = folder_root / "shared" / "brainstorms" / "by-host" / self.host_id
        changed += self._copy_subtree(bs_src, bs_dst, allow_root=vault_resolved)

        manifest_src = self.vault_dir / "vault_manifest.json"
        manifest_dst = folder_root / "vault_manifest.json"
        if manifest_src.is_file() and not manifest_src.is_symlink() and not manifest_dst.exists():
            if self._atomic_copy_file(manifest_src, manifest_dst):
                changed += 1

        pinned_src = self.vault_dir / "shared" / "pinned_global.md"
        pinned_dst = folder_root / "shared" / "pinned_global.md"
        if pinned_src.is_file() and not pinned_src.is_symlink():
            if self._atomic_copy_file(pinned_src, pinned_dst, only_if_newer=True):
                changed += 1

        res = self._make_result(
            direction=direction,
            ok=True,
            files_changed=changed,
            message=f"Pushed {changed} single-writer file(s) to sync folder.",
            started=started,
        )
        self._emit("INFO", res)
        return res

    def _pull_unlocked(self, *, direction: str, started: float) -> SyncResult:
        """Pull other hosts' shards and shared files without overwriting active host shards."""
        folder_root = self._validate_folder_root(
            direction=direction,
            started=started,
            create=False,
        )
        if not folder_root.is_dir():
            res = self._make_result(
                direction=direction,
                ok=True,
                files_changed=0,
                message="Sync folder does not exist yet; nothing to pull.",
                started=started,
            )
            self._emit("INFO", res)
            return res

        changed = 0
        remote_hosts = folder_root / "hosts"
        if remote_hosts.is_dir() and not remote_hosts.is_symlink():
            for host_dir in sorted(remote_hosts.iterdir()):
                if host_dir.name == self.host_id or host_dir.name in _IGNORED_DIR_NAMES:
                    continue
                if host_dir.is_symlink() or not host_dir.is_dir():
                    continue
                changed += self._copy_subtree(
                    host_dir,
                    self.vault_dir / "hosts" / host_dir.name,
                    allow_root=folder_root,
                )

        remote_bs = folder_root / "shared" / "brainstorms" / "by-host"
        if remote_bs.is_dir() and not remote_bs.is_symlink():
            for host_dir in sorted(remote_bs.iterdir()):
                if host_dir.name == self.host_id or host_dir.name in _IGNORED_DIR_NAMES:
                    continue
                if host_dir.is_symlink() or not host_dir.is_dir():
                    continue
                changed += self._copy_subtree(
                    host_dir,
                    self.vault_dir / "shared" / "brainstorms" / "by-host" / host_dir.name,
                    allow_root=folder_root,
                )

        pinned_remote = folder_root / "shared" / "pinned_global.md"
        pinned_local = self.vault_dir / "shared" / "pinned_global.md"
        if (
            pinned_remote.is_file()
            and not pinned_remote.is_symlink()
            and pinned_remote.resolve().is_relative_to(folder_root)
        ):
            if self._atomic_copy_file(pinned_remote, pinned_local, only_if_newer=True):
                changed += 1

        if changed > 0:
            try:
                from mipmem.core.fts_recall import sync_fts_index

                sync_fts_index(self.vault_dir, locale=self.locale, logger=self.logger)
            except Exception:
                pass

        res = self._make_result(
            direction=direction,
            ok=True,
            files_changed=changed,
            message=f"Pulled {changed} file(s) from other hosts.",
            started=started,
        )
        self._emit("INFO", res)
        return res

    def pull(self) -> SyncResult:
        """Pull foreign shards from folder_path safely under sync.lock."""
        started = time.perf_counter()
        with acquire_sync_lock(self.cache_dir):
            return self._pull_unlocked(direction="pull", started=started)

    def push(self) -> SyncResult:
        """Push active host shards to folder_path safely under sync.lock."""
        started = time.perf_counter()
        with acquire_sync_lock(self.cache_dir):
            return self._push_unlocked(direction="push", started=started)

    def sync(self) -> SyncResult:
        """Execute bidirectional single-writer folder sync under sync.lock."""
        started = time.perf_counter()
        with acquire_sync_lock(self.cache_dir):
            pull_res = self._pull_unlocked(direction="sync", started=started)
            push_res = self._push_unlocked(direction="sync", started=started)
            total_changed = pull_res.files_changed + push_res.files_changed
            final = self._make_result(
                direction="sync",
                ok=True,
                files_changed=total_changed,
                message=(
                    f"Folder sync complete (pulled {pull_res.files_changed}, "
                    f"pushed {push_res.files_changed})."
                ),
                started=started,
            )
            self._emit("INFO", final)
            return final
