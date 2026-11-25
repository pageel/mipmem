"""Opt-in verified zipapp self-updater and install-mode guard for MipMem."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import zipfile

from mipmem import __version__
from mipmem.core.diagnostics import (
    CP_UPD_01,
    UPDATE_VERIFICATION_FAILED,
    MipmemError,
    RotatingJsonLogger,
    _best_effort_chmod,
)

GITHUB_OWNER = "pageel"
GITHUB_REPO = "mipmem"
DEFAULT_RELEASES_API_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)
RELEASE_BY_TAG_API_TEMPLATE = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{{tag}}"
)
ALLOWED_RELEASE_ASSET_HOSTS: frozenset[str] = frozenset(
    {
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
MAX_PYZ_BYTES = 10 * 1024 * 1024
MAX_CHECKSUMS_BYTES = 64 * 1024
DEFAULT_HTTP_TIMEOUT_SEC = 15.0

_SHA256_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?([^\s].*?)\s*$")
_VERSION_RE = re.compile(
    r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True)
class UpdateResult:
    """Structured result emitted by MipMem self-update checks and applies."""

    install_mode: str
    target_path: str
    current_version: str
    target_version: str = ""
    update_available: bool = False
    dry_run: bool = False
    check_only: bool = False
    sha256_verified: bool = False
    health_verified: bool = False
    applied: bool = False
    recommended_command: str = ""
    error_code: Optional[str] = None
    message: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize this update result into a JSON-compatible dictionary."""
        return asdict(self)


def recommended_upgrade_command(install_mode: str) -> str:
    """Return the canonical human upgrade command for each installation channel."""
    mapping = {
        "zipapp": "mipmem self-update",
        "uv": "uv tool upgrade mipmem",
        "pipx": "pipx upgrade mipmem",
        "pip": "python3 -m pip install --upgrade mipmem",
        "plugin": "Update MipMem via your Antigravity IDE or CLI Plugin Manager.",
        "git-dev": "git pull --ff-only",
    }
    return mapping.get(install_mode, "python3 -m pip install --upgrade mipmem")


# @para-doc [#csa-updater-install-mode]
def detect_install_mode(
    *,
    module_file: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[str, Path]:
    """Detect active runtime channel: zipapp, uv, pipx, pip, plugin, or git-dev."""
    env_map = os.environ if env is None else env
    raw_file = Path(module_file if module_file is not None else __file__).expanduser()

    parts = raw_file.parts
    for idx, part in enumerate(parts):
        if part.lower().endswith(".pyz"):
            zip_path = Path(*parts[: idx + 1])
            if zip_path.parent.exists():
                return "zipapp", zip_path.parent.resolve() / zip_path.name
            return "zipapp", zip_path

    # Check argv[0] when invoked directly as a .pyz script in zipapp mode.
    if module_file is None and sys.argv:
        argv_zero = sys.argv[0].strip()
        if argv_zero.lower().endswith(".pyz"):
            p_argv = Path(argv_zero).expanduser()
            if p_argv.exists():
                return "zipapp", p_argv.resolve()
            return "zipapp", p_argv

    lowered_parts = tuple(p.lower() for p in parts)
    for idx in range(len(lowered_parts) - 1):
        if lowered_parts[idx] == "uv" and lowered_parts[idx + 1] == "tools":
            return "uv", raw_file.parent
        if lowered_parts[idx] == "pipx" and lowered_parts[idx + 1] == "venvs":
            return "pipx", raw_file.parent

    if "site-packages" in lowered_parts or "dist-packages" in lowered_parts:
        return "pip", raw_file.parent

    candidates = [raw_file.parent, *raw_file.parents]
    for parent in candidates:
        if (parent / ".git").exists():
            resolved = parent.resolve() if parent.exists() else parent
            return "git-dev", resolved

    plugin_root = str(
        env_map.get("PLUGIN_ROOT") or env_map.get("CLAUDE_PLUGIN_ROOT") or ""
    ).strip()
    if plugin_root:
        p_root = Path(plugin_root).expanduser()
        return "plugin", (p_root.resolve() if p_root.exists() else p_root)

    for parent in candidates:
        if (parent / "plugin.json").is_file():
            resolved = parent.resolve() if parent.exists() else parent
            return "plugin", resolved

    return "pip", raw_file.parent


def validate_release_url(
    url: str,
    *,
    allow_localhost_http: bool = False,
) -> str:
    """Enforce strict HTTPS allowlist for official pageel/mipmem GitHub release URLs."""
    cleaned = str(url or "").strip()
    if not cleaned:
        raise MipmemError(UPDATE_VERIFICATION_FAILED, "Release URL must not be empty.")

    parsed = urlsplit(cleaned)
    if parsed.username is not None or parsed.password is not None:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            "Release URL must not contain embedded user credentials.",
        )

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if scheme == "http":
        if allow_localhost_http and host in ("127.0.0.1", "localhost", "::1"):
            return cleaned
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Refusing insecure HTTP release URL: {host or cleaned}.",
        )

    if scheme != "https":
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Unsupported URL scheme {scheme!r} for release download.",
        )

    if allow_localhost_http and host in ("127.0.0.1", "localhost", "::1"):
        return cleaned

    api_prefix = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
    if host == "api.github.com" and (path == api_prefix or path.startswith(api_prefix + "/")):
        return cleaned

    web_prefix = f"/{GITHUB_OWNER}/{GITHUB_REPO}/releases/"
    if host == "github.com" and path.startswith(web_prefix):
        return cleaned

    if host in ALLOWED_RELEASE_ASSET_HOSTS and path.startswith("/"):
        return cleaned

    raise MipmemError(
        UPDATE_VERIFICATION_FAILED,
        f"Release URL host or path is outside official pageel/mipmem allowlist: {cleaned}.",
    )


def parse_sha256sums(manifest_text: str) -> dict[str, str]:
    """Parse GNU sha256sum text into a filename -> lowercase hex digest mapping."""
    checksums: dict[str, str] = {}
    for line_no, raw_line in enumerate(manifest_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SHA256_LINE_RE.match(stripped)
        if not match:
            raise MipmemError(
                UPDATE_VERIFICATION_FAILED,
                f"Malformed SHA256SUMS entry at line {line_no}.",
            )
        digest = match.group(1).lower()
        asset_name = match.group(2).strip().replace("\\", "/")
        checksums[asset_name] = digest
        checksums[Path(asset_name).name] = digest

    if not checksums:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            "SHA256SUMS manifest contained no valid checksum entries.",
        )
    return checksums


def _parse_version_tuple(version: str) -> tuple[int, int, int, int, tuple[str, ...]]:
    """Parse semantic version string into comparable tuple with pre-release handling."""
    match = _VERSION_RE.match(str(version or "").strip())
    if not match:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Invalid semantic version string: {version!r}.",
        )
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    pre = match.group(4) or ""
    # Stable release (1) sorts higher than any pre-release (0).
    is_stable = 0 if pre else 1
    pre_parts = tuple(pre.split(".")) if pre else ()
    return major, minor, patch, is_stable, pre_parts


def compare_versions(current: str, target: str) -> int:
    """Compare semantic versions returning -1 (current < target), 0, or 1."""
    cur_t = _parse_version_tuple(current)
    tgt_t = _parse_version_tuple(target)
    if cur_t < tgt_t:
        return -1
    if cur_t > tgt_t:
        return 1
    return 0


class _ValidatingRedirectHandler(HTTPRedirectHandler):
    """Redirect handler validating every hop against the official release allowlist."""

    def __init__(self, *, allow_localhost_http: bool = False) -> None:
        super().__init__()
        self._allow_localhost_http = allow_localhost_http

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[Request]:
        validate_release_url(newurl, allow_localhost_http=self._allow_localhost_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout_sec: float,
    allow_localhost_http: bool,
    accept: Optional[str] = None,
) -> bytes:
    """Download bounded bytes over verified TLS/allowlisted release URLs."""
    validated_url = validate_release_url(url, allow_localhost_http=allow_localhost_http)
    req_headers: dict[str, str] = {"User-Agent": f"mipmem/{__version__}"}
    if accept:
        req_headers["Accept"] = accept
    request = Request(validated_url, headers=req_headers, method="GET")

    opener = build_opener(
        HTTPSHandler(context=ssl.create_default_context()),
        _ValidatingRedirectHandler(allow_localhost_http=allow_localhost_http),
    )

    chunks: list[bytes] = []
    total_read = 0
    try:
        with opener.open(request, timeout=timeout_sec) as response:
            final_url = str(getattr(response, "geturl", lambda: validated_url)())
            validate_release_url(final_url, allow_localhost_http=allow_localhost_http)
            while True:
                block = response.read(65536)
                if not block:
                    break
                total_read += len(block)
                if total_read > max_bytes:
                    raise MipmemError(
                        UPDATE_VERIFICATION_FAILED,
                        f"Downloaded asset exceeds {max_bytes} bytes safety limit.",
                    )
                chunks.append(bytes(block))
    except MipmemError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Release download failed: {exc}",
        ) from exc

    return b"".join(chunks)


def _extract_release_assets(
    release_payload: Mapping[str, Any],
) -> tuple[str, Optional[str], Optional[str]]:
    """Extract normalized version, mipmem.pyz URL, and SHA256SUMS URL from release JSON."""
    raw_tag = str(release_payload.get("tag_name") or "").strip()
    if not raw_tag:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            "Release metadata is missing 'tag_name'.",
        )
    normalized_ver = raw_tag[1:] if raw_tag.lower().startswith("v") else raw_tag
    _parse_version_tuple(normalized_ver)

    pyz_url: Optional[str] = None
    sums_url: Optional[str] = None
    assets = release_payload.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, Mapping):
                continue
            name = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if not dl_url:
                continue
            if name == "mipmem.pyz":
                pyz_url = dl_url
            elif name == "SHA256SUMS":
                sums_url = dl_url
    return normalized_ver, pyz_url, sums_url


def _verify_temp_zipapp(
    tmp_path: Path,
    *,
    timeout_sec: float,
) -> bool:
    """Validate zip integrity via ZipFile.testzip() and smoke-check `health --json`."""
    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                raise MipmemError(
                    UPDATE_VERIFICATION_FAILED,
                    f"Corrupt archive entry in downloaded zipapp: {bad_member}.",
                )
    except MipmemError:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Downloaded file is not a valid zipapp archive: {exc}",
        ) from exc

    _best_effort_chmod(tmp_path, 0o755)
    isolated_env = {
        key: val
        for key, val in os.environ.items()
        if key not in ("PYTHONPATH", "MIPMEM_CONFIG_DIR", "MIPMEM_VAULT_DIR", "MIPMEM_CACHE_DIR")
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(tmp_path), "health", "--json"],
            cwd=str(tmp_path.parent),
            env=isolated_env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Pre-swap health verification failed to execute: {exc}",
        ) from exc

    if proc.returncode != 0:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Pre-swap health check exited with status {proc.returncode}.",
        )
    try:
        health_payload = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            f"Pre-swap health check produced invalid JSON: {exc}",
        ) from exc
    if not isinstance(health_payload, dict) or health_payload.get("success") is not True:
        raise MipmemError(
            UPDATE_VERIFICATION_FAILED,
            "Pre-swap health check did not report success=true.",
        )
    return True


def _log_update_event(
    logger: Optional[RotatingJsonLogger],
    *,
    level: str,
    started: float,
    install_mode: str,
    current_version: str,
    target_version: str,
    dry_run: bool,
    sha256_verified: bool,
    health_verified: bool,
    applied: bool,
    error_code: Optional[str],
) -> None:
    """Emit standardized CP-UPD-01 diagnostics record when logger is configured."""
    if logger is None:
        return
    duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
    logger.log_event(
        level,
        "updater",
        CP_UPD_01,
        error_code=error_code,
        duration_ms=duration_ms,
        details={
            "install_mode": install_mode,
            "current_version": current_version,
            "target_version": target_version,
            "dry_run": dry_run,
            "sha256_verified": sha256_verified,
            "health_verified": health_verified,
            "applied": applied,
            "error_code": error_code,
        },
    )


# @para-doc [#csa-updater-atomic-self-update]
def run_self_update(
    *,
    target_version: Optional[str] = None,
    check_only: bool = False,
    dry_run: bool = False,
    current_version: str = __version__,
    releases_api_url: Optional[str] = None,
    module_file: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
    allow_localhost_http: bool = False,
    logger: Optional[RotatingJsonLogger] = None,
) -> UpdateResult:
    """Opt-in verified zipapp self-updater adhering to allowlists, SHA256SUMS, and health."""
    started = time.perf_counter()
    mode, target_path = detect_install_mode(module_file=module_file, env=env)
    upgrade_cmd = recommended_upgrade_command(mode)
    resolved_ver = ""
    sha_ok = False
    health_ok = False
    applied = False

    try:
        if mode != "zipapp" and not check_only:
            raise MipmemError(
                UPDATE_VERIFICATION_FAILED,
                (
                    f"Self-update refuses to overwrite non-zipapp install ({mode!r}). "
                    f"Run: {upgrade_cmd}"
                ),
            )

        if releases_api_url:
            meta_url = releases_api_url
        elif target_version:
            cleaned_tag = target_version.strip()
            if not cleaned_tag.lower().startswith("v"):
                cleaned_tag = f"v{cleaned_tag}"
            meta_url = RELEASE_BY_TAG_API_TEMPLATE.format(tag=cleaned_tag)
        else:
            meta_url = DEFAULT_RELEASES_API_URL

        raw_meta = _download_bytes(
            meta_url,
            max_bytes=MAX_CHECKSUMS_BYTES * 4,
            timeout_sec=timeout_sec,
            allow_localhost_http=allow_localhost_http,
            accept="application/vnd.github+json",
        )
        try:
            release_data = json.loads(raw_meta.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MipmemError(
                UPDATE_VERIFICATION_FAILED,
                f"Invalid release JSON payload: {exc}",
            ) from exc
        if not isinstance(release_data, dict):
            raise MipmemError(UPDATE_VERIFICATION_FAILED, "Release metadata must be JSON object.")

        resolved_ver, pyz_url, sums_url = _extract_release_assets(release_data)
        update_available = compare_versions(current_version, resolved_ver) < 0

        if check_only:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            _log_update_event(
                logger,
                level="INFO",
                started=started,
                install_mode=mode,
                current_version=current_version,
                target_version=resolved_ver,
                dry_run=dry_run,
                sha256_verified=False,
                health_verified=False,
                applied=False,
                error_code=None,
            )
            return UpdateResult(
                install_mode=mode,
                target_path=str(target_path),
                current_version=current_version,
                target_version=resolved_ver,
                update_available=update_available,
                dry_run=dry_run,
                check_only=True,
                recommended_command=upgrade_cmd,
                message=(
                    f"Update available: {current_version} -> {resolved_ver}"
                    if update_available
                    else f"Already up to date ({current_version})."
                ),
                duration_ms=duration_ms,
            )

        if not update_available and not target_version:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            _log_update_event(
                logger,
                level="INFO",
                started=started,
                install_mode=mode,
                current_version=current_version,
                target_version=resolved_ver,
                dry_run=dry_run,
                sha256_verified=False,
                health_verified=False,
                applied=False,
                error_code=None,
            )
            return UpdateResult(
                install_mode=mode,
                target_path=str(target_path),
                current_version=current_version,
                target_version=resolved_ver,
                update_available=False,
                dry_run=dry_run,
                check_only=False,
                recommended_command=upgrade_cmd,
                message=f"Already up to date ({current_version}).",
                duration_ms=duration_ms,
            )

        if not pyz_url or not sums_url:
            raise MipmemError(
                UPDATE_VERIFICATION_FAILED,
                "Release is missing required 'mipmem.pyz' or 'SHA256SUMS' asset.",
            )

        sums_bytes = _download_bytes(
            sums_url,
            max_bytes=MAX_CHECKSUMS_BYTES,
            timeout_sec=timeout_sec,
            allow_localhost_http=allow_localhost_http,
        )
        checksums = parse_sha256sums(sums_bytes.decode("utf-8", errors="strict"))
        expected_digest = checksums.get("mipmem.pyz")
        if not expected_digest:
            raise MipmemError(
                UPDATE_VERIFICATION_FAILED,
                "SHA256SUMS manifest does not contain an entry for 'mipmem.pyz'.",
            )

        if dry_run:
            sha_ok = True
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            _log_update_event(
                logger,
                level="INFO",
                started=started,
                install_mode=mode,
                current_version=current_version,
                target_version=resolved_ver,
                dry_run=True,
                sha256_verified=True,
                health_verified=False,
                applied=False,
                error_code=None,
            )
            return UpdateResult(
                install_mode=mode,
                target_path=str(target_path),
                current_version=current_version,
                target_version=resolved_ver,
                update_available=update_available,
                dry_run=True,
                check_only=False,
                sha256_verified=True,
                health_verified=False,
                applied=False,
                recommended_command=upgrade_cmd,
                message=f"Dry-run: would update {target_path} to {resolved_ver}.",
                duration_ms=duration_ms,
            )

        pyz_bytes = _download_bytes(
            pyz_url,
            max_bytes=MAX_PYZ_BYTES,
            timeout_sec=timeout_sec,
            allow_localhost_http=allow_localhost_http,
        )
        actual_digest = hashlib.sha256(pyz_bytes).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise MipmemError(
                UPDATE_VERIFICATION_FAILED,
                "Downloaded zipapp SHA-256 digest does not match SHA256SUMS.",
            )
        sha_ok = True

        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target_path.parent),
            prefix=".tmp_mipmem_update_",
            suffix=".pyz",
        )
        tmp_path = Path(tmp_name)
        tmp_replaced = False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(pyz_bytes)
            health_ok = _verify_temp_zipapp(tmp_path, timeout_sec=timeout_sec)
            os.replace(tmp_path, target_path)
            tmp_replaced = True
            applied = True
        finally:
            if not tmp_replaced and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        _log_update_event(
            logger,
            level="INFO",
            started=started,
            install_mode=mode,
            current_version=current_version,
            target_version=resolved_ver,
            dry_run=False,
            sha256_verified=sha_ok,
            health_verified=health_ok,
            applied=applied,
            error_code=None,
        )
        return UpdateResult(
            install_mode=mode,
            target_path=str(target_path),
            current_version=current_version,
            target_version=resolved_ver,
            update_available=update_available,
            dry_run=False,
            check_only=False,
            sha256_verified=sha_ok,
            health_verified=health_ok,
            applied=applied,
            recommended_command=upgrade_cmd,
            message=f"Updated zipapp binary {target_path} to {resolved_ver}.",
            duration_ms=duration_ms,
        )
    except MipmemError as exc:
        _log_update_event(
            logger,
            level="ERROR",
            started=started,
            install_mode=mode,
            current_version=current_version,
            target_version=resolved_ver,
            dry_run=dry_run,
            sha256_verified=sha_ok,
            health_verified=health_ok,
            applied=applied,
            error_code=exc.code,
        )
        raise
