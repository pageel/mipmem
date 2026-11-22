"""Validator for the isolated Native Antigravity Plugin bundle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Optional, Union

from mipmem.core.diagnostics import (
    CP_HK_01,
    PLUGIN_MANIFEST_INVALID,
    MipmemError,
    RotatingJsonLogger,
)

ALLOWED_PLUGIN_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "displayName",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "suggestedPrompts",
        "logo",
        "disabled",
    }
)

REQUIRED_FLAT_HOOK_EVENTS: tuple[tuple[str, str], ...] = (
    ("SessionStart", "python3 ./bin/mipmem.pyz hook session-start"),
    ("PreInvocation", "python3 ./bin/mipmem.pyz hook pre-invocation"),
    ("Stop", "python3 ./bin/mipmem.pyz hook stop"),
)

_PERSONAL_PATH_RE = re.compile(r"/(?:home|Users|media)/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\")


@dataclass(frozen=True)
class PluginValidationReport:
    """Validation outcome for a Native Antigravity Plugin directory."""

    plugin_root: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    checked_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize validation report into a JSON-compatible dictionary."""
        return asdict(self)


def _load_json_object(path: Path, label: str, errors: list[str]) -> Optional[dict[str, Any]]:
    """Read a required JSON file and validate that its root is an object."""
    if not path.is_file() or path.is_symlink():
        errors.append(f"{label}: missing required file ({path.name}).")
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{label}: unable to read file ({exc}).")
        return None
    if _PERSONAL_PATH_RE.search(raw):
        errors.append(f"{label}: contains machine-specific personal user path.")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"{label}: invalid JSON ({exc}).")
        return None
    if not isinstance(decoded, dict):
        errors.append(f"{label}: root must be a JSON object.")
        return None
    return decoded


def _validate_plugin_json(
    root: Path,
    errors: list[str],
    checked: list[str],
    expected_version: Optional[str],
) -> None:
    """Validate plugin.json top-level fields against the official Antigravity schema."""
    path = root / "plugin.json"
    checked.append("plugin.json")
    data = _load_json_object(path, "plugin.json", errors)
    if data is None:
        return

    unknown = sorted(set(data.keys()) - ALLOWED_PLUGIN_FIELDS)
    if unknown:
        errors.append(
            f"plugin.json: unsupported top-level field(s): {', '.join(unknown)}."
        )
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("plugin.json: 'name' must be a non-empty string.")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        errors.append("plugin.json: 'version' must be a non-empty string.")
    elif expected_version is not None and version.strip() != expected_version.strip():
        errors.append(
            f"plugin.json: version {version!r} does not match expected {expected_version!r}."
        )


def _validate_hooks_json(root: Path, errors: list[str], checked: list[str]) -> None:
    """Validate hooks.json flat arrays and explicit `python3 ./bin/mipmem.pyz hook ...`."""
    path = root / "hooks.json"
    checked.append("hooks.json")
    data = _load_json_object(path, "hooks.json", errors)
    if data is None:
        return

    entry = data.get("mipmem")
    if not isinstance(entry, dict):
        errors.append("hooks.json: missing top-level 'mipmem' hook configuration object.")
        return

    for event_name, expected_cmd in REQUIRED_FLAT_HOOK_EVENTS:
        handlers = entry.get(event_name)
        if not isinstance(handlers, list) or not handlers:
            errors.append(f"hooks.json: '{event_name}' must be a non-empty flat array.")
            continue
        matched_cmd = False
        for idx, handler in enumerate(handlers):
            if not isinstance(handler, dict):
                errors.append(
                    f"hooks.json: '{event_name}[{idx}]' must be a command handler object."
                )
                continue
            if "matcher" in handler or "hooks" in handler:
                errors.append(
                    f"hooks.json: '{event_name}[{idx}]' must be a flat handler object "
                    "(do not wrap SessionStart/PreInvocation/Stop in matcher/hooks)."
                )
                continue
            cmd = str(handler.get("command") or "").strip()
            if not cmd.startswith("python3 ./bin/mipmem.pyz hook "):
                errors.append(
                    f"hooks.json: '{event_name}[{idx}].command' must start with "
                    "'python3 ./bin/mipmem.pyz hook ' (got {cmd!r}).".format(cmd=cmd)
                )
            if cmd == expected_cmd:
                matched_cmd = True
        if not matched_cmd:
            errors.append(
                f"hooks.json: '{event_name}' is missing canonical command {expected_cmd!r}."
            )


def _validate_mcp_config_json(root: Path, errors: list[str], checked: list[str]) -> None:
    """Validate mcp_config.json uses relative ['bin/mipmem.pyz', 'mcp'] under PLUGIN_ROOT."""
    path = root / "mcp_config.json"
    checked.append("mcp_config.json")
    data = _load_json_object(path, "mcp_config.json", errors)
    if data is None:
        return

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get("mipmem"), dict):
        errors.append("mcp_config.json: missing 'mcpServers.mipmem' stdio server entry.")
        return

    srv = servers["mipmem"]
    if srv.get("command") != "python3":
        errors.append("mcp_config.json: 'mcpServers.mipmem.command' must be 'python3'.")
    args = srv.get("args")
    if args != ["bin/mipmem.pyz", "mcp"]:
        errors.append(
            "mcp_config.json: 'mcpServers.mipmem.args' must be ['bin/mipmem.pyz', 'mcp'] "
            "(relative to plugin root, without ${PLUGIN_ROOT} interpolation)."
        )


def _validate_rules_and_skill(root: Path, errors: list[str], checked: list[str]) -> None:
    """Validate rules/AGENTS.md (no YAML frontmatter) and skills/mem/SKILL.md."""
    agents_path = root / "rules" / "AGENTS.md"
    checked.append("rules/AGENTS.md")
    if not agents_path.is_file() or agents_path.is_symlink():
        errors.append("rules/AGENTS.md: missing required plugin rules file.")
    else:
        agents_text = agents_path.read_text(encoding="utf-8")
        if _PERSONAL_PATH_RE.search(agents_text):
            errors.append("rules/AGENTS.md: contains machine-specific personal user path.")
        stripped = agents_text.lstrip("\ufeff").lstrip()
        if not stripped:
            errors.append("rules/AGENTS.md: file must not be empty.")
        if stripped.startswith("---"):
            errors.append(
                "rules/AGENTS.md: plugin rules must be plain Markdown without YAML frontmatter."
            )
        lower_agents = agents_text.lower()
        if "mem_bs_save" not in lower_agents or "confirm" not in lower_agents:
            errors.append(
                "rules/AGENTS.md: must instruct agent to ask user confirmation before "
                "saving artifacts via mem_bs_save."
            )

    skill_path = root / "skills" / "mem" / "SKILL.md"
    checked.append("skills/mem/SKILL.md")
    if not skill_path.is_file() or skill_path.is_symlink():
        errors.append("skills/mem/SKILL.md: missing required plugin skill file.")
    else:
        skill_text = skill_path.read_text(encoding="utf-8")
        if _PERSONAL_PATH_RE.search(skill_text):
            errors.append("skills/mem/SKILL.md: contains machine-specific personal user path.")
        stripped_skill = skill_text.lstrip("\ufeff")
        if not stripped_skill.startswith("---"):
            errors.append(
                "skills/mem/SKILL.md: must start with YAML frontmatter (name/description)."
            )
        lower_skill = skill_text.lower()
        if "mem_bs_save" not in lower_skill or "confirm" not in lower_skill:
            errors.append(
                "skills/mem/SKILL.md: must document opt-in user confirmation for mem_bs_save."
            )


# @para-doc [#csa-plugin-manifest-bundle]
def validate_plugin_bundle(
    plugin_root: Union[str, Path],
    *,
    expected_version: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> PluginValidationReport:
    """Validate all Native Antigravity Plugin manifest, hook, MCP, rule, and skill assets."""
    resolved = Path(plugin_root).expanduser().resolve()
    errors: list[str] = []
    checked: list[str] = []

    _validate_plugin_json(resolved, errors, checked, expected_version)
    _validate_hooks_json(resolved, errors, checked)
    _validate_mcp_config_json(resolved, errors, checked)
    _validate_rules_and_skill(resolved, errors, checked)

    valid = len(errors) == 0
    if logger is not None:
        logger.log_event(
            "INFO" if valid else "ERROR",
            "plugin_manifest",
            CP_HK_01,
            error_code=None if valid else PLUGIN_MANIFEST_INVALID,
            details={"valid": valid, "error_count": len(errors), "checked_files": checked},
        )
    return PluginValidationReport(
        plugin_root=str(resolved),
        valid=valid,
        errors=errors,
        checked_files=checked,
    )


def assert_valid_plugin_bundle(
    plugin_root: Union[str, Path],
    *,
    expected_version: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> PluginValidationReport:
    """Validate plugin bundle and raise MipmemError(PLUGIN_MANIFEST_INVALID) if invalid."""
    report = validate_plugin_bundle(
        plugin_root,
        expected_version=expected_version,
        logger=logger,
    )
    if not report.valid:
        raise MipmemError(
            PLUGIN_MANIFEST_INVALID,
            "Invalid plugin bundle: " + "; ".join(report.errors),
        )
    return report
