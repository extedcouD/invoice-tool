"""Link detected invoices into an externally-supplied GSTR-2A return.

For each B2B invoice row in the return, we find the matching PDF from a scan run,
copy it into a single flat folder under a unique, human-meaningful name
(`<GSTIN>__<invoice-no>.pdf`), and write that name into a new `Invoice Ref` column
on the sheet — so a reviewer can jump from any row straight to the source PDF.

Matching keys on `(GSTIN, normalized invoice number)` taken from the PDF's content
and filename — never the folder's FY/month — so an invoice paid in a later year still
links to its row. Rows we cannot match are called out explicitly (`NOT FOUND`,
highlighted, and listed on a `Link Report` sheet). The input template is never
modified; a new `<stem>_linked.xlsx` is written into the run dir.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ..core.models import Document, RunResult
from ..io.excel import _FLAG_FILL, _HEADER_FILL, _HEADER_FONT, _autosize, _style_header
from ..io.runstore import RunStore
from ..matching import vendor

DEFAULT_SHEET = "Part A - B2B Invoices"
DEFAULT_REF_HEADER = "Invoice Ref"
GSTIN_HEADER = "GSTIN of Supplier"
INVNO_HEADER = "Invoice Number"
REPORT_SHEET = "Link Report"


@dataclass
class LinkReport:
    total: int = 0                 # B2B data rows considered
    matched: int = 0
    not_found: int = 0
    ambiguous: int = 0
    copy_failed: int = 0
    duplicate_filings: int = 0     # matched rows resolved from >1 copy of the same invoice
    unreferenced_invoices: int = 0  # detected invoices with no B2B row (reverse gap)
    unmatched_rows: list = field(default_factory=list)   # (row, gstin, invno)
    ambiguous_rows: list = field(default_factory=list)   # (row, gstin, invno, n_candidates)
    out_path: Path | None = None
    flat_dir: Path | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _sanitize(s: str) -> str:
    """Filesystem-safe token: slashes/spaces -> '-', drop other unsafe chars."""
    s = s.strip().replace("/", "-").replace("\\", "-")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    return s.strip("-._")


def _flat_name(gstin_raw: str, invno_raw: str) -> str:
    g = _sanitize(gstin_raw) or "NOGSTIN"
    n = _sanitize(invno_raw) or "NOINV"
    return f"{g}__{n}.pdf"


def _build_index(invoices: list[Document]):
    """Index detected invoices by (norm_gstin, norm_invno) and by norm_invno alone.

    Each doc registers under BOTH its filename id and its printed content number, so a
    junk-named file still matches on the number printed inside the PDF. Inner dicts are
    keyed by doc.id to dedupe.
    """
    by_key: dict[tuple[str, str], dict[str, Document]] = {}
    by_invno: dict[str, dict[str, Document]] = {}
    for d in invoices:
        g = vendor.norm_gstin(d.fields.vendor_gstin)
        for raw in (d.fields.invoice_id, d.fields.invoice_no_content):
            n = vendor.norm_id(raw)
            if not n:
                continue
            by_invno.setdefault(n, {})[d.id] = d
            if g:
                by_key.setdefault((g, n), {})[d.id] = d
    return by_key, by_invno


def _resolve(by_key, by_invno, g: str, n: str) -> list[Document]:
    """Candidate docs for a row: composite key first, then invoice-no-only
    (narrowed by GSTIN when that leaves any candidate)."""
    if not n:
        return []
    if g and (g, n) in by_key:
        return list(by_key[(g, n)].values())
    if n in by_invno:
        cands = list(by_invno[n].values())
        if g:
            narrowed = [d for d in cands if vendor.norm_gstin(d.fields.vendor_gstin) == g]
            if narrowed:
                return narrowed
        return cands
    return []


def _pick_best(cands: list[Document]) -> Document:
    """Highest confidence wins; deterministic tie-break by doc.id."""
    return max(cands, key=lambda d: (d.confidence, d.id))


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def link_gstr(result: RunResult, gstr_path: Path, store: RunStore,
              sheet_name: str = DEFAULT_SHEET,
              ref_header: str = DEFAULT_REF_HEADER) -> LinkReport:
    gstr_path = Path(gstr_path)
    invoices = result.invoices()
    by_key, by_invno = _build_index(invoices)

    wb = load_workbook(gstr_path)  # read-write, preserves styles/formulas
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"sheet {sheet_name!r} not found; available: {wb.sheetnames}")
    ws = wb[sheet_name]

    # locate columns by header text (row 1), tolerant of case/whitespace
    headers = {_s(c.value).lower(): c.column for c in ws[1] if c.value is not None}
    gstin_col = headers.get(GSTIN_HEADER.lower())
    invno_col = headers.get(INVNO_HEADER.lower())
    if not gstin_col or not invno_col:
        raise ValueError(
            f"could not find '{GSTIN_HEADER}' and '{INVNO_HEADER}' columns in "
            f"sheet {sheet_name!r}; headers seen: {[_s(c.value) for c in ws[1]]}")

    # find-or-append the Invoice Ref column (idempotent on re-run)
    ref_col = headers.get(ref_header.lower())
    if not ref_col:
        ref_col = ws.max_column + 1
        cell = ws.cell(row=1, column=ref_col, value=ref_header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT

    # fresh flat folder so re-runs don't leave stale copies
    flat_dir = store.linked_dir
    shutil.rmtree(flat_dir, ignore_errors=True)
    flat_dir.mkdir(parents=True, exist_ok=True)

    rep = LinkReport(out_path=store.gstr_linked_path(gstr_path), flat_dir=flat_dir)
    matched_ids: set[str] = set()
    used_names: set[str] = set()

    for r in range(2, ws.max_row + 1):
        gstin_raw = _s(ws.cell(row=r, column=gstin_col).value)
        invno_raw = _s(ws.cell(row=r, column=invno_col).value)
        if not gstin_raw and not invno_raw:
            continue  # skip trailing/blank rows
        rep.total += 1

        g, n = vendor.norm_gstin(gstin_raw), vendor.norm_id(invno_raw)
        cands = _resolve(by_key, by_invno, g, n)

        ref_cell = ws.cell(row=r, column=ref_col)
        if not cands:
            rep.not_found += 1
            rep.unmatched_rows.append((r, gstin_raw, invno_raw))
            ref_cell.value = "NOT FOUND"
            ref_cell.fill = _FLAG_FILL
            continue

        # >1 candidate: same invoice filed twice (dup) vs genuinely conflicting GSTINs
        gstins = {vendor.norm_gstin(d.fields.vendor_gstin) for d in cands
                  if d.fields.vendor_gstin}
        if len(cands) > 1 and len(gstins) > 1:
            rep.ambiguous += 1
            rep.ambiguous_rows.append((r, gstin_raw, invno_raw, len(cands)))
            ref_cell.value = f"AMBIGUOUS ({len(cands)})"
            ref_cell.fill = _FLAG_FILL
            continue

        doc = _pick_best(cands)
        if len(cands) > 1:
            rep.duplicate_filings += 1

        name = _flat_name(gstin_raw, invno_raw)
        while name in used_names:  # extremely unlikely; guarantees folder uniqueness
            name = name[:-4] + "-dup.pdf"
        try:
            shutil.copy2(Path(doc.path), flat_dir / name)
        except OSError:
            rep.copy_failed += 1
            ref_cell.value = "COPY FAILED"
            ref_cell.fill = _FLAG_FILL
            continue

        used_names.add(name)
        matched_ids.add(doc.id)
        rep.matched += 1
        ref_cell.value = name

    rep.unreferenced_invoices = sum(1 for d in invoices if d.id not in matched_ids)
    _autosize(ws, [ref_header])  # widen at least the ref column
    _write_report_sheet(wb, rep, gstr_path, sheet_name)

    wb.save(rep.out_path)
    return rep


def _write_report_sheet(wb, rep: LinkReport, gstr_path: Path, sheet_name: str) -> None:
    if REPORT_SHEET in wb.sheetnames:
        del wb[REPORT_SHEET]
    ws = wb.create_sheet(REPORT_SHEET)
    ws.append(["metric", "value"])
    _style_header(ws, 2)
    for k, v in [
        ("source_template", gstr_path.name),
        ("b2b_sheet", sheet_name),
        ("b2b_rows", rep.total),
        ("matched", rep.matched),
        ("not_found", rep.not_found),
        ("ambiguous", rep.ambiguous),
        ("copy_failed", rep.copy_failed),
        ("duplicate_filings", rep.duplicate_filings),
        ("detected_invoices_unreferenced", rep.unreferenced_invoices),
    ]:
        ws.append([k, v])

    if rep.unmatched_rows:
        ws.append([])
        ws.append(["NOT FOUND — no PDF matched these B2B rows"])
        ws.append(["row", "GSTIN of Supplier", "Invoice Number"])
        for row, gstin, invno in rep.unmatched_rows:
            ws.append([row, gstin, invno])

    if rep.ambiguous_rows:
        ws.append([])
        ws.append(["AMBIGUOUS — multiple conflicting matches"])
        ws.append(["row", "GSTIN of Supplier", "Invoice Number", "candidates"])
        for row, gstin, invno, ncand in rep.ambiguous_rows:
            ws.append([row, gstin, invno, ncand])

    _autosize(ws, ["metric", "value", "col3", "col4"])
