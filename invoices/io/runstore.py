"""Persistence for a run. The run directory is the single source of truth, so
runs are resumable and `export`/`review` operate without re-scanning.

Layout:
  out/run_<ts>/
    detections.json   # full RunResult (documents + events + metrics), at end
    checkpoint.jsonl  # one finished Document per line, appended live (resume ledger)
    run_meta.json     # {root, complete} — lets an interrupted run be found + resumed
    status.json       # live progress snapshot (written during scan)
    review_pdfs/      # copies of flagged PDFs for the review UI
    master_<ts>.xlsx  # exported workbook

Resumability: each document is appended to ``checkpoint.jsonl`` the moment it
finishes, so an interrupted scan keeps its work. A resumed run skips every source
already in the checkpoint (keyed by :attr:`Document.source_key`) and the final
``detections.json`` is assembled from the ledger. This matters for very large
(e.g. 120 GB Google-Drive) trees where a run spans hours and may be interrupted.
"""
from __future__ import annotations

import json
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
    def new(cls, out_root: Path, run_id: str | None = None) -> "RunStore":
        run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
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
    def write_meta(self, root: str, complete: bool) -> None:
        self.meta_path.write_text(json.dumps({"root": root, "complete": complete}))

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def find_resumable(cls, out_root: Path, root: str) -> Optional["RunStore"]:
        """Newest incomplete run under ``out_root`` for the same input ``root``
        (has a checkpoint, meta.complete is False), or None."""
        runs = sorted(Path(out_root).glob("run_*"))
        for run_dir in reversed(runs):
            try:
                meta = json.loads((run_dir / "run_meta.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("root") == root and not meta.get("complete") \
                    and (run_dir / "checkpoint.jsonl").exists():
                return cls(run_dir)
        return None
