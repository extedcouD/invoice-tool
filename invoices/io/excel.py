"""Excel export: Master / Exceptions / Summary sheets, built with openpyxl.

A fresh workbook is written per run (per the "new Excel from scratch" decision).
"""
from __future__ import annotations

import os
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..config import MASTER_COLUMNS
from ..core.models import Document, RunResult

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_FLAG_FILL = PatternFill("solid", fgColor="FCE4D6")


def _rel(path: str, root: str | None) -> str:
    """Path relative to the scanned root; falls back to the basename off-tree."""
    if not root:
        return os.path.basename(path)
    try:
        return os.path.relpath(path, root)
    except ValueError:  # e.g. different drive on Windows
        return os.path.basename(path)


def _row_for(doc: Document, root: str | None) -> dict:
    f = doc.fields
    p = doc.path_info
    return {
        "invoice_id": f.invoice_id or "",
        "company": p.company or "",
        "vendor_name_pdf": f.vendor_name_pdf or "",
        "vendor_gstin": f.vendor_gstin or "",
        "invoice_date": f.invoice_date or f.invoice_date_raw or "",
        "taxable_value": f.taxable_value if f.taxable_value is not None else "",
        "total_value": f.total_value if f.total_value is not None else "",
        "bill_to_name": f.bill_to_name or "",
        "bill_to_gstin": f.bill_to_gstin or "",
        "fy": p.fy or "",
        "month": p.month or "",
        "date_folder": p.date_folder or "",
        "bank": p.bank or "",
        "source": doc.source.value,
        "confidence": round(doc.confidence, 3),
        "reviewed": "yes" if doc.reviewed else "",
        "flags": "; ".join(fl.code for fl in doc.flags),
        "rel_path": _rel(doc.path, root),
        "file_path": doc.path,
    }


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"


def _autosize(ws, headers: list[str]) -> None:
    for i, h in enumerate(headers, start=1):
        width = max(len(h) + 2, 12)
        ws.column_dimensions[get_column_letter(i)].width = min(width, 40)


def write_master(result: RunResult, path: Path) -> Path:
    wb = Workbook()

    # -- Master: one row per detected invoice -------------------------------
    ws = wb.active
    ws.title = "Master"
    ws.append(MASTER_COLUMNS)
    _style_header(ws, len(MASTER_COLUMNS))
    for doc in result.invoices():
        row = _row_for(doc, result.root)
        ws.append([row[c] for c in MASTER_COLUMNS])
        if doc.needs_review:
            for c in range(1, len(MASTER_COLUMNS) + 1):
                ws.cell(row=ws.max_row, column=c).fill = _FLAG_FILL
    _autosize(ws, MASTER_COLUMNS)

    # -- Exceptions: flagged invoices + non-invoice PDFs in scope -----------
    exc = wb.create_sheet("Exceptions")
    exc_cols = ["rel_path", "file_path", "doc_type", "reason", "flags", "confidence",
                "review_pdf", "company_folder"]
    exc.append(exc_cols)
    _style_header(exc, len(exc_cols))
    for doc in result.documents:
        flagged = doc.needs_review
        odd = doc.doc_type.value not in ("invoice", "approval")
        if not (flagged or odd or doc.error):
            continue
        exc.append([
            _rel(doc.path, result.root),
            doc.path,
            doc.doc_type.value,
            doc.error or ("; ".join(fl.message for fl in doc.flags)) or "non-standard doc",
            "; ".join(fl.code for fl in doc.flags),
            round(doc.confidence, 3),
            doc.review_pdf_path or "",
            doc.path_info.company or "",
        ])
    _autosize(exc, exc_cols)

    # -- Summary: rollups ----------------------------------------------------
    s = wb.create_sheet("Summary")
    s.append(["metric", "value"])
    _style_header(s, 2)
    summ = result.summary()
    rows = [
        ("run_id", result.run_id),
        ("root", result.root),
        ("total_pdfs", summ["total_pdfs"]),
        ("invoices", summ["invoices"]),
        ("flagged_for_review", summ["flagged"]),
        ("reviewed", summ["reviewed"]),
        ("ocr_docs", summ["ocr_docs"]),
        ("ocr_ratio", summ["ocr_ratio"]),
        ("avg_invoice_confidence", summ["avg_invoice_confidence"]),
    ]
    for k, v in summ["by_type"].items():
        rows.append((f"doc_type:{k}", v))
    for k, v in rows:
        s.append([k, v])
    _autosize(s, ["metric", "value"])

    path = Path(path)
    wb.save(path)
    return path
