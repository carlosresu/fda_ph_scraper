"""Lightweight text normalization helpers used by the FDA PH scrapers."""

from __future__ import annotations

import re
import unicodedata

_GM_TOKEN_RX = re.compile(r"(?<![a-z])gms?(?![a-z])")


def _normalize_unit_tokens(text: str) -> str:
    """Restrict gm/gms collapsing to standalone unit tokens."""
    return _GM_TOKEN_RX.sub("g", text)


def normalize_text(value: str) -> str:
    """Canonical lowercased text used during form/route extraction."""
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"\biv\b", "intravenous", normalized)
    normalized = re.sub(r"[^\w%/+\.\- ]+", " ", normalized)
    normalized = normalized.replace("microgram", "mcg").replace("μg", "mcg").replace("µg", "mcg")
    normalized = re.sub(r"(?<![a-z])cc(?![a-z])", "ml", normalized)
    normalized = normalized.replace("milli litre", "ml").replace("milliliter", "ml")
    normalized = _normalize_unit_tokens(normalized)
    normalized = normalized.replace("milligram", "mg")
    normalized = normalized.replace("polymixin", "polymyxin")
    normalized = normalized.replace("hydrochlorde", "hydrochloride")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
