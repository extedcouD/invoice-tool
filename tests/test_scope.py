"""One financial year per run: the FY scope filter.

Pure — no scan, no OCR, no network, no Drive auth. The client's GSTR-2A returns
arrive one workbook per financial year, so a run is scoped to one FY folder and the
other years' subtrees are never descended.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from invoices.config import DEFAULTS
from invoices.io.drive import DriveClient, FOLDER_MIME, PDF_MIME, walk_drive
from invoices.io.runstore import RunStore
from invoices.stages import walk as walkmod
from invoices.stages.walk import keep_fy_dir, list_fy_folders, norm_fy, walk

YEARS = ["FY 22-23", "FY 23-24"]


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A two-year skeleton. walk() only stat()s the files, so they can be empty."""
    for fy in YEARS:
        d = (tmp_path / fy / "Payments" / "Kotak" / "Sep-2022" / "24-Sep-2022"
             / "Apex Business Consultants Pvt Ltd")
        d.mkdir(parents=True)
        (d / f"invoice_{fy.replace(' ', '')}_001.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "KYC").mkdir()          # not an FY folder — must be ignored
    return tmp_path


def _walk(root: Path, fy: str | None):
    return list(walk(root, replace(DEFAULTS, fy_scope=fy)))


# ---- the scope filter ------------------------------------------------------
def test_scoped_walk_yields_only_the_chosen_year(tree: Path) -> None:
    docs = _walk(tree, "FY 22-23")
    assert docs, "expected the scoped year's PDFs"
    assert {d.path_info.fy for d in docs} == {"FY 22-23"}


def test_unscoped_walk_still_yields_every_year(tree: Path) -> None:
    """The year is optional — no year means the old whole-tree behaviour."""
    assert {d.path_info.fy for d in _walk(tree, None)} == set(YEARS)


def test_scoped_walk_keeps_the_fy_and_flags_nothing(tree: Path) -> None:
    """The regression this design exists to prevent.

    Re-rooting at the FY folder (the tempting shortcut) drops the FY component from
    the walk-relative path, so _parse_path sets no info.fy and *every* doc comes out
    flagged `path_incomplete` — a 100%-flagged review queue and a blank `fy` column.
    Pruning keeps the root at the tree root, so the FY survives.
    """
    for d in _walk(tree, "FY 22-23"):
        assert d.path_info.fy == "FY 22-23"
        assert "path_incomplete" not in {f.code for f in d.flags}


def test_other_years_are_never_descended(tree: Path, monkeypatch) -> None:
    """Pins *prune*, not *filter*: the point is to not walk the other years at all."""
    seen: list[str] = []
    real = walkmod.os.walk

    def spy(top, *a, **kw):
        for dirpath, dirnames, filenames in real(top, *a, **kw):
            seen.append(str(dirpath))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(walkmod.os, "walk", spy)
    _walk(tree, "FY 22-23")
    assert not [p for p in seen if "FY 23-24" in p]


def test_fy_name_spelling_is_tolerated(tree: Path) -> None:
    """RE_FY accepts 'FY22-23' and 'fy 22 - 23'. An exact == would prune the whole
    tree for a user who typed one of those — a clean, successful, zero-invoice run,
    which is indistinguishable from a broken tool."""
    for spelling in ("fy22-23", "FY 22 - 23", "FY22-23"):
        docs = _walk(tree, spelling)
        assert {d.path_info.fy for d in docs} == {"FY 22-23"}, spelling


def test_keep_fy_dir_leaves_non_fy_folders_alone() -> None:
    assert keep_fy_dir("Payments", "FY 22-23")     # not an FY folder -> descend
    assert keep_fy_dir("FY 22-23", None)           # no scope -> descend everything
    assert not keep_fy_dir("FY 23-24", "FY 22-23")
    assert norm_fy(" fy 22 - 23 ") == norm_fy("FY22-23")


def test_list_fy_folders_lists_the_years_and_ignores_others(tree: Path) -> None:
    assert list_fy_folders(tree) == YEARS          # sorted; "KYC" excluded
    assert list_fy_folders(tree / "nope") == []    # unreadable -> no crash


# ---- resume identity is (root, fy) ----------------------------------------
def _incomplete_run(out: Path, run_id: str, meta: dict) -> None:
    store = RunStore.new(out, run_id=run_id)
    store.checkpoint_path.write_text("")
    store.update_meta(**meta)


def test_a_year_scoped_run_never_resumes_another_years_checkpoint(tmp_path: Path) -> None:
    """find_resumable keys on (root, fy). On root alone, starting FY 23-24 would
    reopen FY 22-23's checkpoint and detections.json would span two years."""
    out = tmp_path / "out"
    _incomplete_run(out, "20260101-000000", {"root": "k", "fy": "FY 22-23", "complete": False})

    assert RunStore.find_resumable(out, "k", fy="FY 22-23") is not None
    assert RunStore.find_resumable(out, "k", fy="FY 23-24") is None
    assert RunStore.find_resumable(out, "k") is None          # all-years != one year


def test_a_legacy_run_without_an_fy_key_still_resumes(tmp_path: Path) -> None:
    """Runs written before the key existed have no "fy" -> None -> unscoped."""
    out = tmp_path / "out"
    _incomplete_run(out, "20260101-000000", {"root": "k", "complete": False})

    assert RunStore.find_resumable(out, "k") is not None
    assert RunStore.find_resumable(out, "k", fy="FY 22-23") is None


def test_the_run_dir_carries_the_year(tmp_path: Path) -> None:
    """So several per-year outputs are tellable apart on disk, timestamp still first
    (a lexical sort of run_* must stay chronological)."""
    store = RunStore.new(tmp_path, run_id="20260101-000000", label="FY 22-23")
    assert store.dir.name == "run_20260101-000000_FY-22-23"
    assert store.master_path().name == "master_20260101-000000_FY-22-23.xlsx"


# ---- Drive: the prune must skip the LISTING, not just the download ---------
class FakeDrive(DriveClient):
    """No network, no auth: _svc is a lazy property and list_children is the only
    thing walk_pdf_tree touches."""

    def __init__(self, tree: dict) -> None:
        super().__init__(None)
        self.tree, self.listed = tree, []

    def list_children(self, folder_id: str) -> list[dict]:
        self.listed.append(folder_id)
        return self.tree.get(folder_id, [])


def _folder(fid: str, name: str) -> dict:
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME}


def _drive_tree() -> dict:
    """root -> FY 22-23 / FY 23-24 -> Payments -> Kotak -> Sep-2022 -> 24-Sep-2022 -> Co -> pdf"""
    tree: dict = {"root": [_folder("fy2223", "FY 22-23"), _folder("fy2324", "FY 23-24")]}
    for fid, fy in (("fy2223", "FY 22-23"), ("fy2324", "FY 23-24")):
        chain = [(f"{fid}-pay", "Payments"), (f"{fid}-kotak", "Kotak"),
                 (f"{fid}-mon", "Sep-2022"), (f"{fid}-day", "24-Sep-2022"),
                 (f"{fid}-co", "Apex Business Consultants Pvt Ltd")]
        parent = fid
        for cid, name in chain:
            tree[parent] = [_folder(cid, name)]
            parent = cid
        tree[parent] = [{"id": f"{fid}-pdf", "name": f"invoice_{fy}.pdf",
                         "mimeType": PDF_MIME, "size": "10", "modifiedTime": "2026-01-01"}]
    return tree


def test_walk_drive_prunes_the_other_years_listings() -> None:
    fake = FakeDrive(_drive_tree())
    docs = list(walk_drive("root", replace(DEFAULTS, fy_scope="FY 22-23"), client=fake))

    assert {d.path_info.fy for d in docs} == {"FY 22-23"}
    assert all("path_incomplete" not in {f.code for f in d.flags} for d in docs)
    # The listing is the network cost on Drive, and the scope gate in walk_drive
    # runs *after* it — so asserting only on the yielded docs would not prove the
    # other year's subtree was skipped rather than merely filtered out.
    assert not [f for f in fake.listed if f.startswith("fy2324")]


def test_walk_drive_unscoped_still_walks_every_year() -> None:
    fake = FakeDrive(_drive_tree())
    docs = list(walk_drive("root", DEFAULTS, client=fake))
    assert {d.path_info.fy for d in docs} == set(YEARS)
