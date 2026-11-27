"""Dual-Channel OSS release readiness validator for pageel/mipmem."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
from typing import Any, Optional, Sequence
import zipfile

_INIT_VERSION_RE = re.compile(
    r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]',
    re.MULTILINE,
)
_SHA256_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?([^\s].*?)\s*$")
_SECTION_RE = re.compile(r"^\[([^\[\]]+)\]\s*$")
_HEADING_VERSION_RE = re.compile(r"^#{1,6}\s+.*`v(\d+\.\d+\.\d+)`")
_LAYOUT_TARGETS = ("plugin.json", "CHANGELOG.md", "bin/mipmem.pyz")
_LAYOUT_VERSION_RE = re.compile(r"(?:\(v|through\s+v)(\d+\.\d+\.\d+)\b")
_CHANGELOG_TOP_RE = re.compile(r"^##\s+\[(\d+\.\d+\.\d+)\]")


def _load_toml_fallback(text: str) -> dict[str, Any]:
    """Extract minimal pyproject.toml fields safely on Python < 3.11 without tomllib."""
    project_tbl: dict[str, Any] = {}
    urls_tbl: dict[str, Any] = {}
    section = ""

    # Join simple multiline brackets so lists/tables remain parseable without full TOML AST.
    lines: list[str] = []
    buf = ""
    depth = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if depth == 0:
                continue
        buf = f"{buf} {stripped}".strip() if buf else stripped
        depth += stripped.count("[") + stripped.count("{")
        depth -= stripped.count("]") + stripped.count("}")
        if depth <= 0:
            depth = 0
            lines.append(buf)
            buf = ""
    if buf:
        lines.append(buf)

    for line in lines:
        sec_match = _SECTION_RE.match(line)
        if sec_match:
            section = sec_match.group(1).strip()
            continue
        if "=" not in line:
            continue
        key, raw_val = (part.strip() for part in line.split("=", 1))
        if section == "project":
            if key == "version":
                project_tbl["version"] = raw_val.strip("\"' ")
            elif key == "dependencies":
                inner = raw_val.strip()
                if inner == "[]":
                    project_tbl["dependencies"] = []
                else:
                    items = [x.strip(" \"'") for x in inner.strip("[]").split(",") if x.strip()]
                    project_tbl["dependencies"] = items
            elif key == "authors":
                project_tbl["authors_raw"] = raw_val
        elif section == "project.urls":
            urls_tbl[key] = raw_val.strip("\"' ")

    return {"project": project_tbl, "urls": urls_tbl}


def _parse_pyproject(pyproject_path: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Parse pyproject.toml returning (project_table, urls_table, raw_text)."""
    raw_text = pyproject_path.read_text(encoding="utf-8")
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        parsed = _load_toml_fallback(raw_text)
        return parsed["project"], parsed["urls"], raw_text

    data = tomllib.loads(raw_text)
    proj = data.get("project", {}) if isinstance(data, dict) else {}
    if not isinstance(proj, dict):
        proj = {}
    urls = proj.get("urls", {})
    if not isinstance(urls, dict):
        urls = {}
    return proj, urls, raw_text


def _audit_pyproject(repo_root: Path, issues: list[str]) -> Optional[str]:
    """Validate version, dependencies=[], zero author PII, and [project.urls]."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        issues.append("pyproject.toml: file-missing")
        return None

    try:
        proj, urls, raw_text = _parse_pyproject(pyproject_path)
    except Exception as exc:
        issues.append(f"pyproject.toml: invalid-toml ({exc})")
        return None

    version = str(proj.get("version") or "").strip()
    if not version:
        issues.append("pyproject.toml: missing-version")

    deps = proj.get("dependencies")
    if isinstance(deps, list) and len(deps) > 0:
        issues.append(
            f"pyproject.toml: non-empty-dependencies (expected [], got {deps!r})"
        )

    authors = proj.get("authors")
    if isinstance(authors, list):
        for entry in authors:
            if isinstance(entry, dict):
                if "email" in entry or "@" in str(entry.get("name", "")):
                    issues.append("pyproject.toml: author-email-pii-disallowed")
                    break
    else:
        authors_raw = str(proj.get("authors_raw") or raw_text)
        for line in authors_raw.splitlines():
            if "authors" in line or "@" in line:
                if "authors" in line and ("email" in line or "@" in line):
                    issues.append("pyproject.toml: author-email-pii-disallowed")
                    break
            if "email" in str(proj.get("authors_raw") or "") or "@" in str(
                proj.get("authors_raw") or ""
            ):
                issues.append("pyproject.toml: author-email-pii-disallowed")
                break

    if not urls or "Homepage" not in urls or "Repository" not in urls:
        issues.append("pyproject.toml: missing-project-urls (require Homepage & Repository)")

    return version or None


def _audit_versions(repo_root: Path, pyproject_version: Optional[str], issues: list[str]) -> None:
    """Verify exact version parity across pyproject.toml, __init__.py, and plugin.json."""
    init_path = repo_root / "src" / "mipmem" / "__init__.py"
    init_version: Optional[str] = None
    if not init_path.is_file():
        issues.append("src/mipmem/__init__.py: file-missing")
    else:
        init_match = _INIT_VERSION_RE.search(init_path.read_text(encoding="utf-8"))
        if not init_match:
            issues.append("src/mipmem/__init__.py: missing-__version__")
        else:
            init_version = init_match.group(1).strip()

    plugin_path = repo_root / "plugin.json"
    plugin_version: Optional[str] = None
    if not plugin_path.is_file():
        issues.append("plugin.json: file-missing")
    else:
        try:
            plugin_data = json.loads(plugin_path.read_text(encoding="utf-8"))
            plugin_version = str(plugin_data.get("version") or "").strip() or None
            if not plugin_version:
                issues.append("plugin.json: missing-version")
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"plugin.json: invalid-json ({exc})")

    if pyproject_version and init_version and pyproject_version != init_version:
        issues.append(
            f"version-mismatch: pyproject.toml={pyproject_version} vs __init__.py={init_version}"
        )
    if pyproject_version and plugin_version and pyproject_version != plugin_version:
        issues.append(
            f"version-mismatch: pyproject.toml={pyproject_version} vs plugin.json={plugin_version}"
        )
    if init_version and plugin_version and init_version != plugin_version:
        issues.append(
            f"version-mismatch: __init__.py={init_version} vs plugin.json={plugin_version}"
        )


def _audit_workflows(repo_root: Path, issues: list[str]) -> None:
    """Audit ci.yml and release.yml for sync/*.py, macos-latest, and OIDC + PEP 740."""
    ci_path = repo_root / ".github" / "workflows" / "ci.yml"
    if not ci_path.is_file():
        issues.append(".github/workflows/ci.yml: file-missing")
    else:
        ci_text = ci_path.read_text(encoding="utf-8")
        if "src/mipmem/sync/*.py" not in ci_text:
            issues.append(".github/workflows/ci.yml: ci-missing-sync-coverage")
        if "macos-latest" not in ci_text:
            issues.append(".github/workflows/ci.yml: ci-missing-macos-smoke")

    rel_path = repo_root / ".github" / "workflows" / "release.yml"
    if not rel_path.is_file():
        issues.append(".github/workflows/release.yml: file-missing")
    else:
        rel_text = rel_path.read_text(encoding="utf-8")
        if "PYPI_API_TOKEN" in rel_text:
            issues.append(".github/workflows/release.yml: static-pypi-token-forbidden")
        if "id-token: write" not in rel_text or "pypa/gh-action-pypi-publish" not in rel_text:
            issues.append(".github/workflows/release.yml: missing-pypi-oidc-trusted-publisher")
        if "attestations: true" not in rel_text:
            issues.append(".github/workflows/release.yml: missing-pep740-attestation")
        if "SHA256SUMS" not in rel_text:
            issues.append(".github/workflows/release.yml: missing-SHA256SUMS-step")


def _audit_dist_dir(dist_dir: Path, issues: list[str]) -> None:
    """Verify SHA256SUMS parity and archive integrity inside dist_dir."""
    dist_root = dist_dir.resolve()
    if not dist_root.is_dir():
        issues.append(f"{dist_dir}: dist-directory-missing")
        return

    sums_path = dist_root / "SHA256SUMS"
    if not sums_path.is_file():
        issues.append("dist/SHA256SUMS: file-missing")
        return

    listed_files: set[str] = set()
    for line_no, raw_line in enumerate(sums_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SHA256_LINE_RE.match(stripped)
        if not match:
            issues.append(f"dist/SHA256SUMS:{line_no}: malformed-checksum-line")
            continue
        expected_digest = match.group(1).lower()
        target_name = Path(match.group(2).strip()).name
        listed_files.add(target_name)
        candidate = dist_root / target_name
        if not candidate.is_file():
            issues.append(f"dist/{target_name}: referenced-asset-missing")
            continue
        actual_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            issues.append(f"dist/{target_name}: checksum-mismatch")

    pyz_path = dist_root / "mipmem.pyz"
    if pyz_path.is_file():
        if "mipmem.pyz" not in listed_files:
            issues.append("dist/SHA256SUMS: missing-mipmem.pyz-entry")
        try:
            with zipfile.ZipFile(pyz_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    issues.append(f"dist/mipmem.pyz:{bad}: corrupt-zipapp-member")
        except zipfile.BadZipFile:
            issues.append("dist/mipmem.pyz: invalid-zipapp-archive")


def _audit_docs_versions(
    repo_root: Path,
    expected_version: Optional[str],
    issues: list[str],
) -> None:
    """Verify README headings, Repository Layout manifests, and top CHANGELOG match expected."""
    if not expected_version:
        return

    for rel_name in ("README.md", "README.vi.md"):
        doc_path = repo_root / rel_name
        if not doc_path.is_file():
            continue
        for line_no, line in enumerate(doc_path.read_text(encoding="utf-8").splitlines(), start=1):
            heading_match = _HEADING_VERSION_RE.match(line)
            if heading_match:
                found = heading_match.group(1)
                if found != expected_version:
                    issues.append(
                        f"{rel_name}:{line_no}: stale-version-reference "
                        f"(found v{found}, expected v{expected_version})"
                    )
            if any(target in line for target in _LAYOUT_TARGETS):
                for layout_match in _LAYOUT_VERSION_RE.finditer(line):
                    found = layout_match.group(1)
                    if found != expected_version:
                        issues.append(
                            f"{rel_name}:{line_no}: stale-version-reference "
                            f"(found v{found}, expected v{expected_version})"
                        )

    changelog_path = repo_root / "CHANGELOG.md"
    if changelog_path.is_file():
        for line_no, line in enumerate(
            changelog_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            top_match = _CHANGELOG_TOP_RE.match(line)
            if top_match:
                found = top_match.group(1)
                if found != expected_version:
                    issues.append(
                        f"CHANGELOG.md:{line_no}: changelog-version-mismatch "
                        f"(found {found}, expected {expected_version})"
                    )
                break


# @para-doc [#csa-release-readiness-validator]
def validate_release_readiness(
    repo_root: Path,
    dist_dir: Optional[Path] = None,
) -> list[str]:
    """Verify v1.0.0 Dual-Channel release parity, OIDC workflow config, and checksums."""
    root = repo_root.resolve()
    if not root.exists():
        return [f"{repo_root}: root-directory-missing"]

    issues: list[str] = []
    pyproject_version = _audit_pyproject(root, issues)
    _audit_versions(root, pyproject_version, issues)
    _audit_docs_versions(root, pyproject_version, issues)
    _audit_workflows(root, issues)
    if dist_dir is not None:
        _audit_dist_dir(dist_dir, issues)
    return issues


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint returning zero when repository passes all release readiness gates."""
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        prog="verify_release",
        description="Validate Dual-Channel release readiness for pageel/mipmem.",
    )
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=str(default_root),
        help="Path to the mipmem repository root.",
    )
    parser.add_argument(
        "--dist-dir",
        default=None,
        help="Optional release artifacts directory containing SHA256SUMS.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    dist_arg = Path(args.dist_dir) if args.dist_dir else None
    issues = validate_release_readiness(Path(args.repo_root), dist_dir=dist_arg)
    if issues:
        sys.stderr.write(
            f"RELEASE_READINESS_FAILED: {len(issues)} issue(s) detected:\n"
        )
        for item in issues:
            sys.stderr.write(f"  - {item}\n")
        return 1
    sys.stdout.write(f"RELEASE_READINESS_OK: 0 issues in {Path(args.repo_root).name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
