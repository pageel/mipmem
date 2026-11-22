"""Agent-surface transcript, hook, slash-command, and plugin adapters for MipMem."""

from __future__ import annotations

from mipmem.adapters.agy_cli import AntigravityCliAdapter, extract_cli_conversation
from mipmem.adapters.agy_ide import AntigravityIdeAdapter, extract_ide_conversation
from mipmem.adapters.base import (
    NormalizedConversation,
    TranscriptAdapter,
    detect_surface_from_path,
)

__all__ = [
    "AntigravityCliAdapter",
    "AntigravityIdeAdapter",
    "NormalizedConversation",
    "TranscriptAdapter",
    "detect_surface_from_path",
    "extract_cli_conversation",
    "extract_ide_conversation",
]
