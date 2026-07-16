"""Central, config-driven settings. No magic values buried in stage logic.

Tune thresholds, keywords, regexes, and the output column order here without
touching pipeline code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Folder scraping
# --------------------------------------------------------------------------- #
# We only walk the Kotak bank subtree for now (per scope decision).
BANK_SCOPE = "Kotak"

# Files we never treat as candidate invoices.
IGNORE_FILENAMES = {".DS_Store", "Thumbs.db"}
IGNORE_SUFFIXES = {".xlsx", ".xls", ".csv", ".doc", ".docx", ".txt"}
CANDIDATE_SUFFIXES = {".pdf"}

# Expected path grammar under a financial-year root:
#   FY XX-YY / Payments / <Bank> / <Mon-YYYY> / <DD-Mon-YYYY> / <Company> / <file>.pdf
# We parse positionally but tolerantly (see stages/walk.py); these regexes label
# a path component when it matches, rather than asserting a fixed depth.
RE_FY = re.compile(r"^FY\s*\d{2}\s*-\s*\d{2}$", re.I)
RE_MONTH = re.compile(r"^[A-Za-z]{3,9}-\d{4}$")           # Sep-2022
RE_DATE = re.compile(r"^\d{1,2}-[A-Za-z]{3,9}-\d{4}$")     # 24-Sep-2022


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
# A page yielding fewer than this many stripped chars is treated as image-only
# and routed to OCR.
MIN_TEXT_CHARS = 20
OCR_DPI = 300
OCR_LANG = "eng"
# Tesseract engine/segmentation flags. --oem 1 forces the LSTM engine (more
# accurate than the legacy engine the default OEM can fall back to); --psm 6
# assumes a single uniform block of text, which fits dense invoices better than
# the default PSM 3 full-page layout analysis (and does *less* layout work, so
# it is no slower). preserve_interword_spaces keeps column gaps that regexes lean on.
OCR_CONFIG = "--oem 1 --psm 6 -c preserve_interword_spaces=1"

# --- Camera-shot (photo) escalation ---------------------------------------- #
# Phone photos of invoices need different preprocessing than flatbed scans
# (uneven lighting, rotation), and that preprocessing is expensive. So it is an
# *escalation*: the cheap OCR pass runs on every image page as before, and this
# heavier pass only fires when that pass comes back below the confidence floor.
# A clean-scan corpus therefore pays nothing for it. Set False to disable on
# huge runs where the extra per-bad-page cost is not worth it.
OCR_PHOTO_ENABLED = True
# Mean per-word Tesseract confidence (0-100) below which a page is retried
# through the photo pipeline. Also the floor below which a page is flagged
# `low_ocr_confidence` for review. Raise to escalate more, lower to escalate less.
OCR_PHOTO_MIN_CONFIDENCE = 55.0
# Cap the long edge (px) fed to Tesseract on the photo pass. Phone shots are
# often 3-4k px and Tesseract time is superlinear in pixel count; downscaling to
# this stays legible while bounding per-page cost.
OCR_PHOTO_MAX_DIM = 2200
# Radius (px) of the background estimate used to flatten uneven lighting before
# thresholding. Roughly the stroke-to-stroke spacing at OCR resolution.
OCR_PHOTO_BG_RADIUS = 25


# --------------------------------------------------------------------------- #
# Classification (invoice vs not)
# --------------------------------------------------------------------------- #
# Positive/negative content signals. Scored, not hard-coded booleans, so the
# threshold is tunable and reasons are explainable.
INVOICE_SIGNALS = [
    (re.compile(r"\btax\s+invoice\b", re.I), 3.0, "has 'TAX INVOICE'"),
    (re.compile(r"\binvoice\s*no\.?\b", re.I), 1.5, "has 'Invoice No'"),
    (re.compile(r"\bgstin\b", re.I), 1.0, "has GSTIN"),
    (re.compile(r"total\s+invoice\s+value", re.I), 1.5, "has 'Total Invoice Value'"),
    (re.compile(r"place\s+of\s+supply", re.I), 0.5, "has 'Place of Supply'"),
    (re.compile(r"hsn/?sac", re.I), 0.5, "has HSN/SAC"),
]
NOT_INVOICE_SIGNALS = [
    (re.compile(r"purchase\s*/?\s*expense\s+approval", re.I), 4.0, "is an Approval form"),
    (re.compile(r"\bapproval\s+form\b", re.I), 2.0, "is an Approval form"),
    (re.compile(r"approval\s+decision", re.I), 1.0, "has 'Approval Decision'"),
    (re.compile(r"gstr-?2a", re.I), 3.0, "is a GSTR-2A return"),
]
# Filename hint is weak (human-maintained, unreliable) — a tiebreak only.
RE_FILENAME_INVOICE = re.compile(r"^invoice[_\-\s]", re.I)
RE_FILENAME_APPROVAL = re.compile(r"^approval[_\-\s]", re.I)
FILENAME_HINT_WEIGHT = 1.0

# Net score (positive - negative) at/above which a doc is classified an invoice.
INVOICE_SCORE_THRESHOLD = 3.0


# --------------------------------------------------------------------------- #
# Field extraction (Phase 1: detect + link; no line-item parsing)
# --------------------------------------------------------------------------- #
# id is taken from the FILENAME (canonical, per decision), content id is only
# cross-checked. Money/date/gstin are best-effort "relevant data we can gather".
RE_ID_FROM_FILENAME = re.compile(r"^(?:invoice[_\-\s])?(.+?)\.pdf$", re.I)
RE_INVOICE_NO_CONTENT = re.compile(r"invoice\s*no\.?\s*:?\s*([A-Za-z0-9][A-Za-z0-9/\-]+)", re.I)
RE_GSTIN = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b")
RE_DATE_CONTENT = re.compile(r"invoice\s*date\s*:?\s*([0-9]{1,2}[-/][A-Za-z]{3,9}[-/][0-9]{2,4})", re.I)
# Require the "Rs." currency marker before the amount so we grab the SUMMARY
# value ("Taxable Value\nRs. 199,866.89"), not the row number that follows the
# "Taxable Value (Rs.)" column header ("...(Rs.)\n1").
RE_TAXABLE = re.compile(r"taxable\s+value\s*:?\s*rs\.?\s*([\d,]+\.\d{2})", re.I)
RE_TOTAL = re.compile(r"total\s+invoice\s+value\s*:?\s*rs\.?\s*([\d,]+\.?\d*)", re.I)
RE_BILLTO_BLOCK = re.compile(r"bill\s*to\s*:?\s*(.+)", re.I)

# GSTIN of the buyer (bill-to). We resolve the vendor vs bill-to GSTIN by
# position: the seller GSTIN appears before "Bill To"; the buyer's after.
RE_BILLTO_MARKER = re.compile(r"bill\s*to", re.I)

# Legal suffixes / noise stripped before fuzzy vendor matching.
LEGAL_SUFFIXES = [
    "private limited", "pvt ltd", "pvt. ltd.", "pvt limited", "llp", "limited",
    "ltd", "inc", "incorporated", "co", "company", "& associates", "and associates",
]


# --------------------------------------------------------------------------- #
# Fuzzy vendor matching (folder company <-> PDF vendor)
# --------------------------------------------------------------------------- #
FUZZY_AUTO_ACCEPT = 90.0   # >= : names agree, no flag
FUZZY_SOFT_FLAG = 70.0     # [SOFT, AUTO): probably same, low-priority review
# < FUZZY_SOFT_FLAG        : hard mismatch -> review
# A shared GSTIN overrides the score entirely (definitive match).


# --------------------------------------------------------------------------- #
# Validation / confidence
# --------------------------------------------------------------------------- #
# taxable + CGST + SGST (or IGST) should ≈ total, within this absolute tolerance.
AMOUNT_TOLERANCE = 1.0
# Rows at/above this confidence auto-accept; below go to the review queue.
REVIEW_CONFIDENCE_THRESHOLD = 0.80


# --------------------------------------------------------------------------- #
# Excel output columns (Master sheet), in order.
# --------------------------------------------------------------------------- #
MASTER_COLUMNS = [
    "invoice_id", "company", "vendor_name_pdf", "vendor_gstin",
    "invoice_date", "taxable_value", "total_value",
    "bill_to_name", "bill_to_gstin",
    # GSTR-2A linking — the point of the tool, so it sits next to the identity
    # fields rather than at the far right of the sheet.
    "link_status", "gstr_row", "gstr_ref",
    "fy", "month", "date_folder", "bank",
    "source", "confidence", "reviewed", "flags", "rel_path", "file_path",
]


@dataclass(frozen=True)
class Settings:
    """Runtime-overridable knobs (CLI can override a few of these)."""
    bank_scope: str = BANK_SCOPE
    # One financial year per run: the FY folder name ("FY 22-23"), or None for every
    # year under the root. It lives here, beside bank_scope (the other per-run scope
    # filter), because run_scan hands Settings to a fixed-shape
    # `walker(root, settings, control=)` — the channel that reaches walk().
    fy_scope: str | None = None
    min_text_chars: int = MIN_TEXT_CHARS
    ocr_dpi: int = OCR_DPI
    ocr_lang: str = OCR_LANG
    ocr_config: str = OCR_CONFIG
    ocr_photo_enabled: bool = OCR_PHOTO_ENABLED
    ocr_photo_min_confidence: float = OCR_PHOTO_MIN_CONFIDENCE
    ocr_photo_max_dim: int = OCR_PHOTO_MAX_DIM
    ocr_photo_bg_radius: int = OCR_PHOTO_BG_RADIUS
    invoice_score_threshold: float = INVOICE_SCORE_THRESHOLD
    fuzzy_auto_accept: float = FUZZY_AUTO_ACCEPT
    fuzzy_soft_flag: float = FUZZY_SOFT_FLAG
    amount_tolerance: float = AMOUNT_TOLERANCE
    review_confidence_threshold: float = REVIEW_CONFIDENCE_THRESHOLD
    workers: int = 1  # >1 maps documents across a thread pool (OCR shells out to tesseract)
    ocr_enabled: bool = True
    # Treat each page of a multi-page PDF as its own invoice (a single PDF can
    # bundle several; no invoice spans two pages). Always on in the product — this
    # is a knob only so tests can exercise the single-Document-per-file path.
    # Single-page PDFs are byte-identical either way.
    explode_pages: bool = True


DEFAULTS = Settings()
