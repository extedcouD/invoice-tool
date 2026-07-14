"""The long post-scan jobs (export / Drive upload) must never block the request.

On a Drive run each matched invoice is a network download and each uploaded one a
Drive round-trip, so run inline these took *minutes* with the window frozen and no
progress — you clicked Finish, nothing happened, and every review edit queued behind
the same write lock. They now run on a thread and publish progress.
"""
from __future__ import annotations

import threading
import time

import pytest
from openpyxl import Workbook

from invoices.core.interfaces import FileSource
from invoices.core.models import Document, DocType, Fields, RunResult
from invoices.io.gstr import DEFAULT_SHEET, B2BRow, match, write_linked
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
    """Stands in for Drive: every materialize() is a slow network round-trip."""

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
    """`<stem>_linked.xlsx` is named after the return, so the return's name matters.

    On a Drive run the workbook arrives as a temp download; naming that temp file
    `tmpXXXX.xlsx` produced `tmpnpkuwvev_linked.xlsx` as the user's deliverable.
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
