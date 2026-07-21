"""Recovering a match from an already-exported `_linked.xlsx`.

`write_linked`'s "Invoice Ref" cell is a synthesized `<GSTIN>__<invoice-no>.pdf`
built from the row's own columns (see `io/gstr.py::_flat_name`) — it never says
which Document satisfied a row, only that the row *was* matched. So recovery
can't look a doc up by that cell; it retries matching against every Document
(not just `result.invoices()`), which recovers exactly the shape the
continue-run bug produced: a promoted doc's `doc_type` reverting, dropping it
back out of `invoices()` even though its fields are untouched.
"""
from __future__ import annotations

import threading
import time

from openpyxl import Workbook

from invoices.core.interfaces import FileSource
from invoices.core.models import Document, DocType, Fields, RunResult
from invoices.detect import _backfill_from_linked_xlsx
from invoices.io.gstr import DEFAULT_SHEET, match, read_b2b_rows, write_linked
from invoices.io.runstore import RunStore


def _inv(doc_id: str, number: str, gstin: str, doc_type=DocType.INVOICE) -> Document:
    return Document(
        id=doc_id, path=f"/t/{doc_id}.pdf", filename=f"{doc_id}.pdf",
        size_bytes=1, source_key=doc_id, doc_type=doc_type,
        fields=Fields(invoice_id=number, vendor_gstin=gstin),
    )


class FixedSource(FileSource):
    """A trivial FileSource: every materialize() returns the same local PDF."""

    def __init__(self, pdf):
        self.pdf = pdf

    def materialize(self, doc: Document) -> str:
        return str(self.pdf)

    def cleanup(self, doc: Document, path: str) -> None:
        pass


def _build_linked_run(tmp_path):
    """3 rows, all matched: row 2 -> d0, row 3 -> d1, row 4 has no matching doc
    at all (stays NOT FOUND in the export). Returns (store, result, gstr, docs)."""
    gstr = tmp_path / "GSTR2A_Return.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = DEFAULT_SHEET
    ws.append(["GSTIN of Supplier", "Invoice Number", "Invoice Value"])
    ws.append(["27ABCDE0000F1Z5", "INV-0", 100])
    ws.append(["27FGHIJ0000F1Z6", "INV-1", 100])
    ws.append(["27NOPE00000F1Z1", "NOPE-1", 100])   # never matches anything
    wb.save(gstr)

    docs = [
        _inv("d0", "INV-0", "27ABCDE0000F1Z5"),
        _inv("d1", "INV-1", "27FGHIJ0000F1Z6"),
    ]
    pdf = tmp_path / "src.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")

    store = RunStore.new(tmp_path / "out", run_id="t")
    result = RunResult(run_id="t", root=str(tmp_path), documents=docs)
    rows = read_b2b_rows(gstr)
    plan = match(rows, result.invoices())
    write_linked(result, plan, gstr, store, file_source=FixedSource(pdf))
    return store, result, gstr, docs


def test_recovers_a_doc_that_reverted_out_of_invoices(tmp_path):
    store, result, gstr, docs = _build_linked_run(tmp_path)

    # Simulate exactly what the continue-run bug did to a bind_row promotion:
    # doc_type flips back, dropping the doc out of invoices(), fields untouched.
    docs[0].doc_type = DocType.OTHER

    recovered = _backfill_from_linked_xlsx(result, gstr, store)

    assert recovered == 1
    assert docs[0].doc_type == DocType.INVOICE
    assert store.manual_links() == {2: "d0"}
    assert docs[0].reviewed is False   # an inference, not a human's own click


def test_does_not_touch_a_row_that_was_never_matched(tmp_path):
    store, result, gstr, docs = _build_linked_run(tmp_path)
    docs[0].doc_type = DocType.OTHER

    _backfill_from_linked_xlsx(result, gstr, store)

    assert 4 not in store.manual_links()   # row 4 (NOPE-1) was NOT FOUND in the export too


def test_never_overrides_an_existing_manual_link(tmp_path):
    store, result, gstr, docs = _build_linked_run(tmp_path)
    docs[1].doc_type = DocType.OTHER
    store.set_manual_link(3, "some-other-doc")   # a human already decided differently

    _backfill_from_linked_xlsx(result, gstr, store)

    assert store.manual_links()[3] == "some-other-doc"   # untouched
    assert docs[1].doc_type == DocType.OTHER              # not promoted


def test_returns_zero_with_no_linked_xlsx_yet(tmp_path):
    gstr = tmp_path / "GSTR2A_Return.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = DEFAULT_SHEET
    ws.append(["GSTIN of Supplier", "Invoice Number"])
    ws.append(["27ABCDE0000F1Z5", "INV-0"])
    wb.save(gstr)

    store = RunStore.new(tmp_path / "out", run_id="t2")
    result = RunResult(run_id="t2", root=str(tmp_path), documents=[])

    assert _backfill_from_linked_xlsx(result, gstr, store) == 0


def test_survives_a_corrupt_linked_xlsx(tmp_path):
    store, result, gstr, docs = _build_linked_run(tmp_path)
    docs[0].doc_type = DocType.OTHER

    linked_path = store.dir / f"{gstr.stem}_linked.xlsx"
    linked_path.write_text("not actually an xlsx")

    assert _backfill_from_linked_xlsx(result, gstr, store) == 0
    assert docs[0].doc_type == DocType.OTHER   # untouched — the bad file must not sink anything
