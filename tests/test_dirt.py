"""Asserts the planted edge cases (scripts/introduce_dirt.py) are handled as
intended. Runs against the most recent full scan + the dirt manifest; skips
cleanly if either is absent (so CI without the sample data still passes).
"""
import glob
import json
import os

import pytest

MANIFEST = "Testing Environment/_dirt_manifest.json"


def _load():
    runs = sorted(glob.glob("out/run_*/detections.json"))
    if not runs or not os.path.exists(MANIFEST):
        return None, None
    docs = json.load(open(runs[-1]))["documents"]
    manifest = json.load(open(MANIFEST))
    return docs, manifest


DOCS, MANIFEST_ENTRIES = _load()
pytestmark = pytest.mark.skipif(DOCS is None, reason="no scan+dirt manifest present")


def _by_path():
    return {d["path"]: d for d in DOCS}


def _entry(label):
    return next(e for e in MANIFEST_ENTRIES if e["label"] == label)


def _doc(label):
    e = _entry(label)
    d = _by_path().get(e["path"])
    assert d is not None, f"{label}: planted doc not found in run (rescan needed?)"
    return d, e


def _flags(d):
    return [f["code"] for f in d["flags"]]


def test_misnamed_invoice_detected_by_content():
    d, _ = _doc("misnamed_invoice")
    assert d["doc_type"] == "invoice"          # no Invoice_ prefix, content classifier caught it
    assert "id_mismatch" in _flags(d)


def test_invoice_named_approval():
    d, _ = _doc("invoice_named_approval")
    assert d["doc_type"] == "invoice"          # content wins over misleading filename
    assert "filename_content_mismatch" in _flags(d)


def test_approval_named_invoice_not_counted():
    d, _ = _doc("approval_named_invoice")
    assert d["doc_type"] == "approval"         # must NOT be treated as an invoice
    assert "filename_content_mismatch" in _flags(d)


def test_nested_subfolder_flagged():
    d, _ = _doc("nested_subfolder")
    assert d["doc_type"] == "invoice"
    assert "company_mismatch" in _flags(d)


def test_misfiled_company_flagged():
    d, _ = _doc("misfiled_company")
    assert "company_mismatch" in _flags(d) or "vendor_folder_variant" in _flags(d)


def test_duplicate_id_detected():
    d, _ = _doc("duplicate_id")
    assert "duplicate_id" in _flags(d)         # collides with a clean-set invoice id


def test_vendor_extractor_recovers_non_standard_layout():
    d, e = _doc("vendor_extractor")
    assert d["doc_type"] == "invoice"
    assert d["fields"]["total_value"] == e["expect"]["total_value"]
    assert _flags(d) == []                      # fully recovered -> not flagged


def test_iso_date_folder_flagged():
    d, _ = _doc("iso_date_folder")
    assert "path_incomplete" in _flags(d)


def test_corrupt_pdf_isolated():
    d, _ = _doc("corrupt_pdf")
    assert d["error"]                           # fault isolated, run continued
    assert "stage_error" in _flags(d)


def test_empty_folder_produced_no_doc():
    assert not any("Ghost Traders" in d["path"] for d in DOCS)
