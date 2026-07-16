"""The long post-scan job (the export) must never block the request.

Copying every matched invoice and rewriting the workbook run inline took *seconds*
with the window frozen and no progress — you clicked Finish, nothing happened, and
every review edit queued behind the same write lock. It now runs on a thread and
publishes progress.
"""
from __future__ import annotations

import threading
import time

import pytest
from openpyxl import Workbook

from invoices.core.interfaces import FileSource
from invoices.core.models import Document, DocType, Fields, RunResult
from invoices.io.gstr import (DEFAULT_SHEET, B2BRow, match, read_b2b_rows,
                              write_linked)
from invoices.io.runstore import RunStore
from invoices.web.app import create_app
from invoices.web.runner import RunController


def _inv(doc_id: str, number: str, gstin: str) -> Document:
    return Document(
        id=doc_id, path=f"/t/{doc_id}.pdf", filename=f"{doc_id}.pdf",
        size_bytes=1, source_key=doc_id, doc_type=DocType.INVOICE,
        fields=Fields(invoice_id=number, vendor_gstin=gstin),
    )


class SlowSource(FileSource):
    """A slow FileSource: every materialize() sleeps, to prove the copies run
    in parallel through the seam rather than one at a time."""

    def __init__(self, pdf, delay=0.15):
        self.pdf, self.delay = pdf, delay
        self.concurrent = 0
        self.peak = 0
        self._lk = threading.Lock()

    def materialize(self, doc: Document) -> str:
        with self._lk:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        time.sleep(self.delay)
        with self._lk:
            self.concurrent -= 1
        return str(self.pdf)

    def cleanup(self, doc: Document, path: str) -> None:
        pass


@pytest.fixture()
def linked_env(tmp_path):
    """A store + result + plan + a real GSTR workbook, ready to export."""
    gstr = tmp_path / "GSTR2A_Return.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = DEFAULT_SHEET
    ws.append(["GSTIN of Supplier", "Invoice Number", "Invoice Value"])
    docs = []
    for i in range(8):
        gstin, num = f"27ABCDE{i:04d}F1Z5", f"INV-{i:03d}"
        ws.append([gstin, num, 100])
        docs.append(_inv(f"d{i}", num, gstin))
    wb.save(gstr)

    pdf = tmp_path / "src.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")

    store = RunStore.new(tmp_path / "out", run_id="t")
    result = RunResult(run_id="t", root=str(tmp_path), complete=True, documents=docs)
    rows = [B2BRow(row=i + 2, gstin=d.fields.vendor_gstin,
                   invoice_no=d.fields.invoice_id, supplier=None, value=100)
            for i, d in enumerate(docs)]
    plan = match(rows, result.invoices(), {})
    return store, result, plan, gstr, pdf


def test_write_linked_reports_progress_and_fetches_in_parallel(linked_env):
    store, result, plan, gstr, pdf = linked_env
    src = SlowSource(pdf, delay=0.15)
    seen: list[tuple[int, int]] = []

    t0 = time.perf_counter()
    rep = write_linked(result, plan, gstr, store, file_source=src,
                       on_progress=lambda d, n: seen.append((d, n)), workers=8)
    elapsed = time.perf_counter() - t0

    assert rep.matched == 8
    assert len(list(store.linked_dir.glob("*.pdf"))) == 8
    # progress is actually reported, ending at 8/8
    assert seen[0] == (0, 8) and seen[-1] == (8, 8)
    # and the downloads overlapped: serial would be 8 x 0.15s = 1.2s
    assert src.peak > 1, "downloads ran serially — the whole point was to overlap them"
    assert elapsed < 0.9, f"took {elapsed:.2f}s; serial would be ~1.2s"


def test_the_export_keeps_the_workbooks_real_name(linked_env):
    """`<stem>_linked.xlsx` is named after the return, so the return's name matters —
    a temp-named workbook would ship `tmpnpkuwvev_linked.xlsx` as the deliverable.
    """
    store, result, plan, gstr, pdf = linked_env
    rep = write_linked(result, plan, gstr, store, file_source=SlowSource(pdf, delay=0))
    assert rep.out_path.name == "GSTR2A_Return_linked.xlsx"


def test_finish_returns_immediately_and_reports_progress(linked_env):
    """The bug: /finish ran the export inline, so the POST took minutes."""
    store, result, plan, gstr, pdf = linked_env
    ctl = RunController(store.dir.parent)
    ctl.store, ctl.result, ctl.plan = store, result, plan
    ctl.gstr_path, ctl.phase = gstr, "done"
    ctl.file_source = SlowSource(pdf, delay=0.15)
    c = create_app(ctl).test_client()

    t0 = time.perf_counter()
    r = c.post("/finish")
    assert r.status_code == 200 and r.get_json()["ok"]
    assert time.perf_counter() - t0 < 0.5, "POST /finish blocked on the export"

    # while it runs, review is read-only (the export reads `result` as it writes)
    assert c.post("/approve_all").status_code == 302

    for _ in range(200):
        t = c.get("/api/task").get_json()
        if t["phase"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert t["phase"] == "done", t.get("error")
    assert t["done"] == t["total"] == 8
    assert ctl.link_report.matched == 8
    assert c.get("/finish").status_code == 200          # the report page


def test_read_b2b_rows_captures_invoice_date(tmp_path):
    """The return's Invoice Date rides onto the RowMatch so the review UI can show
    it. A real datetime cell is normalized to a plain YYYY-MM-DD (not a stray
    '00:00:00'); a free-text date is passed through verbatim."""
    import datetime as dt

    gstr = tmp_path / "R.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = DEFAULT_SHEET
    ws.append(["GSTIN of Supplier", "Invoice Number", "Invoice Date", "Invoice Value"])
    ws.append(["27ABCDE0000F1Z5", "INV-1", dt.datetime(2022, 8, 15), 100])
    ws.append(["27ZZZZZ0000F1Z9", "Z-9", "15/09/2022", 200])   # a free-text date
    wb.save(gstr)

    rows = read_b2b_rows(gstr)
    assert rows[0].invoice_date == "2022-08-15"       # datetime -> ISO date
    assert rows[1].invoice_date == "15/09/2022"       # free text left as-is

    # and it survives the pure match onto the RowMatch the queue renders
    plan = match(rows, [])
    assert plan.rows[0].invoice_date == "2022-08-15"


def test_write_linked_writes_skipped_and_preserves_a_prefilled_ref(tmp_path):
    """A skipped supplier's row exports SKIPPED; a pre-filled 'Invoice Ref' a human
    put on the sheet is preserved verbatim rather than clobbered with NOT FOUND."""
    from openpyxl import load_workbook

    gstr = tmp_path / "R.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = DEFAULT_SHEET
    ws.append(["GSTIN of Supplier", "Invoice Number",
               "Supplier Trade/Legal Name", "Invoice Ref"])
    ws.append(["27ABCDE0000F1Z5", "INV-1", "Apex", None])        # matched
    ws.append(["27ZZZZZ0000F1Z9", "Z-9", "Zephyr Ltd", None])   # skipped
    ws.append(["27CCCCC0000F1Z7", "C-5", "Carol", "prior.pdf"])  # unmatched + prefilled
    wb.save(gstr)

    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")
    store = RunStore.new(tmp_path / "out", run_id="tskip")
    doc = _inv("d1", "INV-1", "27ABCDE0000F1Z5")
    result = RunResult(run_id="tskip", root=str(tmp_path), documents=[doc])

    rows = read_b2b_rows(gstr)                       # captures the pre-filled ref
    plan = match(rows, result.invoices(), skipped=["Zephyr Ltd"])
    rep = write_linked(result, plan, gstr, store, file_source=SlowSource(pdf, delay=0))

    wb2 = load_workbook(rep.out_path)
    ws2 = wb2[DEFAULT_SHEET]
    refcol = next(c.column for c in ws2[1]
                  if str(c.value).strip().lower() == "invoice ref")
    vals = {ws2.cell(row=r, column=2).value: ws2.cell(row=r, column=refcol).value
            for r in range(2, ws2.max_row + 1)}
    wb2.close()

    assert vals["Z-9"] == "SKIPPED"
    assert vals["C-5"] == "prior.pdf"                # preserved, not clobbered
    assert vals["INV-1"].endswith(".pdf") and vals["INV-1"] != "NOT FOUND"
    assert rep.skipped == 1


def test_a_second_job_is_refused_while_one_runs(linked_env):
    store, result, plan, gstr, pdf = linked_env
    ctl = RunController(store.dir.parent)
    ctl.store, ctl.result, ctl.plan = store, result, plan
    ctl.gstr_path, ctl.phase = gstr, "done"
    ctl.file_source = SlowSource(pdf, delay=0.15)
    c = create_app(ctl).test_client()

    assert c.post("/finish").get_json()["ok"]
    r = c.post("/finish")                    # an impatient second click
    assert r.status_code == 409 and not r.get_json()["ok"]
