"""Multiple invoices in one PDF: each page is treated as its own invoice.

`walk_pages` explodes a multi-page PDF into per-page seed Documents on the
generator side (the pipeline is strictly one-doc-in → one-doc-out per stage);
`extract` reads only that page; `parse` suffixes the id so siblings don't
false-flag as duplicates; and the deliverable copy is sliced to one page. A
single-page file stays byte-identical to plain `walk()`.

Pure — builds tiny real PDFs in tmp_path with PyMuPDF; no scan, no OCR.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz
import pytest

from invoices.config import DEFAULTS
from invoices.core.models import Document, DocType, RunResult
from invoices.detect import _flag_duplicate_ids
from invoices.io.pdf import PdfTextSource, write_page_pdf
from invoices.stages.parse import ParseStage
from invoices.stages.walk import doc_id_for, walk, walk_pages


def _pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for txt in pages:
        pg = doc.new_page()
        pg.insert_text((72, 72), txt, fontsize=11)
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    folder = (tmp_path / "FY 22-23" / "Payments" / "Kotak" / "Sep-2022"
              / "24-Sep-2022" / "Apex")
    folder.mkdir(parents=True)
    _pdf(folder / "invoice_multi.pdf",
         ["PAGE ONE alpha content", "PAGE TWO beta content", "PAGE THREE gamma content"])
    _pdf(folder / "invoice_single.pdf", ["ONLY PAGE solo content"])
    return tmp_path


def _paged(root: Path, **kw):
    return list(walk_pages(root, replace(DEFAULTS, **kw)))


# ---- explosion on the generator side --------------------------------------
def test_walk_pages_explodes_a_multipage_pdf(tree: Path) -> None:
    multi = [d for d in _paged(tree) if d.filename == "invoice_multi.pdf"]
    assert len(multi) == 3
    assert sorted(d.page_index for d in multi) == [0, 1, 2]
    assert all("#page=" in d.source_key for d in multi)
    assert len({d.id for d in multi}) == 3                 # per-page unique ids
    d0 = next(d for d in multi if d.page_index == 0)
    assert d0.id == doc_id_for(d0.source_key)              # id derives from the key


def test_walk_pages_leaves_a_single_page_byte_identical(tree: Path) -> None:
    p = next(d for d in _paged(tree) if d.filename == "invoice_single.pdf")
    q = next(d for d in walk(tree, DEFAULTS) if d.filename == "invoice_single.pdf")
    assert p.page_index is None
    assert p.source_key == q.source_key and p.id == q.id
    assert "#page=" not in p.source_key


def test_walk_pages_off_is_a_passthrough(tree: Path) -> None:
    docs = _paged(tree, explode_pages=False)
    assert all(d.page_index is None for d in docs)
    assert len(docs) == len(list(walk(tree, DEFAULTS)))


def test_walk_pages_tolerates_a_corrupt_pdf(tmp_path: Path) -> None:
    folder = tmp_path / "FY 22-23" / "Payments" / "Kotak" / "m" / "d" / "Co"
    folder.mkdir(parents=True)
    (folder / "invoice_bad.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    docs = _paged(tmp_path)
    assert len(docs) == 1 and docs[0].page_index is None   # fell back, no crash


# ---- extract reads only its page ------------------------------------------
def _page_doc(pdf: Path, page_index):
    key = (f"local:{pdf}#page={page_index}" if page_index is not None
           else f"local:{pdf}")
    return Document(id="x", path=str(pdf), filename=pdf.name,
                    source_key=key, page_index=page_index)


def test_extract_reads_only_its_own_page(tmp_path: Path) -> None:
    p = tmp_path / "m.pdf"
    _pdf(p, ["ALPHA this is page one of the invoice",
             "BETA this is page two of the invoice"])
    src = PdfTextSource(DEFAULTS)
    t0, _ = src.extract(_page_doc(p, 0))
    t1, _ = src.extract(_page_doc(p, 1))
    whole, _ = src.extract(_page_doc(p, None))
    assert "ALPHA" in t0 and "BETA" not in t0
    assert "BETA" in t1 and "ALPHA" not in t1
    assert "ALPHA" in whole and "BETA" in whole            # legacy concatenation


def test_extract_flags_a_page_out_of_range(tmp_path: Path) -> None:
    p = tmp_path / "m.pdf"
    _pdf(p, ["page one of the document", "page two of the document"])
    d = _page_doc(p, 5)
    assert PdfTextSource(DEFAULTS).extract(d) == ("", "none")
    assert "page_out_of_range" in {f.code for f in d.flags}


# ---- id identity + duplicate detection ------------------------------------
def test_parse_suffixes_only_exploded_pages() -> None:
    stage = ParseStage()
    single = Document(id="a", path="/x/invoice_A-1.pdf", filename="invoice_A-1.pdf")
    stage.process(single)
    assert single.fields.invoice_id == "A-1"               # unchanged, canonical

    page2 = Document(id="b", path="/x/invoice_A-1.pdf",
                     filename="invoice_A-1.pdf", page_index=1)
    stage.process(page2)
    assert page2.fields.invoice_id == "A-1-p2"             # 0-based page → -p2


def _invoice_page(did: str, folder: str, page_index):
    doc = Document(id=did, path=f"/x/{folder}/f.pdf", filename="f.pdf",
                   doc_type=DocType.INVOICE, page_index=page_index)
    ParseStage().process(doc)
    return doc


def test_sibling_pages_are_not_flagged_duplicate() -> None:
    docs = [_invoice_page(f"d{k}", "a", k) for k in range(3)]
    result = RunResult(run_id="r", root="/x", documents=docs)
    _flag_duplicate_ids(result)
    assert all("duplicate_id" not in {f.code for f in d.flags} for d in docs)


def test_true_cross_folder_duplicate_still_flags() -> None:
    # same filename AND same page index in two folders = a real filing duplicate
    docs = [_invoice_page("d1", "a", 1), _invoice_page("d2", "b", 1)]
    result = RunResult(run_id="r", root="/x", documents=docs)
    _flag_duplicate_ids(result)
    assert all("duplicate_id" in {f.code for f in d.flags} for d in docs)


# ---- the deliverable copy is sliced ---------------------------------------
def test_write_page_pdf_writes_exactly_one_page(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    _pdf(src, ["p1 aaa content", "p2 bbb content", "p3 ccc content"])
    dest = tmp_path / "out.pdf"
    write_page_pdf(str(src), 1, dest)
    out = fitz.open(str(dest))
    try:
        assert out.page_count == 1
        assert "bbb" in out[0].get_text()
    finally:
        out.close()
