"""Continue a saved run: RunStore.open_existing + describe.

Pure — no scan, no OCR, no network. These back both the `scan --continue-run`
CLI flag and the setup form's "Continue a saved run" source mode, so the append
path can reuse an existing run dir's (root, fy, gstr) without re-typing them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from invoices.core.models import Document
from invoices.io.runstore import RunStore


def _doc(i: int) -> Document:
    return Document(id=f"d{i:03d}", path=f"/tree/{i}.pdf", filename=f"{i}.pdf",
                    size_bytes=1, source_key=f"local:/tree/{i}.pdf")


def _make_run(out: Path, root: str, *, fy: str | None, complete: bool,
              docs: int = 2, gstr_path: str | None = None) -> RunStore:
    store = RunStore.new(out, run_id="t")
    for i in range(docs):
        store.append_checkpoint(_doc(i))
    store.write_meta(root=root, complete=complete, fy=fy)
    if gstr_path is not None:
        store.update_meta(gstr_path=gstr_path)
    return store


# ---- open_existing ---------------------------------------------------------
def test_open_existing_rejects_missing_dir(tmp_path: Path):
    with pytest.raises(ValueError):
        RunStore.open_existing(tmp_path / "nope")


def test_open_existing_rejects_non_run_folder(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(ValueError):
        RunStore.open_existing(d)          # no run_meta.json / checkpoint.jsonl


def test_open_existing_opens_a_real_run(tmp_path: Path):
    made = _make_run(tmp_path / "out", str(tmp_path), fy="FY 22-23", complete=True)
    reopened = RunStore.open_existing(made.dir)
    assert reopened.dir == made.dir


# ---- describe --------------------------------------------------------------
def test_describe_reports_root_fy_done_and_complete(tmp_path: Path):
    store = _make_run(tmp_path / "out", str(tmp_path), fy="FY 22-23",
                      complete=True, docs=3)
    d = store.describe()
    assert d["root"] == str(tmp_path)
    assert d["fy"] == "FY 22-23"
    assert d["done"] == 3            # one per unique source_key
    assert d["complete"] is True
    assert d["tree_exists"] is True  # root is a real dir


def test_describe_flags_a_moved_tree(tmp_path: Path):
    store = _make_run(tmp_path / "out", "/gone/tree", fy=None, complete=True)
    d = store.describe()
    assert d["fy"] is None
    assert d["tree_exists"] is False


def test_describe_gstr_none_when_no_workbook(tmp_path: Path):
    store = _make_run(tmp_path / "out", str(tmp_path), fy=None, complete=True)
    assert store.describe()["gstr"] is None


def test_describe_gstr_falls_back_to_the_kept_copy(tmp_path: Path):
    """A copied run folder loses the original absolute gstr path, but the kept copy
    (_keep_gstr_with_run) travels inside the run dir — describe must find it."""
    store = _make_run(tmp_path / "out", str(tmp_path), fy=None, complete=True,
                      gstr_path="/original/location/GSTR2A.xlsx")   # no longer on disk
    kept = store.dir / "GSTR2A.xlsx"
    kept.write_bytes(b"x")
    assert store.describe()["gstr"] == str(kept)
