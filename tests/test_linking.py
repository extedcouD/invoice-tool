"""GSTR-2A linking: the pure matcher, the manual-bind override, and the
correct-and-rematch loop the review UI is built on.

These need no scan and no fixtures on disk — `match()` is a pure function over
(B2B rows, Documents), which is exactly why linking was split out of the workbook
writer: the review UI re-runs it after every single edit.
"""
from __future__ import annotations

import pytest

from invoices.core.models import Document, DocType, Fields, LinkStatus, RunResult
from invoices.io.gstr import (AMBIGUOUS, B2BRow, MATCHED, NOT_FOUND, SKIPPED,
                              apply_plan, match, suggest)


def inv(doc_id: str, number: str | None, gstin: str | None,
        confidence: float = 0.9, content: str | None = None) -> Document:
    return Document(
        id=doc_id, path=f"/x/{doc_id}.pdf", filename=f"{doc_id}.pdf",
        doc_type=DocType.INVOICE, confidence=confidence,
        fields=Fields(invoice_id=number, invoice_no_content=content,
                      vendor_gstin=gstin),
    )


def row(n: int, number: str, gstin: str) -> B2BRow:
    return B2BRow(row=n, gstin=gstin, invoice_no=number)


G1 = "36AAECA4456F1Z1"
G2 = "29AACCB8890F1ZX"


def test_matched_not_found_and_unreferenced():
    rows = [row(2, "APEX/22-23/003", G1), row(3, "GONE/22-23/009", G2)]
    docs = [inv("d1", "APEX/22-23/003", G1), inv("d2", "SPARE/22-23/001", G2)]

    plan = match(rows, docs)
    by_row = {r.row: r for r in plan.rows}

    assert by_row[2].status == MATCHED and by_row[2].doc_id == "d1"
    # the row whose PDF was never filed — the thing the client actually asks about
    assert by_row[3].status == NOT_FOUND and by_row[3].doc_id is None
    # the reverse gap: a detected invoice that satisfies no row
    assert plan.unreferenced == ["d2"]

    c = plan.counts()
    assert (c["matched"], c["not_found"], c["unreferenced"]) == (1, 1, 1)


def test_invoice_number_normalization_bridges_formats():
    """'APEX/22-23/003' in the return vs 'APEX-22-23-003' on disk is the same invoice."""
    plan = match([row(2, "APEX/22-23/003", G1)], [inv("d1", "APEX-22-23-003", G1)])
    assert plan.rows[0].status == MATCHED


def test_content_number_rescues_a_junk_filename():
    """A junk-named PDF still matches on the number printed inside it."""
    plan = match([row(2, "APEX/22-23/003", G1)],
                 [inv("d1", "scan0001", G1, content="APEX/22-23/003")])
    assert plan.rows[0].status == MATCHED


def test_conflicting_gstins_are_ambiguous_not_guessed():
    """Same invoice number under two different suppliers — a human must choose."""
    docs = [inv("d1", "INV/001", G1), inv("d2", "INV/001", G2)]
    plan = match([row(2, "INV/001", "07AAOFS7712K1Z1")], docs)

    r = plan.rows[0]
    assert r.status == AMBIGUOUS
    assert sorted(r.candidates) == ["d1", "d2"]
    assert r.doc_id is None       # never silently pick one


def test_duplicate_filing_picks_the_best_copy():
    """The same invoice filed in two folders is a duplicate, not a conflict."""
    docs = [inv("d1", "INV/001", G1, confidence=0.6),
            inv("d2", "INV/001", G1, confidence=0.95)]
    plan = match([row(2, "INV/001", G1)], docs)

    assert plan.rows[0].status == MATCHED
    assert plan.rows[0].doc_id == "d2"        # highest confidence wins
    assert plan.duplicate_filings == 1


def test_manual_binding_overrides_not_found():
    """A row the matcher gave up on, bound by hand in review, becomes matched."""
    rows = [row(2, "APEX/22-23/003", G1)]
    docs = [inv("d1", "totally-different", G2)]

    assert match(rows, docs).rows[0].status == NOT_FOUND

    bound = match(rows, docs, manual={2: "d1"})
    assert bound.rows[0].status == MATCHED
    assert bound.rows[0].doc_id == "d1"
    assert bound.rows[0].manual is True
    assert bound.unreferenced == []           # d1 is now spoken for


def test_correcting_a_document_rematches_its_row():
    """The review loop: fix the misread number, and the row links on the next match.

    This is the whole point of splitting `match` out of the workbook writer — the
    UI can re-run it after every edit and tell the reviewer 'row 2 now matches'.
    """
    rows = [row(2, "APEX/22-23/003", G1)]
    doc = inv("d1", "APEX/22-23/8O3", G1)      # OCR read 0 as O

    before = match(rows, [doc])
    assert before.rows[0].status == NOT_FOUND
    assert before.unreferenced == ["d1"]

    doc.fields.invoice_id = "APEX/22-23/003"   # the human correction
    after = match(rows, [doc])

    assert after.rows[0].status == MATCHED
    assert after.rows[0].doc_id == "d1"
    assert after.unreferenced == []


def test_apply_plan_stamps_the_result_onto_documents():
    """The old code computed this mapping and threw it away as a count."""
    matched = inv("d1", "APEX/22-23/003", G1)
    orphan = inv("d2", "SPARE/22-23/001", G2)
    result = RunResult(run_id="r", root="/x", documents=[matched, orphan])

    apply_plan(result, match([row(2, "APEX/22-23/003", G1)], result.invoices()))

    assert matched.link_status == LinkStatus.MATCHED
    assert matched.gstr_row == 2
    assert matched.gstr_ref == "36AAECA4456F1Z1__APEX-22-23-003.pdf"
    assert orphan.link_status == LinkStatus.UNREFERENCED
    assert orphan.gstr_row is None
    # linking is now in the per-document trace, so a reviewer can see *why*
    assert any(e.stage == "link" for e in matched.events)


def test_apply_plan_is_idempotent_across_rematches():
    """Re-matching after an edit must not leave stale link state behind."""
    d = inv("d1", "APEX/22-23/003", G1)
    result = RunResult(run_id="r", root="/x", documents=[d])
    rows = [row(2, "APEX/22-23/003", G1)]

    apply_plan(result, match(rows, result.invoices()))
    assert d.link_status == LinkStatus.MATCHED

    d.fields.invoice_id = "broken"             # the reviewer breaks it again
    d.fields.invoice_no_content = None
    apply_plan(result, match(rows, result.invoices()))

    assert d.link_status == LinkStatus.UNREFERENCED
    assert d.gstr_row is None and d.gstr_ref is None


def test_suggest_ranks_the_near_miss_first():
    """Candidates for an unmatched row: the near-miss under the right GSTIN wins."""
    docs = [
        inv("far", "ZZZZ/99-99/999", G2),
        inv("near", "APEX/22-23/8O3", G1),     # one character off, right supplier
        inv("samenum-wrongvendor", "APEX/22-23/003", G2),
    ]
    ranked = suggest(row(2, "APEX/22-23/003", G1), docs, limit=3)

    assert ranked[0][0].id == "near"
    assert ranked[0][1] > ranked[-1][1]
    # scores must actually discriminate — a flat GSTIN bonus once pinned
    # every same-vendor invoice at 100%
    assert len({round(s) for _, s in ranked}) > 1
    assert all(s <= 100 for _, s in ranked)


def test_rejected_document_leaves_the_match():
    """'Not an invoice' in review drops it from the corpus and frees its row."""
    d = inv("d1", "APEX/22-23/003", G1)
    result = RunResult(run_id="r", root="/x", documents=[d])
    rows = [row(2, "APEX/22-23/003", G1)]

    assert match(rows, result.invoices()).rows[0].status == MATCHED

    d.doc_type = DocType.OTHER                 # the reviewer rejects it
    plan = match(rows, result.invoices())      # invoices() now excludes it
    assert plan.rows[0].status == NOT_FOUND


def test_blank_rows_are_skipped_not_counted():
    plan = match([row(2, "", ""), row(3, "APEX/1", G1)], [inv("d1", "APEX/1", G1)])
    # a row with no number can never resolve, but it is still reported, not dropped
    assert plan.rows[0].status == NOT_FOUND
    assert plan.rows[1].status == MATCHED


# ---- skipping a whole supplier --------------------------------------------
def test_skip_supplier_marks_only_its_unmatched_rows():
    """The reviewer skips an entity — its unmatched rows leave the queue; a matched
    row (even of that supplier) is untouched, and a match still wins over a skip."""
    rows = [
        B2BRow(row=2, gstin=G1, invoice_no="APEX/1", supplier="Apex Traders"),
        B2BRow(row=3, gstin=G2, invoice_no="ZED/9", supplier="Zephyr Ltd"),
    ]
    docs = [inv("d1", "APEX/1", G1)]                 # only Apex is filed
    plan = match(rows, docs, skipped=["Zephyr Ltd"])
    by_row = {r.row: r for r in plan.rows}

    assert by_row[2].status == MATCHED               # matched row untouched
    assert by_row[3].status == SKIPPED               # unmatched row of a skipped supplier
    assert "ZED/9" not in {r.invoice_no for r in plan.unresolved_rows()}
    assert plan.counts()["skipped"] == 1


def test_skip_does_not_override_a_match_for_that_supplier():
    rows = [B2BRow(row=2, gstin=G1, invoice_no="APEX/1", supplier="Apex Traders")]
    plan = match(rows, [inv("d1", "APEX/1", G1)], skipped=["Apex Traders"])
    assert plan.rows[0].status == MATCHED


def test_skip_supplier_matches_across_case_and_spacing():
    rows = [B2BRow(row=2, gstin=G2, invoice_no="ZED/9", supplier="Zephyr   Ltd")]
    plan = match(rows, [], skipped=["zephyr ltd"])   # different case + spacing
    assert plan.rows[0].status == SKIPPED


# ---- rerun: an already-filled link column ---------------------------------
def test_existing_ref_is_carried_onto_the_rowmatch():
    """A pre-filled 'Invoice Ref' value round-trips onto the RowMatch so the writer
    can preserve it and the UI can show it."""
    rows = [B2BRow(row=2, gstin=G1, invoice_no="X/1",
                   supplier="Acme", existing_ref="prior.pdf")]
    plan = match(rows, [])
    assert plan.rows[0].existing_ref == "prior.pdf"
    assert plan.rows[0].status == NOT_FOUND          # still surfaced for review
