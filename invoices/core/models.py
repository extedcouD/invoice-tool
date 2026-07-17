"""Domain models. A `Document` is the single object that flows through the
pipeline; every stage enriches it and appends `Event`s explaining what it did.

The event trail is the backbone of observability: any decision (why a PDF was
classified an invoice, why a field was flagged) is recorded on the document and
later surfaced in the web trace viewer.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class DocType(str, Enum):
    UNKNOWN = "unknown"
    INVOICE = "invoice"
    APPROVAL = "approval"
    GSTR2A = "gstr2a"
    OTHER = "other"


class TextSourceKind(str, Enum):
    NONE = "none"
    TEXT = "text"      # extracted from the PDF text layer
    OCR = "ocr"        # produced by Tesseract on a rendered page


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LinkStatus(str, Enum):
    """Where an invoice stands against the GSTR-2A return.

    ``UNREFERENCED`` is the reverse gap — a detected invoice that satisfies no
    B2B row. It is the counterpart of a ``not_found`` row and, like it, is
    something a human has to resolve.
    """
    UNKNOWN = "unknown"            # no GSTR-2A supplied, or not matched yet
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"        # this doc is one of several candidates for a row
    UNREFERENCED = "unreferenced"  # detected invoice with no B2B row


class Event(BaseModel):
    """One recorded decision/observation made by a stage about a document."""
    stage: str
    action: str
    detail: str = ""
    severity: Severity = Severity.INFO
    # seconds since run start; stamped by the events helper (no wall-clock in models)
    t: float = 0.0
    data: dict[str, Any] = Field(default_factory=dict)


class Flag(BaseModel):
    """A reason a document needs human review."""
    code: str                 # machine key, e.g. "company_mismatch"
    message: str              # human text
    severity: Severity = Severity.WARN


class PathInfo(BaseModel):
    """Structured breakdown of the folder path a PDF was found in."""
    fy: Optional[str] = None
    bank: Optional[str] = None
    month: Optional[str] = None
    date_folder: Optional[str] = None
    company: Optional[str] = None       # folder-derived company name
    unmatched: list[str] = Field(default_factory=list)  # path parts we couldn't label


class Fields(BaseModel):
    """Extracted invoice fields (Phase 1 subset)."""
    invoice_id: Optional[str] = None          # canonical, from filename
    invoice_no_content: Optional[str] = None  # id printed inside the PDF (cross-check)
    vendor_name_pdf: Optional[str] = None
    vendor_gstin: Optional[str] = None
    invoice_date: Optional[str] = None        # normalized ISO (YYYY-MM-DD) when parseable
    invoice_date_raw: Optional[str] = None
    taxable_value: Optional[float] = None
    total_value: Optional[float] = None
    bill_to_name: Optional[str] = None
    bill_to_gstin: Optional[str] = None


class Document(BaseModel):
    """State carried through the whole pipeline."""
    id: str                                   # stable per-run id (index-based)
    path: str                                 # absolute path to the source PDF
    filename: str
    size_bytes: int = 0

    # Stable identity of the source bytes, used to skip already-processed files
    # when a scan resumes ("local:<abs-path>").
    source_key: str = ""

    path_info: PathInfo = Field(default_factory=PathInfo)
    doc_type: DocType = DocType.UNKNOWN
    classify_score: float = 0.0

    source: TextSourceKind = TextSourceKind.NONE
    text: str = ""
    page_count: int = 0
    # 0-based page of a multi-page source PDF, set when one PDF bundling several
    # invoices is exploded into one Document per page. None = the whole file (a
    # single-page PDF, or splitting off). This is THE discriminator for the
    # multi-invoice path: extract reads only this page, the id gets a page suffix,
    # and the deliverable copy is sliced to just this page.
    page_index: Optional[int] = None

    fields: Fields = Field(default_factory=Fields)

    confidence: float = 0.0
    flags: list[Flag] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)

    reviewed: bool = False                    # a human confirmed/corrected this row
    review_pdf_path: Optional[str] = None     # copy placed in review folder, if flagged
    error: Optional[str] = None               # set if a stage hard-failed on this doc
    hidden: bool = False                      # human-hidden from the link finder (search/browse)

    # ---- GSTR-2A linking ---------------------------------------------------
    # Stamped by `apply_plan` after every (re)match, so link state round-trips
    # through detections.json and is visible in the review UI — the whole point
    # of the tool is which invoice satisfies which B2B row.
    link_status: LinkStatus = LinkStatus.UNKNOWN
    gstr_row: Optional[int] = None            # the B2B row this invoice satisfies
    gstr_ref: Optional[str] = None            # filename written into 'Invoice Ref'

    # ---- convenience -------------------------------------------------------
    @property
    def is_invoice(self) -> bool:
        return self.doc_type == DocType.INVOICE

    @property
    def is_page_invoice(self) -> bool:
        """True when this Document is one page of an exploded multi-page PDF."""
        return self.page_index is not None

    @property
    def needs_review(self) -> bool:
        return bool(self.flags) and not self.reviewed

    def add_flag(self, code: str, message: str, severity: Severity = Severity.WARN) -> None:
        if not any(f.code == code for f in self.flags):
            self.flags.append(Flag(code=code, message=message, severity=severity))


class StageMetric(BaseModel):
    stage: str
    docs_in: int = 0
    docs_out: int = 0
    errors: int = 0
    seconds: float = 0.0


class RunResult(BaseModel):
    """Everything about one `scan` run — persisted to detections.json."""
    run_id: str
    root: str
    started_at: str = ""            # ISO string, stamped by caller (no clock in models)
    finished_at: str = ""
    # False when the user stopped the scan before the tree was exhausted. The one
    # source of truth for "is this run resumable" — the UI's stopped screen and
    # RunStore.find_resumable must never disagree about it.
    complete: bool = True
    documents: list[Document] = Field(default_factory=list)
    stage_metrics: list[StageMetric] = Field(default_factory=list)

    # ---- rollups (computed, not stored authoritatively) --------------------
    def invoices(self) -> list[Document]:
        return [d for d in self.documents if d.is_invoice]

    def flagged(self) -> list[Document]:
        return [d for d in self.documents if d.needs_review]

    def summary(self) -> dict[str, Any]:
        docs = self.documents
        by_type: dict[str, int] = {}
        for d in docs:
            by_type[d.doc_type.value] = by_type.get(d.doc_type.value, 0) + 1
        invs = self.invoices()
        ocr = sum(1 for d in docs if d.source == TextSourceKind.OCR)
        conf = [d.confidence for d in invs] or [0.0]
        return {
            "total_pdfs": len(docs),
            "by_type": by_type,
            "invoices": len(invs),
            "flagged": len(self.flagged()),
            "reviewed": sum(1 for d in docs if d.reviewed),
            "ocr_docs": ocr,
            "ocr_ratio": round(ocr / len(docs), 3) if docs else 0.0,
            "avg_invoice_confidence": round(sum(conf) / len(conf), 3),
        }
