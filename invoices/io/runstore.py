"""Persistence for a run. The run directory is the single source of truth, so
runs are resumable and `export`/`review` operate without re-scanning.

Layout:
  out/run_<ts>/
    detections.json   # full RunResult (documents + events + metrics)
    status.json       # live progress snapshot (written during scan)
    review_pdfs/      # copies of flagged PDFs for the review UI
    master_<ts>.xlsx  # exported workbook
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..core.models import RunResult


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
