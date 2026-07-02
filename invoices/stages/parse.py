"""Parse stage: pull the Phase-1 fields we can link/report on.

Decisions honoured here:
  - invoice_id comes from the FILENAME (canonical); the content invoice-no is
    parsed only to cross-check.
  - No line-item parsing. We grab the easy "relevant data": vendor, GSTIN, date,
    taxable/total, and bill-to (best-effort).

Extraction is a baseline pass followed by any matching **per-vendor** extractors
(see invoices/extractors.py), so a new vendor template is added without touching
this stage.
"""
from __future__ import annotations

from ..config import RE_ID_FROM_FILENAME
from ..core.interfaces import Stage
from ..core.models import Document, DocType
from ..extractors import default_extract, matching
from ..observability.events import record


def _id_from_filename(filename: str) -> str | None:
    m = RE_ID_FROM_FILENAME.match(filename)
    return m.group(1).strip() if m else None


class ParseStage(Stage):
    name = "parse"

    def process(self, doc: Document) -> Document:
        # id from filename always (cheap, canonical, useful for linking).
        doc.fields.invoice_id = _id_from_filename(doc.filename)

        # Only mine body fields for actual invoices.
        if doc.doc_type != DocType.INVOICE:
            record(doc, self.name, "skipped",
                   f"doc_type={doc.doc_type.value}; id={doc.fields.invoice_id}")
            return doc

        text = doc.text or ""
        # 1) baseline
        try:
            default_extract(text, doc)
        except Exception as exc:  # a bad baseline must not sink the doc
            record(doc, self.name, "baseline_error", str(exc), severity="warn")

        # 2) per-vendor overrides (matched off baseline-parsed fields/content)
        applied: list[str] = []
        for ve in matching(doc):
            try:
                ve.apply(text, doc)
                applied.append(ve.name)
            except Exception as exc:
                record(doc, self.name, "vendor_extractor_error",
                       f"{ve.name}: {exc}", severity="warn")
        if applied:
            record(doc, self.name, "vendor_extractors", f"applied={applied}",
                   applied=applied)

        f = doc.fields
        got = [k for k in ("vendor_name_pdf", "vendor_gstin", "invoice_date",
                           "taxable_value", "total_value") if getattr(f, k) is not None]
        missing = [k for k in ("vendor_gstin", "invoice_date", "total_value")
                   if getattr(f, k) is None]
        record(doc, self.name, "parsed",
               f"id={f.invoice_id} got={got} missing={missing}",
               got=got, missing=missing)
        return doc
