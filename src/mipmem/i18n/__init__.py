"""Bilingual locale lexicon registry and tokenization helpers for mipmem."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from mipmem.i18n.en import EN_BRAINSTORM_CUES, EN_INTENT_CUES, EN_STOP_WORDS
from mipmem.i18n.vi import (
    VI_BRAINSTORM_CUES,
    VI_INTENT_CUES,
    VI_STOP_WORDS,
    fold_vietnamese_d,
    normalize_vi_text,
)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
DEFAULT_BIGRAM_BOOST = 3.5


@dataclass(frozen=True)
class LocaleLexicon:
    """Language-specific lexical configuration for salience and FTS5 recall."""

    locale: str
    stop_words: frozenset[str]
    intent_cues: frozenset[str]
    brainstorm_cues: frozenset[str]
    bigram_boost: float = DEFAULT_BIGRAM_BOOST
    strip_diacritics: bool = False


_EN_LEXICON = LocaleLexicon(
    locale="en",
    stop_words=EN_STOP_WORDS,
    intent_cues=EN_INTENT_CUES,
    brainstorm_cues=EN_BRAINSTORM_CUES,
    bigram_boost=DEFAULT_BIGRAM_BOOST,
    strip_diacritics=False,
)

_VI_LEXICON = LocaleLexicon(
    locale="vi",
    stop_words=EN_STOP_WORDS | VI_STOP_WORDS,
    intent_cues=EN_INTENT_CUES | VI_INTENT_CUES,
    brainstorm_cues=EN_BRAINSTORM_CUES | VI_BRAINSTORM_CUES,
    bigram_boost=DEFAULT_BIGRAM_BOOST,
    strip_diacritics=True,
)

_LEXICON_REGISTRY: dict[str, LocaleLexicon] = {
    "en": _EN_LEXICON,
    "vi": _VI_LEXICON,
}


# @para-doc [#csa-i18n-lexicon-registry]
def get_lexicon(locale: str = "en") -> LocaleLexicon:
    """Return the configured LocaleLexicon for 'en' or 'vi', defaulting to 'en'."""
    key = locale.strip().lower() if isinstance(locale, str) else "en"
    return _LEXICON_REGISTRY.get(key, _EN_LEXICON)


def normalize_for_tokens(text: str, lexicon: LocaleLexicon) -> str:
    """Normalize raw text for ASCII-safe tokenization according to the active locale."""
    # Always fold đ/Đ and diacritics (mandatory for vi, safe for mixed EN/VI docs).
    _ = lexicon.strip_diacritics
    return normalize_vi_text(text)


def extract_anchor_tokens(text: str, lexicon: LocaleLexicon) -> list[str]:
    """Extract lowercase tokens excluding locale stop-words and 1-char noise."""
    normalized = normalize_for_tokens(text, lexicon)
    tokens = _TOKEN_RE.findall(normalized)
    return [tok for tok in tokens if len(tok) >= 2 and tok not in lexicon.stop_words]


def extract_bigrams(tokens: Sequence[str]) -> list[tuple[str, str]]:
    """Build ordered adjacent token pairs for compound-phrase FTS5 NEAR boosts."""
    if len(tokens) < 2:
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for idx in range(len(tokens) - 1):
        pair = (tokens[idx], tokens[idx + 1])
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def has_cue_match(text: str, cues: frozenset[str], lexicon: LocaleLexicon) -> bool:
    """Return True if any single-word or multi-word cue appears in normalized text."""
    normalized = normalize_for_tokens(text, lexicon)
    if not normalized:
        return False
    token_set = set(_TOKEN_RE.findall(normalized))
    padded = f" {normalized} "
    for cue in cues:
        if " " in cue:
            if f" {cue} " in padded:
                return True
        elif cue in token_set:
            return True
    return False


__all__ = [
    "DEFAULT_BIGRAM_BOOST",
    "LocaleLexicon",
    "extract_anchor_tokens",
    "extract_bigrams",
    "fold_vietnamese_d",
    "get_lexicon",
    "has_cue_match",
    "normalize_vi_text",
]
