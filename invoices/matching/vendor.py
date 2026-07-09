"""Fuzzy company <-> vendor matching.

Pipeline: normalize both names (strip legal suffixes, punctuation, case, '&'->
'and'), then score with rapidfuzz token_set_ratio (order-independent, tolerant
of extra tokens). A shared GSTIN, when known, overrides the score entirely.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..config import LEGAL_SUFFIXES


def normalize(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"\(.*?\)", " ", s)          # drop parentheticals
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)       # strip punctuation
    s = re.sub(r"\s+", " ", s).strip()
    # strip trailing legal suffixes (longest first so 'pvt ltd' beats 'ltd')
    for suf in sorted(LEGAL_SUFFIXES, key=len, reverse=True):
        s = re.sub(rf"\b{re.escape(suf)}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score(a: str | None, b: str | None) -> float:
    """0..100 similarity of two company names after normalization."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return float(fuzz.token_set_ratio(na, nb))


def norm_id(s: str | None) -> str:
    """Normalize an invoice number/id for cross-format equality.

    Strips everything but alphanumerics and lowercases, so slash- and dash-style
    ids collapse to the same key: 'APEX/22-23/003' and 'APEX-22-23-003' -> 'apex2223003'.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower()) if s else ""


def norm_gstin(s: str | None) -> str:
    """Normalize a GSTIN for equality (uppercase, alphanumerics only)."""
    return re.sub(r"[^A-Z0-9]", "", s.upper()) if s else ""
