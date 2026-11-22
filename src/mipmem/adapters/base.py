"""Normalized transcript conversation contract and surface detector."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, Union, runtime_checkable

from mipmem.core.diagnostics import RotatingJsonLogger
from mipmem.core.sessions import SessionRecord, normalize_surface


@dataclass(frozen=True)
class NormalizedConversation:
    """Agent-agnostic conversation snapshot extracted from CLI or IDE transcripts."""

    conversation_id: str
    surface: str = "generic"
    source_kind: str = "unknown"
    title: str = ""
    step_count: int = 0
    last_modified_time: str = ""
    workspace_paths: list[str] = field(default_factory=list)
    recent_prompts: list[str] = field(default_factory=list)
    last_agent_response: str = ""
    edited_files: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    brainstorm_refs: list[str] = field(default_factory=list)
    is_subagent: bool = False
    expanded_truncated_steps: int = 0
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize the normalized conversation into a JSON-compatible dictionary."""
        return asdict(self)

    def to_session_record(self, host_id: str) -> SessionRecord:
        """Convert this conversation into a canonical SessionRecord for vault writing."""
        return SessionRecord(
            conversation_id=self.conversation_id,
            host_id=host_id,
            surface=normalize_surface(self.surface),
            last_modified_time=self.last_modified_time,
            step_count=max(0, int(self.step_count)),
            title=self.title,
            edited_files=list(self.edited_files),
            touched_files=list(self.touched_files),
            recent_prompts=list(self.recent_prompts),
            last_agent_response=self.last_agent_response,
            brainstorm_refs=list(self.brainstorm_refs),
            body=self.body,
        )


@runtime_checkable
class TranscriptAdapter(Protocol):
    """Protocol implemented by surface-specific transcript harvesters."""

    surface: str

    def extract(
        self,
        *,
        conversation_id: str,
        transcript_path: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        logger: Optional[RotatingJsonLogger] = None,
    ) -> Optional[NormalizedConversation]:
        """Extract a normalized conversation from local transcript sources."""
        ...


# @para-doc [#csa-adapter-normalized-schema]
def detect_surface_from_path(transcript_path: Optional[Union[str, Path]]) -> str:
    """Detect canonical agent surface ('cli', 'ide', or 'generic') from transcript path."""
    if transcript_path is None:
        return "generic"
    raw = str(transcript_path).strip()
    if not raw:
        return "generic"

    normalized = raw.replace("\\", "/")
    segments = [part.strip().lower() for part in normalized.split("/") if part.strip()]
    for segment in segments:
        if segment == "antigravity-cli":
            return "cli"
    for segment in segments:
        if segment in ("antigravity", "antigravity-ide"):
            return "ide"
    return "generic"
