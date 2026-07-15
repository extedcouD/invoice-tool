"""Finding a PDF by where it was filed, rather than by the fields that failed.

`PathIndex` is pure over Documents — no scan, no fixtures on disk — which is what
lets the review UI run `search()` on every keystroke.
"""
from __future__ import annotations

from invoices.core.models import Document, DocType, Fields, PathInfo
from invoices.matching.paths import PathIndex, rel_parts

ROOT = "/data/Testing Environment"
G1 = "36AAECA4456F1Z1"


def doc(doc_id: str, rel: str, *, number: str | None = None,
        gstin: str | None = None, kind: DocType = DocType.INVOICE,
        vendor: str | None = None) -> Document:
    """A doc filed at ``rel`` under the scan root (last component = filename)."""
    parts = rel.split("/")
    company = parts[0] if len(parts) > 1 else None
    return Document(
        id=doc_id, path=f"{ROOT}/{rel}", filename=parts[-1],
        doc_type=kind, confidence=0.9,
        path_info=PathInfo(company=company,
                           month=next((p for p in parts if "-20" in p), None)),
        fields=Fields(invoice_id=number, vendor_gstin=gstin, vendor_name_pdf=vendor),
    )


TREE = [
    doc("d1", "Apex Business Consultants/2022-23/Aug-2022/15-08-2022/APEX-22-23-003.pdf",
        number="APEX/22-23/003", gstin=G1, vendor="Apex Business Consultants"),
    doc("d2", "Apex Business Consultants/2022-23/Aug-2022/15-08-2022/APEX-22-23-004.pdf",
        number="APEX/22-23/004", gstin=G1),
    doc("d3", "Apex Business Consultants/2022-23/Sep-2022/02-09-2022/APEX-22-23-011.pdf",
        number="APEX/22-23/011", gstin=G1),
    doc("d4", "Zephyr Logistics/2022-23/Aug-2022/15-08-2022/ZEP-8891.pdf",
        number="ZEP-8891"),
    # filed right next to d1, but the classifier called it an approval — the exact
    # case that puts a B2B row in the "no PDF" queue.
    doc("d5", "Apex Business Consultants/2022-23/Aug-2022/15-08-2022/approval-note.pdf",
        kind=DocType.APPROVAL),
]


def index() -> PathIndex:
    return PathIndex(TREE, ROOT)


# ---- relative paths ------------------------------------------------------
def test_rel_parts_relative_to_root():
    assert rel_parts(TREE[3], ROOT) == [
        "Zephyr Logistics", "2022-23", "Aug-2022", "15-08-2022", "ZEP-8891.pdf"]


def test_rel_parts_survives_a_path_outside_the_root():
    """A run reopened after its tree moved must degrade, not explode."""
    d = doc("z", "Apex/x.pdf")
    assert rel_parts(d, "/somewhere/else")[-1] == "x.pdf"


# ---- search --------------------------------------------------------------
def test_search_by_folder_path():
    hits = index().search("apex aug 003")
    assert hits[0].doc.id == "d1"
    assert hits[0].folder.endswith("Aug-2022/15-08-2022")


def test_search_tolerates_typos():
    """The reviewer is recalling a folder name, not copying it."""
    assert index().search("apx busines")[0].doc.path_info.company == \
        "Apex Business Consultants"


def test_search_finds_pdfs_that_are_not_detected_invoices():
    """The answer to an unmatched row is often a PDF the classifier misread."""
    hits = index().search("approval note")
    assert hits and hits[0].doc.id == "d5"
    assert hits[0].is_invoice is False        # the UI warns before binding it

    assert not any(h.doc.id == "d5"
                   for h in index().search("approval note", invoices_only=True))


def test_search_still_finds_by_field():
    """Path search must not be a downgrade for a reviewer who knows the number."""
    assert index().search(G1)[0].doc.fields.vendor_gstin == G1
    assert index().search("ZEP-8891")[0].doc.id == "d4"


def test_search_ranks_a_literal_fragment_over_a_lookalike():
    hits = index().search("APEX-22-23-011")
    assert hits[0].doc.id == "d3"             # not the near-identical d1/d2


def test_search_is_empty_for_an_empty_query():
    assert index().search("   ") == []


# ---- browse --------------------------------------------------------------
def test_browse_root_lists_company_folders_with_counts():
    b = index().browse()
    assert [f["name"] for f in b["folders"]] == \
        ["Apex Business Consultants", "Zephyr Logistics"]
    apex = b["folders"][0]
    assert apex["pdfs"] == 4 and apex["invoices"] == 3   # d5 is an approval
    assert b["files"] == []                              # nothing filed at the root


def test_browse_descends_to_the_files():
    b = index().browse(["Apex Business Consultants", "2022-23", "Aug-2022", "15-08-2022"])
    assert [h.doc.id for h in b["files"]] == ["d1", "d2", "d5"]
    assert b["folders"] == []
    assert [c["name"] for c in b["crumbs"]][0] == "Apex Business Consultants"
    assert b["crumbs"][-1]["path"][-1] == "15-08-2022"


def test_browse_an_unknown_folder_is_empty_not_an_error():
    b = index().browse(["Nope"])
    assert b["folders"] == [] and b["files"] == []


# ---- folder auto-suggestion ---------------------------------------------
def test_suggest_folders_from_the_supplier_name():
    """A B2B row names its supplier; the tree is filed by company — so browsing
    should start at the right folder, not at the root."""
    hints = index().suggest_folders("Apex Business Consultants Pvt Ltd")
    assert hints and hints[0]["name"] == "Apex Business Consultants"
    assert hints[0]["path"] == ["Apex Business Consultants"]


def test_suggest_folders_declines_when_nothing_resembles_the_supplier():
    assert index().suggest_folders("Completely Different Traders") == []
    assert index().suggest_folders(None) == []
