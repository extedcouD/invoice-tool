"""Fast, OCR-free tests. Filenames in the sample tree are the ground truth."""
import glob
import json
import os

import pytest

from invoices.matching import vendor
from invoices.extractors import default_extract, money, norm_date, vendor_name
from invoices.stages.parse import _id_from_filename
from invoices.stages.classify import ClassifyStage
from invoices.stages.parse import ParseStage
from invoices.stages.walk import _parse_path
from invoices.core.models import Document, DocType


# --------------------------------------------------------------------------- #
# fuzzy vendor matching
# --------------------------------------------------------------------------- #
def test_vendor_normalize_strips_legal_and_parens():
    assert vendor.normalize("Sharma Mehta & Associates (Chartered Accountants)") == "sharma mehta"
    assert vendor.normalize("Apex Business Consultants Pvt Ltd") == "apex business consultants"


def test_vendor_score_high_for_suffix_variants():
    assert vendor.score("Apex Business Consultants Pvt Ltd", "Apex Business Consultants LLP") >= 90
    assert vendor.score("Lex & Partners Advocates LLP", "Lex and Partners Advocates") >= 90


def test_vendor_score_low_for_different_companies():
    assert vendor.score("Apex Business Consultants", "Urban Tiffin Foods") < 70


# --------------------------------------------------------------------------- #
# field parsing helpers
# --------------------------------------------------------------------------- #
def test_id_from_filename():
    assert _id_from_filename("Invoice_LEX-22-23-002.pdf") == "LEX-22-23-002"
    assert _id_from_filename("Invoice_BYTE-22-23-009.pdf") == "BYTE-22-23-009"


def test_money_and_date():
    assert money("235,842.93") == 235842.93
    assert money("1,23,456.78") == 123456.78  # Indian lakh grouping
    assert money(None) is None
    assert norm_date("15-Mar-2023") == "2023-03-15"
    assert norm_date("garbage") is None


def test_vendor_name_after_tax_invoice():
    text = "TAX INVOICE\nLex & Partners Advocates LLP\nInvoice No.: LEX/22-23/002"
    assert vendor_name(text) == "Lex & Partners Advocates LLP"


def test_taxable_ignores_table_header_rownum():
    # "Taxable Value (Rs.)" column header is followed by row number "1"; the real
    # value is the later summary line "Taxable Value\nRs. 199,866.89".
    text = ("Taxable Value (Rs.)\n1\nservice\n998231\n1\n100.00\n"
            "Taxable Value\nRs. 199,866.89\nTotal Invoice Value\nRs. 235,842.93")
    d = _doc(text, "Invoice_X.pdf")
    default_extract(text, d)
    assert d.fields.taxable_value == 199866.89
    assert d.fields.total_value == 235842.93


def test_billto_skips_colon_line():
    text = "Bill To:\nNimbus Retail Solutions Pvt Ltd\nGSTIN: 07AAFCN4321P1Z5"
    d = _doc(text, "Invoice_X.pdf")
    default_extract(text, d)
    assert d.fields.bill_to_name == "Nimbus Retail Solutions Pvt Ltd"


# --------------------------------------------------------------------------- #
# per-vendor extractor recovers a non-standard layout the baseline misses
# --------------------------------------------------------------------------- #
ZEPHYR_TXT = (
    "BILL / TAX INVOICE\nZephyr Logistics Pvt Ltd\n"
    "Bill No: ZEPH-22-23-001\nGST Registration No: 07ZEPHY1234K1Z5\n"
    "Dated: 12/08/2022\nTaxable: INR 1,04,489.00\nAmount Payable: INR 1,23,456.78"
)


def test_baseline_misses_zephyr_layout():
    d = _doc(ZEPHYR_TXT, "Invoice_ZEPH.pdf")
    default_extract(ZEPHYR_TXT, d)
    # baseline finds vendor+gstin but not the oddly-labelled money/date/no
    assert d.fields.vendor_gstin == "07ZEPHY1234K1Z5"
    assert d.fields.total_value is None
    assert d.fields.invoice_date is None
    assert d.fields.invoice_no_content is None


def test_vendor_extractor_recovers_zephyr():
    d = _doc(ZEPHYR_TXT, "Invoice_ZEPH.pdf")
    d.doc_type = DocType.INVOICE
    ParseStage().process(d)
    assert d.fields.invoice_no_content == "ZEPH-22-23-001"
    assert d.fields.invoice_date == "2022-08-12"
    assert d.fields.taxable_value == 104489.00
    assert d.fields.total_value == 123456.78
    assert any(e.action == "vendor_extractors" for e in d.events)


# --------------------------------------------------------------------------- #
# classification on synthetic text (no OCR needed)
# --------------------------------------------------------------------------- #
INVOICE_TXT = ("TAX INVOICE\nAcme Corp Pvt Ltd\nInvoice No.: ACME/22-23/001\n"
               "GSTIN: 07AAJFL5629H1ZQ\nPlace of Supply: 07-Delhi\n"
               "Total Invoice Value Rs. 1,000.00")
APPROVAL_TXT = ("Nimbus Retail\nPURCHASE / EXPENSE APPROVAL FORM\n"
                "Approval Decision\nStatus: Approved\nReference Invoice No.: ACME/22-23/001")


def _doc(text, filename):
    d = Document(id="d0", path="/x.pdf", filename=filename)
    d.text = text
    return d


def test_classify_invoice_vs_approval():
    st = ClassifyStage()
    assert st.process(_doc(INVOICE_TXT, "Invoice_ACME.pdf")).doc_type == DocType.INVOICE
    assert st.process(_doc(APPROVAL_TXT, "Approval_ACME.pdf")).doc_type == DocType.APPROVAL


def test_classify_ignores_misleading_filename():
    # filename says invoice, content is clearly an approval -> approval + flag
    d = ClassifyStage().process(_doc(APPROVAL_TXT, "Invoice_WRONG.pdf"))
    assert d.doc_type == DocType.APPROVAL
    assert any(f.code == "filename_content_mismatch" for f in d.flags)


# --------------------------------------------------------------------------- #
# tolerant path parsing
# --------------------------------------------------------------------------- #
def test_parse_path_labels_components():
    parts = ["FY 22-23", "Payments", "Kotak", "Sep-2022", "24-Sep-2022",
             "Apex Business Consultants Pvt Ltd"]
    info = _parse_path(parts)
    assert info.fy == "FY 22-23"
    assert info.month == "Sep-2022"
    assert info.date_folder == "24-Sep-2022"
    assert info.company == "Apex Business Consultants Pvt Ltd"


# --------------------------------------------------------------------------- #
# golden-set regression (uses the most recent scan if one exists)
# --------------------------------------------------------------------------- #
def _latest_detections():
    runs = sorted(glob.glob("out/run_*/detections.json"))
    return runs[-1] if runs else None


def _dirt_paths():
    """Planted dirt intentionally violates the filename convention, so exclude it
    from the filename-based ground-truth metric (see tests/test_dirt.py)."""
    m = "Testing Environment/_dirt_manifest.json"
    if not os.path.exists(m):
        return set()
    return {e["path"] for e in json.load(open(m)) if e.get("path")}


@pytest.mark.skipif(_latest_detections() is None, reason="no scan run present")
def test_golden_classification_precision_recall():
    dirt = _dirt_paths()
    docs = [d for d in json.load(open(_latest_detections()))["documents"]
            if d["path"] not in dirt]

    def truth(d):
        n = d["filename"].lower()
        return "invoice" if n.startswith("invoice") else "approval" if n.startswith("approval") else "other"

    tp = sum(1 for d in docs if truth(d) == "invoice" and d["doc_type"] == "invoice")
    fp = sum(1 for d in docs if truth(d) != "invoice" and d["doc_type"] == "invoice")
    fn = sum(1 for d in docs if truth(d) == "invoice" and d["doc_type"] != "invoice")
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    assert precision >= 0.98, f"precision {precision}"
    assert recall >= 0.98, f"recall {recall}"


@pytest.mark.skipif(_latest_detections() is None, reason="no scan run present")
def test_golden_text_invoices_fully_parsed():
    dirt = _dirt_paths()
    docs = [d for d in json.load(open(_latest_detections()))["documents"]
            if d["path"] not in dirt]
    text_inv = [d for d in docs if d["doc_type"] == "invoice" and d["source"] == "text"]
    # digitally-generated invoices should yield every core field
    for d in text_inv:
        f = d["fields"]
        assert f["invoice_id"] and f["vendor_gstin"] and f["total_value"] is not None, d["filename"]
