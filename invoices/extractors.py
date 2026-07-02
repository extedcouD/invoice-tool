"""Field extractors: a default baseline + pluggable **per-vendor** overrides.

Real vendors use wildly different invoice layouts ("Bill No" vs "Invoice No",
"Amount Payable" vs "Total Invoice Value", lakh-grouped numbers, ...). The
`default_extract` baseline handles the common templated case; when it can't, a
per-vendor extractor — matched by GSTIN, vendor name, or a content signature —
fills or overrides specific fields.

Add one without touching the pipeline:

    @register_vendor("acme", match=gstin_prefix("29AACCB"))
    def acme(text, doc):
        m = re.search(r"Ref#\\s*(\\S+)", text)
        if m: doc.fields.invoice_no_content = m.group(1)

Matching runs AFTER the baseline (so matchers can key off baseline-parsed
fields); multiple matches apply in ascending `priority` so the highest wins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from dateutil import parser as dateparser

from .config import (
    RE_BILLTO_MARKER, RE_DATE_CONTENT, RE_GSTIN, RE_INVOICE_NO_CONTENT,
    RE_TAXABLE, RE_TOTAL,
)
from .core.models import Document


# --------------------------------------------------------------------------- #
# shared normalizers
# --------------------------------------------------------------------------- #
def money(raw: str | None) -> float | None:
    if not raw:
        return None
    # tolerate Indian lakh grouping (1,23,456.78) and plain commas alike
    try:
        return round(float(raw.replace(",", "").strip()), 2)
    except ValueError:
        return None


def norm_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return dateparser.parse(raw, dayfirst=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def vendor_name(text: str) -> str | None:
    """Seller name = the line after 'TAX INVOICE', else the first real line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.search(r"tax\s+invoice", ln, re.I):
            if i + 1 < len(lines):
                return lines[i + 1]
            break
    return lines[0] if lines else None


# --------------------------------------------------------------------------- #
# default baseline extractor (the templated "TAX INVOICE" layout)
# --------------------------------------------------------------------------- #
def default_extract(text: str, doc: Document) -> None:
    f = doc.fields

    m = RE_INVOICE_NO_CONTENT.search(text)
    if m:
        f.invoice_no_content = m.group(1).strip()

    f.vendor_name_pdf = vendor_name(text)

    billto = RE_BILLTO_MARKER.search(text)
    split = billto.start() if billto else len(text)
    gstins = list(RE_GSTIN.finditer(text))
    seller = next((g.group(1) for g in gstins if g.start() < split), None)
    buyer = next((g.group(1) for g in gstins if g.start() >= split), None)
    f.vendor_gstin = seller or (gstins[0].group(1) if gstins else None)
    f.bill_to_gstin = buyer

    if billto:
        tail = re.sub(r"^[\s:]+", "", text[billto.end():])
        for ln in tail.splitlines():
            if re.search(r"[A-Za-z0-9]", ln):
                f.bill_to_name = ln.strip()
                break

    dm = RE_DATE_CONTENT.search(text)
    if dm:
        f.invoice_date_raw = dm.group(1)
        f.invoice_date = norm_date(dm.group(1))

    tx = RE_TAXABLE.search(text)
    f.taxable_value = money(tx.group(1)) if tx else None
    tot = RE_TOTAL.search(text)
    f.total_value = money(tot.group(1)) if tot else None


# --------------------------------------------------------------------------- #
# per-vendor extractor registry
# --------------------------------------------------------------------------- #
Matcher = Callable[[Document], bool]
Apply = Callable[[str, Document], None]


@dataclass
class VendorExtractor:
    name: str
    match: Matcher
    apply: Apply
    priority: int = 0


REGISTRY: list[VendorExtractor] = []


def register_vendor(name: str, match: Matcher, priority: int = 0):
    """Decorator registering a per-vendor extractor."""
    def deco(fn: Apply) -> Apply:
        REGISTRY.append(VendorExtractor(name, match, fn, priority))
        return fn
    return deco


def matching(doc: Document) -> list[VendorExtractor]:
    return sorted([v for v in REGISTRY if _safe_match(v, doc)],
                  key=lambda v: v.priority)


def _safe_match(v: VendorExtractor, doc: Document) -> bool:
    try:
        return bool(v.match(doc))
    except Exception:
        return False


# ---- matcher helpers ------------------------------------------------------ #
def gstin_prefix(prefix: str) -> Matcher:
    p = prefix.upper()
    return lambda d: bool(d.fields.vendor_gstin and d.fields.vendor_gstin.upper().startswith(p))


def vendor_name_contains(sub: str) -> Matcher:
    s = sub.lower()
    return lambda d: bool(d.fields.vendor_name_pdf and s in d.fields.vendor_name_pdf.lower())


def text_matches(pattern: str) -> Matcher:
    rx = re.compile(pattern, re.I)
    return lambda d: bool(rx.search(d.text or ""))


# --------------------------------------------------------------------------- #
# built-in example: a vendor whose invoices use a non-standard layout the
# baseline can't fully parse ("Bill No", "Dated", "Amount Payable", lakh commas).
# Demonstrates recovering an otherwise-flagged invoice. See scripts/introduce_dirt.py.
# --------------------------------------------------------------------------- #
@register_vendor("zephyr-logistics",
                 match=text_matches(r"zephyr\s+logistics"), priority=10)
def _zephyr(text: str, doc: Document) -> None:
    f = doc.fields
    m = re.search(r"bill\s*no\.?\s*:?\s*([A-Za-z0-9][A-Za-z0-9/\-]+)", text, re.I)
    if m:
        f.invoice_no_content = m.group(1).strip()
    dm = re.search(r"\bdated\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", text, re.I)
    if dm:
        f.invoice_date_raw = dm.group(1)
        f.invoice_date = norm_date(dm.group(1))
    am = re.search(r"amount\s+payable\s*:?\s*(?:inr|rs\.?|₹)?\s*([\d,]+\.\d{2})", text, re.I)
    if am:
        f.total_value = money(am.group(1))
    tv = re.search(r"taxable\s*:?\s*(?:inr|rs\.?|₹)?\s*([\d,]+\.\d{2})", text, re.I)
    if tv:
        f.taxable_value = money(tv.group(1))
