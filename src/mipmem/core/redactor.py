"""Two-layer pre-write secret redactor and $HOME normalization for MipMem."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Optional

from mipmem.core.diagnostics import (
    CP_RED_01,
    RotatingJsonLogger,
    register_default_scrubber,
)

_PEM_PATTERN = re.compile(
    "-----BEGIN"
    + r" [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END"
    + r" [A-Z0-9 ]*PRIVATE KEY-----"
)
_URI_PASS_PATTERN = re.compile(
    r"(://[A-Za-z0-9_.-]+:)(?!\[REDACTED:)([^@\s/\"']+)(@)"
)

_TOKEN_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "GITHUB_FINE_PAT",
        re.compile("github" + r"_pat_[A-Za-z0-9_]{20,}"),
        "[REDACTED:GITHUB_FINE_PAT]",
    ),
    (
        "GITHUB_PAT",
        re.compile("ghp" + r"_[A-Za-z0-9]{20,}"),
        "[REDACTED:GITHUB_PAT]",
    ),
    (
        "GITHUB_OAUTH",
        re.compile(r"\bgh[ousr]" + r"_[A-Za-z0-9]{20,}"),
        "[REDACTED:GITHUB_OAUTH]",
    ),
    (
        "OPENAI_KEY",
        re.compile("sk" + r"-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        "[REDACTED:OPENAI_KEY]",
    ),
    (
        "GOOGLE_API_KEY",
        re.compile("AIza" + r"[0-9A-Za-z_-]{30,}"),
        "[REDACTED:GOOGLE_API_KEY]",
    ),
    (
        "AWS_ACCESS_KEY",
        re.compile("AKIA" + r"[0-9A-Z]{16}"),
        "[REDACTED:AWS_ACCESS_KEY]",
    ),
    (
        "JWT_TOKEN",
        re.compile(
            "eyJ"
            + r"[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
        "[REDACTED:JWT_TOKEN]",
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    """Result returned by redact_with_stats with match counts per secret family."""

    text: str
    redacted_counts: dict[str, int] = field(default_factory=dict)
    home_replacements: int = 0

    @property
    def total_secrets(self) -> int:
        """Return the total number of redacted secret matches."""
        return sum(self.redacted_counts.values())


def _safe_home_dir(home_dir: Optional[Path]) -> Optional[str]:
    """Return a normalized home path string suitable for boundary-safe replacement."""
    try:
        candidate = str(home_dir if home_dir is not None else Path.home())
    except RuntimeError:
        return None
    normalized = candidate.rstrip("/\\")
    if len(normalized) <= 1:
        return None
    return normalized


def redact_with_stats(text: str, home_dir: Optional[Path] = None) -> RedactionResult:
    """Apply pattern and path redaction and return redacted text with statistics."""
    if not text:
        return RedactionResult(text=text, redacted_counts={}, home_replacements=0)

    counts: dict[str, int] = {}
    working = text

    def _replace_pem(_match: re.Match[str]) -> str:
        counts["PEM_PRIVATE_KEY"] = counts.get("PEM_PRIVATE_KEY", 0) + 1
        return "[REDACTED:PEM_PRIVATE_KEY]"

    working = _PEM_PATTERN.sub(_replace_pem, working)

    def _replace_uri(match: re.Match[str]) -> str:
        counts["URI_PASSWORD"] = counts.get("URI_PASSWORD", 0) + 1
        return f"{match.group(1)}[REDACTED:URI_PASSWORD]{match.group(3)}"

    working = _URI_PASS_PATTERN.sub(_replace_uri, working)

    for label, pattern, token in _TOKEN_RULES:
        def _replace_token(_match: re.Match[str], key: str = label, repl: str = token) -> str:
            counts[key] = counts.get(key, 0) + 1
            return repl

        working = pattern.sub(_replace_token, working)

    home_replacements = 0
    home_str = _safe_home_dir(home_dir)
    if home_str is not None and home_str in working:
        boundary_re = re.compile(re.escape(home_str) + r"(?=$|[/\\:\s\"'`)\],;!?|])")

        def _replace_home(_match: re.Match[str]) -> str:
            nonlocal home_replacements
            home_replacements += 1
            return "~"

        working = boundary_re.sub(_replace_home, working)

    return RedactionResult(
        text=working,
        redacted_counts=counts,
        home_replacements=home_replacements,
    )


def scrub_without_logging(text: str, home_dir: Optional[Path] = None) -> str:
    """Scrub secrets and $HOME paths without emitting diagnostic log events."""
    return redact_with_stats(text, home_dir=home_dir).text


# @para-doc [#csa-redactor-secret-scrub]
def redact_text(
    text: str,
    home_dir: Optional[Path] = None,
    *,
    logger: Optional[RotatingJsonLogger] = None,
) -> str:
    """Redact secrets and normalize $HOME paths before any disk or log write."""
    started = time.perf_counter()
    result = redact_with_stats(text, home_dir=home_dir)
    if result.total_secrets > 0:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        active_logger = logger if logger is not None else RotatingJsonLogger()
        active_logger.log_event(
            "INFO",
            "redactor",
            CP_RED_01,
            duration_ms=elapsed_ms,
            details={
                "redacted_counts": result.redacted_counts,
                "home_replacements": result.home_replacements,
            },
        )
    return result.text


register_default_scrubber(scrub_without_logging)
