"""Continuing/resuming a run must never revert review edits — and, with them,
matches the reviewer already confirmed.

Pins the fix for the bug where continuing a finished run rebuilt `documents`
purely from `checkpoint.jsonl` (a pre-review ledger that nothing re-appends to
after a review edit), silently discarding everything a human had saved into
detections.json — including a `bind_row` promotion, which drops the doc back
out of `invoices()` and un-matches its B2B row.
"""
from __future__ import annotations

from pathlib import Path

from invoices.core.models import Document, DocType, RunResult
from invoices.detect import _assemble_documents, run_scan
from invoices.io.runstore import RunStore


def _doc(i: int) -> Document:
    return Document(id=f"d{i:03d}", path=f"/x/{i}.pdf", filename=f"{i}.pdf",
                    source_key=f"local:/x/{i}.pdf")


# ---- _assemble_documents, in isolation (no pipeline at all) ----------------
def test_assemble_documents_falls_back_to_ledger_with_no_prior_result(tmp_path: Path):
    store = RunStore.new(tmp_path / "out", run_id="t")
    store.append_checkpoint(_doc(0))
    store.append_checkpoint(_doc(1))
    docs = _assemble_documents(store)
    assert sorted(d.id for d in docs) == ["d000", "d001"]


def test_assemble_documents_keeps_reviewed_state_and_adds_new_docs(tmp_path: Path):
    store = RunStore.new(tmp_path / "out", run_id="t")
    for i in range(2):
        store.append_checkpoint(_doc(i))

    # A review edit: doc 0 promoted to an invoice and reviewed, saved the way a
    # real run finishes (detect.py's own store.save call).
    reviewed = _doc(0)
    reviewed.doc_type = DocType.INVOICE
    reviewed.reviewed = True
    result = RunResult(run_id=store.run_id, root="/x", documents=[reviewed, _doc(1)])
    store.save(result)

    # A continue: one more PDF arrives and gets checkpointed. Docs 0/1 are
    # skipped by the pipeline (already in checkpoint.jsonl), so their ledger
    # entries stay the stale, pre-review versions.
    store.append_checkpoint(_doc(2))

    docs = _assemble_documents(store)
    by_id = {d.id: d for d in docs}
    assert set(by_id) == {"d000", "d001", "d002"}
    assert by_id["d000"].doc_type == DocType.INVOICE   # preserved, not reverted
    assert by_id["d000"].reviewed is True


def test_assemble_documents_survives_a_corrupt_detections_file(tmp_path: Path):
    store = RunStore.new(tmp_path / "out", run_id="t")
    store.append_checkpoint(_doc(0))
    store.detections_path.write_text("not json")
    docs = _assemble_documents(store)
    assert [d.id for d in docs] == ["d000"]


# ---- end-to-end through run_scan --------------------------------------------
def test_continuing_a_run_preserves_a_promoted_documents_state(tmp_path: Path):
    out = tmp_path / "out"

    def walker_5(root, settings, control=None):
        for i in range(5):
            yield _doc(i)

    store1, result1 = run_scan("/nowhere", out, quiet=True, walker=walker_5,
                               root_key="k")

    # Simulate a human's bind_row promotion + review exactly like web/app.py's
    # `_persist` does: mutate the Document, save detections.json.
    target = next(d for d in result1.documents if d.id == "d000")
    target.doc_type = DocType.INVOICE
    target.reviewed = True
    store1.save(result1)

    def walker_8(root, settings, control=None):
        for i in range(8):          # docs 0-4 already checkpointed, 5-7 new
            yield _doc(i)

    store2, result2 = run_scan("/nowhere", out, quiet=True, walker=walker_8,
                               store=store1, root_key="k")

    assert {d.id for d in result2.documents} == {f"d{i:03d}" for i in range(8)}
    kept = next(d for d in result2.documents if d.id == "d000")
    assert kept.doc_type == DocType.INVOICE
    assert kept.reviewed is True
