"""Validate stage: compute a confidence score and decide review-worthiness.

Confidence is a transparent additive model (every deduction is recorded), so a
reviewer can see *why* a row scored low. Any flag — from any stage — already
routes a doc to the review queue; a low score adds an explicit `low_confidence`
flag on top.
"""
from __future__ import annotations

from ..config import Settings, DEFAULTS
from ..core.interfaces import Stage
from ..core.models import Document, DocType, TextSourceKind
from ..observability.events import record


class ValidateStage(Stage):
    name = "validate"

    def __init__(self, settings: Settings = DEFAULTS) -> None:
        self.s = settings

    def process(self, doc: Document) -> Document:
        if doc.doc_type != DocType.INVOICE:
            return doc

        f = doc.fields
        conf = 1.0
        deductions: list[str] = []

        def cut(amount: float, why: str) -> None:
            nonlocal conf
            conf -= amount
            deductions.append(f"-{amount:.2f} {why}")

        if doc.source == TextSourceKind.OCR:
            cut(0.15, "OCR source")
        if not f.vendor_gstin:
            cut(0.15, "no vendor GSTIN")
        if not f.invoice_date:
            cut(0.10, "no invoice date")
        if f.total_value is None:
            cut(0.15, "no total value")
        if f.taxable_value is None:
            cut(0.05, "no taxable value")
        if not f.vendor_name_pdf:
            cut(0.10, "no vendor name")

        # amount sanity: total should be >= taxable (GST is additive).
        if f.taxable_value is not None and f.total_value is not None:
            if f.total_value + self.s.amount_tolerance < f.taxable_value:
                cut(0.20, "total < taxable")
                doc.add_flag("amount_inconsistent",
                             f"total {f.total_value} < taxable {f.taxable_value}")

        # existing flags from earlier stages reduce confidence too
        for fl in doc.flags:
            if fl.code == "company_mismatch":
                cut(0.30, "company mismatch")
            elif fl.code == "company_name_soft_mismatch":
                cut(0.10, "company soft mismatch")
            elif fl.code == "id_mismatch":
                cut(0.20, "id mismatch")

        conf = max(0.0, min(1.0, conf))
        doc.confidence = round(conf, 3)

        if conf < self.s.review_confidence_threshold:
            doc.add_flag("low_confidence", f"confidence {conf:.2f} below "
                         f"{self.s.review_confidence_threshold:.2f}")

        record(doc, self.name, f"confidence={conf:.2f}",
               "; ".join(deductions) or "no deductions",
               confidence=conf, needs_review=doc.needs_review)
        return doc
