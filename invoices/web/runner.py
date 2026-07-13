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
from functools import partial
from pathlib import Path
from typing import Any, Optional

from ..config import DEFAULTS, Settings
from ..core.control import RunControl
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
        self.phase: str = "idle"          # idle|scanning|linking|done|stopped|error
        self.error: Optional[str] = None
        self.link_report = None
        self.gstr_path: Optional[Path] = None   # remembered for post-review linking
        self._thread: Optional[threading.Thread] = None
        self.control: Optional[RunControl] = None   # pause/stop for the live scan
        # ---- Google Drive source/sink (Phase 3) ----
        self.source_mode: str = "local"    # local | drive
        self.drive = None                  # DriveClient once signed in
        self.resumed: bool = False         # this run reopened an interrupted one
        self.upload_folder_name: Optional[str] = None
        self.upload_report: Optional[dict] = None

    # ---- Google Drive auth -------------------------------------------------
    def authenticate_drive(self, open_browser: bool = True) -> dict:
        """Run (or refresh) Google Drive OAuth. Returns {ok} or {ok:False,error}."""
        try:
            from ..io.drive import DriveClient
            self.drive = DriveClient.authenticate(open_browser=open_browser)
            return {"ok": True}
        except Exception as exc:  # missing client_secret / declined consent / offline
            return {"ok": False, "error": str(exc)}

    # ---- lifecycle ---------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.phase in ("scanning", "linking")

    def start(self, invoice_root: str | Path, gstr_path: str | Path | None,
              source_mode: str = "local",
              upload_folder_name: str | None = None) -> dict:
        """Validate inputs and kick off scan on a background thread.

        ``source_mode`` is "local" (a filesystem folder) or "drive" (a Google
        Drive folder id/URL, using the already-authenticated client). If an
        interrupted run for the same input exists, it is **reopened and resumed**
        rather than restarted. Returns immediately with ``{ok, run_id, resumed}``.
        """
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "A run is already in progress."}

            self.source_mode = source_mode
            self.upload_folder_name = (upload_folder_name or "").strip() or None

            if source_mode == "drive":
                if self.drive is None:
                    return {"ok": False, "error": "Connect Google Drive first."}
                from ..io.drive import (DriveFileSource, file_id_from,
                                        folder_id_from, walk_drive)
                folder_id = folder_id_from(str(invoice_root))
                if not folder_id:
                    return {"ok": False, "error": "Enter a Google Drive folder link or id."}
                # The GSTR-2A workbook lives on Drive too. Fetch it now — it's small,
                # and a bad link should fail here in the form rather than an hour
                # later when the user clicks "Finish & export".
                gstr: Optional[Path] = None
                if gstr_path:
                    try:
                        gstr = Path(self.drive.download_workbook_to_temp(
                            file_id_from(str(gstr_path))))
                    except Exception as exc:
                        return {"ok": False,
                                "error": f"Could not read that GSTR-2A workbook from Drive: {exc}"}
                root: Any = folder_id
                root_key = f"drive:{folder_id}"
                file_source: Any = DriveFileSource(self.drive)
                walker: Any = partial(walk_drive, client=self.drive)
            else:
                gstr = Path(str(gstr_path)).expanduser() if gstr_path else None
                if gstr is not None and not gstr.exists():
                    return {"ok": False, "error": f"GSTR-2A file not found: {gstr}"}
                inv = Path(str(invoice_root)).expanduser()
                if not inv.exists() or not inv.is_dir():
                    return {"ok": False, "error": f"Invoice folder not found: {inv}"}
                root = inv
                root_key = str(inv.resolve())
                file_source = None
                walker = None

            # Resume an interrupted run for the same input, else mint a fresh one.
            existing = RunStore.find_resumable(self.output_root, root_key)
            self.store = existing or RunStore.new(self.output_root)
            self.resumed = existing is not None
            self.result = None
            self.link_report = None
            self.upload_report = None
            self.gstr_path = gstr           # linked later, after review (see link_now)
            self.error = None
            self.phase = "scanning"
            self.control = RunControl()
            self._thread = threading.Thread(
                target=self._run, args=(root, root_key, file_source, walker),
                daemon=True)
            self._thread.start()
            return {"ok": True, "run_id": self.store.run_id, "resumed": self.resumed}

    # ---- pause / stop ------------------------------------------------------
    def pause(self) -> dict:
        if self.control is None or not self.busy:
            return {"ok": False, "error": "No run in progress."}
        self.control.pause()
        return {"ok": True, "paused": True}

    def resume_run(self) -> dict:
        if self.control is None or not self.busy:
            return {"ok": False, "error": "No run in progress."}
        self.control.resume()
        return {"ok": True, "paused": False}

    def stop(self) -> dict:
        """Stop after the in-flight documents finish. The run stays resumable.

        Everything already scanned is in the checkpoint and the partial master is
        still written, so this is a safe exit, not a discard: start the same folder
        again and it picks up where it left off.
        """
        if self.control is None or not self.busy:
            return {"ok": False, "error": "No run in progress."}
        self.control.stop()
        return {"ok": True, "stopping": True}

    def _run(self, root, root_key, file_source, walker) -> None:
        try:
            store, result = run_scan(root, self.output_root, self.settings,
                                     quiet=True, store=self.store,
                                     file_source=file_source, walker=walker,
                                     root_key=root_key, control=self.control)
            with self._lock:
                self.store, self.result = store, result
                self.phase = "stopped" if (
                    self.control is not None and self.control.stopped) else "done"
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

    def upload_results(self) -> dict:
        """Publish outputs to a NEW Google Drive folder (drive mode only).

        Creates ``<name>/`` with the master workbook plus an ``invoices/``
        subfolder of the detected invoices — the latter assembled with
        server-side ``files.copy`` so invoice bytes never leave Drive. The input
        tree is only ever read, never modified.
        """
        if self.source_mode != "drive" or self.drive is None \
                or self.result is None or self.store is None:
            return {"ok": False, "error": "No Drive run to upload."}
        try:
            from ..io.drive import XLSX_MIME
            name = self.upload_folder_name or f"InvoiceLinker {self.store.run_id}"
            folder_id = self.drive.create_folder(name)
            master = self.store.master_path()
            if master.exists():
                self.drive.upload_file(master, folder_id, mime=XLSX_MIME)
            # The linked GSTR-2A workbook is the point of the whole run, so it goes
            # up alongside the master (when linking has actually been run).
            linked = False
            if self.link_report is not None:
                out = Path(self.link_report.out_path)
                if out.exists():
                    self.drive.upload_file(out, folder_id, mime=XLSX_MIME)
                    linked = True
            inv_folder = self.drive.create_folder("invoices", folder_id)
            copied = 0
            for d in self.result.invoices():
                if d.drive_file_id:
                    self.drive.copy_file(d.drive_file_id, inv_folder, name=d.filename)
                    copied += 1
            report = {"ok": True, "folder": name, "folder_id": folder_id,
                      "invoices": copied, "linked_workbook": linked}
        except Exception as exc:
            report = {"ok": False, "error": str(exc)}
        with self._lock:
            self.upload_report = report
        return report

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
                "source_mode": self.source_mode,
                "drive_signed_in": self.drive is not None,
                "resumed": self.resumed,
            }
            if self.result is not None:
                st["summary"] = self.result.summary()
            if self.link_report is not None:
                r = self.link_report
                st["link"] = {"matched": r.matched, "total": r.total,
                              "not_found": r.not_found, "ambiguous": r.ambiguous}
            if self.upload_report is not None:
                st["upload"] = self.upload_report
        return st
