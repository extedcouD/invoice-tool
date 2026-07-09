"""Reconcile stage: cross-check folder-derived facts against PDF content.

- company (folder) vs vendor name (PDF): fuzzy, two-tier flagging.
- invoice id (filename) vs invoice-no (PDF body): normalized equality.

NOTE: we deliberately do NOT reconcile the folder date against the invoice
date — the folder is the *payment* date, which legitimately differs from the
invoice date (seen in the sample: invoice 06-Sep, paid/foldered 24-Sep).
"""
from __future__ import annotations

from ..config import Settings, DEFAULTS
from ..core.interfaces import Stage
from ..core.models import Document, DocType, Severity
from ..matching import vendor
from ..observability.events import record


class ReconcileStage(Stage):
    name = "reconcile"

    def __init__(self, settings: Settings = DEFAULTS) -> None:
        self.s = settings

    def process(self, doc: Document) -> Document:
        if doc.doc_type != DocType.INVOICE:
            return doc

        f = doc.fields
        company = doc.path_info.company

        # --- company vs vendor -------------------------------------------
        if company and f.vendor_name_pdf:
            sc = vendor.score(company, f.vendor_name_pdf)
            if sc >= self.s.fuzzy_auto_accept:
                record(doc, self.name, "vendor_match",
                       f"{sc:.0f} '{company}' ~ '{f.vendor_name_pdf}'", score=sc)
            elif sc >= self.s.fuzzy_soft_flag:
                doc.add_flag("company_name_soft_mismatch",
                             f"folder '{company}' vs PDF '{f.vendor_name_pdf}' (fuzzy {sc:.0f})",
                             Severity.INFO)
                record(doc, self.name, "vendor_soft_mismatch", f"{sc:.0f}", score=sc)
            else:
                doc.add_flag("company_mismatch",
                             f"folder '{company}' vs PDF '{f.vendor_name_pdf}' (fuzzy {sc:.0f})")
                record(doc, self.name, "vendor_mismatch", f"{sc:.0f}",
                       severity="warn", score=sc)
        elif company and not f.vendor_name_pdf:
            record(doc, self.name, "vendor_unparsed",
                   "no vendor name in PDF to compare against folder")

        # --- filename id vs content invoice-no ---------------------------
        if f.invoice_id and f.invoice_no_content:
            if vendor.norm_id(f.invoice_id) != vendor.norm_id(f.invoice_no_content):
                doc.add_flag("id_mismatch",
                             f"filename id '{f.invoice_id}' vs PDF '{f.invoice_no_content}'")
                record(doc, self.name, "id_mismatch",
                       f"{f.invoice_id} != {f.invoice_no_content}", severity="warn")
            else:
                record(doc, self.name, "id_match", f.invoice_id)
        return doc
