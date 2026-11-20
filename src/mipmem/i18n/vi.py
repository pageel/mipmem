"""Vietnamese d-stroke folding, diacritic normalization, and lexicon cues."""

from __future__ import annotations

import re
import unicodedata

_VI_D_TRANSLATION = str.maketrans({"đ": "d", "Đ": "D"})
_NON_ALNUM_RE = re.compile(r"[^a-z0-9_]+")

VI_STOP_WORDS: frozenset[str] = frozenset(
    {
        "ai",
        "anh",
        "ba",
        "ban",
        "bi",
        "cac",
        "cai",
        "can",
        "chi",
        "cho",
        "chua",
        "co",
        "con",
        "cua",
        "cung",
        "dang",
        "day",
        "de",
        "den",
        "di",
        "duoc",
        "em",
        "gi",
        "hay",
        "het",
        "khi",
        "khong",
        "la",
        "lai",
        "lam",
        "len",
        "luc",
        "ma",
        "minh",
        "moi",
        "mot",
        "muon",
        "nao",
        "nay",
        "nen",
        "neu",
        "ngay",
        "nguoi",
        "nha",
        "nhe",
        "nhieu",
        "nhu",
        "nhung",
        "noi",
        "o",
        "phai",
        "qua",
        "ra",
        "rat",
        "roi",
        "sau",
        "se",
        "tai",
        "theo",
        "thi",
        "toi",
        "tren",
        "trong",
        "truoc",
        "tu",
        "tuy",
        "va",
        "van",
        "vao",
        "ve",
        "vi",
        "viec",
        "voi",
        "vua",
        "xem",
        "xuong",
    }
)

VI_INTENT_CUES: frozenset[str] = frozenset(
    {
        "chot",
        "dac ta",
        "dinh huong",
        "hom qua",
        "kien truc",
        "lich su",
        "lo trinh",
        "nho",
        "phien truoc",
        "phuong an",
        "quyet dinh",
        "rui ro",
        "thiet ke",
        "truy hoi",
    }
)

VI_BRAINSTORM_CUES: frozenset[str] = frozenset(
    {
        "brainstorm",
        "chien luoc",
        "de xuat",
        "kien truc",
        "phuong an",
        "quyet dinh",
        "thao luan",
    }
)


# @para-doc [#csa-i18n-vi-normalizer]
def fold_vietnamese_d(text: str) -> str:
    """Fold Vietnamese d-stroke (U+0111/U+0110) to ASCII d/D before SQLite FTS5 use."""
    return unicodedata.normalize("NFC", text).translate(_VI_D_TRANSLATION)


def normalize_vi_text(text: str) -> str:
    """Strip Vietnamese diacritics and punctuation into space-separated ASCII tokens."""
    folded = fold_vietnamese_d(text)
    decomposed = unicodedata.normalize("NFD", folded)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    collapsed = _NON_ALNUM_RE.sub(" ", stripped.lower()).strip()
    return collapsed
