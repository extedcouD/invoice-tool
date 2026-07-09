"""RunController — the live engine behind the desktop/web UI.

Holds the state of one scan(+link) job and runs it on a background thread so the
UI can poll live progress while it happens. One source of truth shared by both
the Flask views (which *render* what's happening) and the pywebview bridge
(which *starts* the job and provides native file dialogs).

Phases:  idle → scanning → done   (or → error at any point)

GSTR-2A linking is no longer part of the scan; it is deferred and triggered
on demand via :meth:`link_now` from the review UI's "Finish & export" action,
so the linked workbook + flat invoice folder reflect human review corrections.

The scan itself streams fine-grained progress into ``status.json`` via
``StatusWriter``; this controller layers the coarse phase on top and exposes it
through ``status()`` / ``run_state()``.
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from ..config import DEFAULTS, Settings
from ..core.models import RunResult
from ..detect import run_scan
from ..io.gstr import link_gstr
from ..io.runstore import RunStore


class RunController:
    def __init__(self, output_root: Path, settings: Optional[Settings] = None) -> None:
        self.output_root = Path(output_root)
        # 4 threads: OCR shells out to tesseract, so threads give real parallelism
        self.settings = settings or replace(DEFAULTS, workers=4)
        self._lock = threading.Lock()
        self.store: Optional[RunStore] = None
        self.result: Optional[RunResult] = None
        self.phase: str = "idle"          # idle|scanning|linking|done|error
        self.error: Optional[str] = None
        self.link_report = None
        self.gstr_path: Optional[Path] = None   # remembered for post-review linking
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ---------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.phase in ("scanning", "linking")

    def start(self, invoice_root: str | Path, gstr_path: str | Path | None) -> dict:
        """Validate inputs and kick off scan(+link) on a background thread.

        Returns immediately with ``{ok, run_id}`` or ``{ok: False, error}``; the
        UI then polls :meth:`status` / :meth:`run_state` for progress.
        """
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "A run is already in progress."}
            inv = Path(str(invoice_root)).expanduser()
            if not inv.exists() or not inv.is_dir():
                return {"ok": False, "error": f"Invoice folder not found: {inv}"}
            gstr = Path(str(gstr_path)).expanduser() if gstr_path else None
            if gstr is not None and not gstr.exists():
                return {"ok": False, "error": f"GSTR-2A file not found: {gstr}"}
            # Pre-create the run dir NOW so /status can poll status.json from t=0.
            self.store = RunStore.new(self.output_root)
            self.result = None
            self.link_report = None
            self.gstr_path = gstr           # linked later, after review (see link_now)
            self.error = None
            self.phase = "scanning"
            self._thread = threading.Thread(
                target=self._run, args=(inv,), daemon=True)
            self._thread.start()
            return {"ok": True, "run_id": self.store.run_id}

    def _run(self, invoice_root: Path) -> None:
        try:
            store, result = run_scan(invoice_root, self.output_root,
                                     self.settings, quiet=True, store=self.store)
            with self._lock:
                self.store, self.result = store, result
                self.phase = "done"
            # GSTR linking is deliberately deferred: it now runs from the review
            # UI's "Finish & export" action (web/app.py::finish -> link_now) so the
            # linked workbook + flat invoice folder reflect human corrections.
        except Exception as exc:  # never let the worker thread die silently
            with self._lock:
                self.error = str(exc)
                self.phase = "error"
            try:
                self.output_root.mkdir(parents=True, exist_ok=True)
                (self.output_root / "last_error.txt").write_text(traceback.format_exc())
            except OSError:
                pass

    def link_now(self) -> Optional[Any]:
        """Run GSTR-2A linking for the (possibly reviewed) result, on demand.

        Deferred from the scan so the linked workbook + flat invoice folder
        reflect corrections made in review. Returns the ``LinkReport``, or None
        if this run had no GSTR-2A workbook. Callers hold the app write-lock so
        no review edit mutates ``result`` mid-link.
        """
        if self.gstr_path is None or self.result is None or self.store is None:
            return None
        report = link_gstr(self.result, self.gstr_path, self.store)
        with self._lock:
            self.link_report = report
        return report

    # ---- views -------------------------------------------------------------
    def status(self) -> dict:
        """Fine-grained scan progress (from status.json) with the phase on top."""
        payload: dict[str, Any] = {}
        if self.store is not None and self.store.status_path.exists():
            try:
                payload = json.loads(self.store.status_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload["phase"] = self.phase   # coarse phase wins over the scan's 'state'
        return payload

    def run_state(self) -> dict:
        """Coarse state for driving which UI section is shown."""
        with self._lock:
            st: dict[str, Any] = {
                "phase": self.phase,
                "error": self.error,
                "has_result": self.result is not None,
                "run_id": self.store.run_id if self.store else None,
                "output_dir": str(self.store.dir) if self.store else None,
                "has_gstr": self.gstr_path is not None,
            }
            if self.result is not None:
                st["summary"] = self.result.summary()
            if self.link_report is not None:
                r = self.link_report
                st["link"] = {"matched": r.matched, "total": r.total,
                              "not_found": r.not_found, "ambiguous": r.ambiguous}
        return st
