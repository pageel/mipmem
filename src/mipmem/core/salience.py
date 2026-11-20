"""Dual-Anchor session salience scorer (PRIMARY_WORK vs UTILITY_MICRO)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from mipmem.core.sessions import SessionRecord
from mipmem.i18n import get_lexicon, has_cue_match

TIER_PRIMARY_WORK = "PRIMARY_WORK"
TIER_UTILITY_MICRO = "UTILITY_MICRO"

BRAINSTORM_BONUS = 25.0
PRIMARY_WORK_THRESHOLD = 40.0

_EDITED_FILE_WEIGHT = 12.0
_EDITED_FILE_CAP = 48.0
_TOUCHED_FILE_WEIGHT = 3.0
_TOUCHED_FILE_CAP = 15.0
_STEP_WEIGHT_SUBSTANTIVE = 2.5
_STEP_CAP = 30.0
_SUBSTANTIVE_BODY_WORDS = 25
_SUBSTANTIVE_BODY_BONUS = 8.0
_INTENT_CUE_BONUS = 15.0
_MIN_BRAINSTORM_WORDS = 20


@dataclass(frozen=True)
class SalienceResult:
    """Computed salience score, tier classification, and explainable reasons."""

    score: float
    tier: str
    brainstorm_bonus_applied: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize salience result into a JSON-compatible dictionary."""
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def _build_session_text(record: SessionRecord) -> str:
    """Concatenate searchable textual fields of a session for cue detection."""
    parts: list[str] = [record.title]
    if record.recent_prompts:
        parts.extend(record.recent_prompts)
    if record.last_agent_response:
        parts.append(record.last_agent_response)
    if record.body:
        parts.append(record.body)
    return "\n".join(part for part in parts if part).strip()


# @para-doc [#csa-salience-dual-anchor]
def score_session_salience(
    record: SessionRecord,
    *,
    locale: str = "en",
) -> SalienceResult:
    """Score a session with Dual-Anchor structural + lexical cues and Brainstorm Bonus."""
    lexicon = get_lexicon(locale)
    score = 0.0
    reasons: list[str] = []

    edited_set = {path.strip() for path in record.edited_files if path.strip()}
    touched_only = {
        path.strip()
        for path in record.touched_files
        if path.strip() and path.strip() not in edited_set
    }

    if edited_set:
        edited_pts = min(_EDITED_FILE_CAP, len(edited_set) * _EDITED_FILE_WEIGHT)
        score += edited_pts
        reasons.append(f"edited_files:{len(edited_set)}(+{edited_pts:.1f})")

    if touched_only:
        touched_pts = min(_TOUCHED_FILE_CAP, len(touched_only) * _TOUCHED_FILE_WEIGHT)
        score += touched_pts
        reasons.append(f"touched_files:{len(touched_only)}(+{touched_pts:.1f})")

    steps = max(0, int(record.step_count))
    if steps >= 3:
        step_pts = min(_STEP_CAP, steps * _STEP_WEIGHT_SUBSTANTIVE)
    else:
        step_pts = float(steps)
    if step_pts > 0.0:
        score += step_pts
        reasons.append(f"step_count:{steps}(+{step_pts:.1f})")

    combined_text = _build_session_text(record)
    word_count = len(combined_text.split())
    if word_count >= _SUBSTANTIVE_BODY_WORDS:
        score += _SUBSTANTIVE_BODY_BONUS
        reasons.append(f"substantive_body:{word_count}w(+{_SUBSTANTIVE_BODY_BONUS:.1f})")

    if combined_text and has_cue_match(combined_text, lexicon.intent_cues, lexicon):
        score += _INTENT_CUE_BONUS
        reasons.append(f"intent_cue(+{_INTENT_CUE_BONUS:.1f})")

    has_bs_signal = bool(record.brainstorm_refs) or (
        bool(combined_text) and has_cue_match(combined_text, lexicon.brainstorm_cues, lexicon)
    )
    is_substantive_for_bonus = (
        bool(record.brainstorm_refs)
        or steps >= 3
        or len(edited_set) >= 1
        or word_count >= _MIN_BRAINSTORM_WORDS
    )

    brainstorm_bonus_applied = False
    if has_bs_signal and is_substantive_for_bonus:
        score += BRAINSTORM_BONUS
        brainstorm_bonus_applied = True
        reasons.append(f"brainstorm_bonus(+{BRAINSTORM_BONUS:.1f})")

    rounded_score = round(score, 2)
    tier = TIER_PRIMARY_WORK if rounded_score >= PRIMARY_WORK_THRESHOLD else TIER_UTILITY_MICRO
    return SalienceResult(
        score=rounded_score,
        tier=tier,
        brainstorm_bonus_applied=brainstorm_bonus_applied,
        reasons=tuple(reasons),
    )
