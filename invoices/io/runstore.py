"""Persistence for a run. The run directory is the single source of truth, so
runs are resumable and `export`/`review` operate without re-scanning.

Layout:
  out/run_<ts>/
    detections.json   # full RunResult (documents + events + metrics), at end
    checkpoint.jsonl  # one finished Document per line, appended live (resume ledger)
    run_meta.json     # {root, fy, complete} — lets an interrupted run be found + resumed
    status.json       # live progress snapshot (written during scan)
    review_pdfs/      # copies of flagged PDFs for the review UI
    master_<ts>.xlsx  # exported workbook

Resumability: each document is appended to ``checkpoint.jsonl`` the moment it
finishes, so an interrupted scan keeps its work. A resumed run skips every source
already in the checkpoint (keyed by :attr:`Document.source_key`) and the final
``detections.json`` is assembled from the ledger. This matters for very large
(e.g. 120 GB) trees where a run spans hours and may be interrupted.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..core.models import Document, RunResult


class RunStore:
    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "review_pdfs").mkdir(exist_ok=True)

    # ---- factory -----------------------------------------------------------
    @classmethod
    def new(cls, out_root: Path, run_id: str | None = None,
            label: str | None = None) -> "RunStore":
        """``label`` (the scanned FY, when the run is scoped to one year) is appended
        to the run id, so the several per-year runs over one tree are tellable apart
        on disk. The timestamp still leads, so a lexical sort of ``run_*`` is still
        chronological — which ``_latest_run`` and ``find_resumable`` both rely on.
        """
        run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        if label:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")   # FY 22-23 -> FY-22-23
            if slug:
                run_id = f"{run_id}_{slug}"
        return cls(Path(out_root) / f"run_{run_id}")

    @property
    def run_id(self) -> str:
        return self.dir.name.replace("run_", "")

    # ---- paths -------------------------------------------------------------
    @property
    def detections_path(self) -> Path:
        return self.dir / "detections.json"

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.dir / "checkpoint.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.dir / "run_meta.json"

    @property
    def review_dir(self) -> Path:
        return self.dir / "review_pdfs"

    @property
    def link_path(self) -> Path:
        """The GSTR-2A match plan — row->invoice, and the gaps on both sides."""
        return self.dir / "link.json"

    @property
    def linked_dir(self) -> Path:
        """Flat folder holding the invoices referenced by a linked GST return."""
        return self.dir / "linked_invoices"

    def master_path(self) -> Path:
        return self.dir / f"master_{self.run_id}.xlsx"

    def gstr_linked_path(self, template_path: Path | str) -> Path:
        """Output workbook for `link-gstr`: '<template-stem>_linked.xlsx' in the run dir."""
        return self.dir / f"{Path(template_path).stem}_linked.xlsx"

    # ---- (de)serialize -----------------------------------------------------
    def save(self, result: RunResult) -> Path:
        self.detections_path.write_text(result.model_dump_json(indent=2))
        return self.detections_path

    def load(self) -> RunResult:
        data = json.loads(self.detections_path.read_text())
        return RunResult.model_validate(data)

    # ---- GSTR-2A link plan -------------------------------------------------
    def save_link(self, plan) -> Path:
        self.link_path.write_text(json.dumps(plan.to_dict(), indent=2))
        return self.link_path

    def load_link(self):
        """The saved match plan, or None. Import is local to avoid a cycle
        (io.gstr imports RunStore)."""
        from .gstr import LinkPlan

        try:
            return LinkPlan.from_dict(json.loads(self.link_path.read_text()))
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            return None

    # ---- resumable checkpoint ---------------------------------------------
    def append_checkpoint(self, doc: Document) -> None:
        """Append one finished document to the resume ledger (single JSON line).

        Called on the consuming thread as each doc completes, so appends are
        serialized without a lock.
        """
        with self.checkpoint_path.open("a", encoding="utf-8") as fh:
            fh.write(doc.model_dump_json() + "\n")

    def iter_checkpoint(self) -> Iterator[Document]:
        """Yield every document recorded so far. Tolerates a torn final line
        (a crash mid-append) by skipping records that don't parse."""
        if not self.checkpoint_path.exists():
            return
        with self.checkpoint_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Document.model_validate_json(line)
                except Exception:
                    # torn/partial trailing line from an interrupted write
                    continue

    def done_keys(self) -> set[str]:
        """Source keys already processed — the skip-list for a resumed run."""
        return {d.source_key for d in self.iter_checkpoint() if d.source_key}

    def checkpoint_docs(self) -> list[Document]:
        """All finished docs, de-duplicated by source_key (last write wins)."""
        by_key: dict[str, Document] = {}
        ordered: list[Document] = []
        for d in self.iter_checkpoint():
            key = d.source_key or d.path
            if key in by_key:
                ordered[ordered.index(by_key[key])] = d
            else:
                ordered.append(d)
            by_key[key] = d
        return ordered

    # ---- run metadata (for finding a resumable run) -----------------------
    def write_meta(self, root: str, complete: bool, **extra) -> None:
        """Merge, don't clobber: `run_scan` rewrites {root, complete} at the end of
        every run, and that must not wipe the gstr path or the reviewer's manual
        bindings stored alongside them."""
        self.update_meta(root=root, complete=complete, **extra)

    def update_meta(self, **fields) -> None:
        meta = self.read_meta()
        meta.update(fields)
        self.meta_path.write_text(json.dumps(meta, indent=2))

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    # ---- the GSTR-2A workbook + the reviewer's manual row bindings ---------
    # Both live in the meta so a run reopened later (`invoices review`) can still
    # link — previously `serve()` never restored the gstr path, so "Finish &
    # export" on a reopened run always refused to link.
    def gstr_path(self) -> Optional[Path]:
        p = self.read_meta().get("gstr_path")
        return Path(p) if p else None

    def manual_links(self) -> dict[int, str]:
        raw = self.read_meta().get("manual_links") or {}
        return {int(k): v for k, v in raw.items()}

    def set_manual_link(self, row: int, doc_id: Optional[str]) -> dict[int, str]:
        """Bind (or, with doc_id=None, unbind) a B2B row to an invoice."""
        links = self.manual_links()
        if doc_id:
            links[row] = doc_id
        else:
            links.pop(row, None)
        self.update_meta(manual_links={str(k): v for k, v in links.items()})
        return links

    # ---- suppliers the reviewer chose to skip -----------------------------
    # Stored raw (as shown on the sheet); matching normalizes both sides. Persisted
    # in the meta so a skip survives a reopened/continued run, exactly like the
    # manual row bindings above.
    def skipped_suppliers(self) -> set[str]:
        return set(self.read_meta().get("skipped_suppliers") or [])

    def set_skipped_supplier(self, name: str, on: bool = True) -> set[str]:
        """Skip (or, with on=False, un-skip) a supplier's unmatched rows."""
        names = self.skipped_suppliers()
        if on and name:
            names.add(name)
        else:
            names.discard(name)
        self.update_meta(skipped_suppliers=sorted(names))
        return names

    def fy(self) -> Optional[str]:
        """The financial year this run was scoped to, or None if it scanned all years."""
        return self.read_meta().get("fy")

    def describe(self) -> dict:
        """A self-contained summary of this run folder for the "continue" UI + CLI:
        the tree/year/GSTR it was scanned with, how many docs are already done, and
        whether the original PDF tree is still where it was.

        ``gstr`` prefers the path recorded in meta but falls back to the kept copy
        inside the run dir (``detect._keep_gstr_with_run``), so a *copied* run folder
        still finds its workbook. ``tree_exists`` is False when the PDF tree has moved:
        the skip-list keys on absolute ``source_key`` paths, so a moved tree would
        silently reprocess everything — the caller warns instead of pretending it
        appended.
        """
        meta = self.read_meta()
        root = meta.get("root")
        gstr = meta.get("gstr_path")
        if gstr and not Path(gstr).exists():
            local = self.dir / Path(gstr).name
            gstr = str(local) if local.exists() else None
        return {
            "dir": str(self.dir),
            "run_id": self.run_id,
            "root": root,
            "fy": meta.get("fy"),
            "gstr": gstr,
            "complete": bool(meta.get("complete")),
            "done": len(self.done_keys()),
            "tree_exists": bool(root) and Path(root).exists(),
        }

    @classmethod
    def find_resumable(cls, out_root: Path, root: str,
                       fy: str | None = None,
                       pages: bool | None = None) -> Optional["RunStore"]:
        """Newest incomplete run under ``out_root`` for the same input (has a
        checkpoint, meta.complete is False), or None.

        The input is **(root, fy, pages)**, not root alone. A year-scoped run and an
        all-years run over the same tree share a ``root``, so matching on root alone
        would let a fresh "FY 23-24" run reopen an interrupted "FY 22-23" checkpoint
        — and ``checkpoint_docs()`` would then assemble one detections.json spanning
        two years.

        The year is a *separate* meta key rather than a suffix on ``root``, because
        ``root`` is also what every path is made relative to (``io/excel._rel``,
        ``PathIndex``) and so has to stay a real path.

        ``pages`` (per-page explosion) is the same kind of corpus identity: a
        page-exploding run must never reopen a whole-file checkpoint, or the ledger
        would mix a stale whole-file doc with its N page docs. When ``pages`` is None
        the caller opts out of that filter (back-compat); when a bool, a run matches
        only if its recorded ``pages`` marker agrees (a pre-feature run with no marker
        reads as False, so it is refused by a page-exploding run).

        Runs written before the ``fy`` key existed have no "fy", so they read as None
        and still resume an unscoped run.
        """
        runs = sorted(Path(out_root).glob("run_*"))
        for run_dir in reversed(runs):
            try:
                meta = json.loads((run_dir / "run_meta.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("root") == root and meta.get("fy") == fy \
                    and not meta.get("complete") \
                    and (pages is None or bool(meta.get("pages")) == pages) \
                    and (run_dir / "checkpoint.jsonl").exists():
                return cls(run_dir)
        return None

    @classmethod
    def open_existing(cls, run_dir: Path | str) -> "RunStore":
        """Open a specific, already-written run directory — the "continue a saved
        run" entry point (the user points at a run folder to append newly-added PDFs).

        Unlike ``find_resumable`` this ignores ``complete``: continuing a *finished*
        run is the whole point, and passing the store explicitly to ``run_scan``
        bypasses the completeness gate. Validate up front so a wrong folder fails here
        with a clear message instead of as a mysterious empty scan.
        """
        d = Path(run_dir).expanduser()
        if not d.is_dir():
            raise ValueError(f"not a folder: {d}")
        if not (d / "run_meta.json").exists() or not (d / "checkpoint.jsonl").exists():
            raise ValueError(
                f"{d} is not a run folder (needs run_meta.json + checkpoint.jsonl)")
        return cls(d)
