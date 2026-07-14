"""RunController — the live engine behind the desktop/web UI.

Holds the state of one scan(+link) job and runs it on a background thread so the
UI can poll live progress while it happens. One source of truth shared by both
the Flask views (which *render* what's happening) and the pywebview bridge
(which *starts* the job and provides native file dialogs).

Phases:  idle → scanning → [linking] → finishing → done | stopped   (or → error)

GSTR-2A **matching** now runs as soon as the scan ends (it is pure and in-memory),
so the review queue can open on the real question — which B2B rows have no PDF —
rather than that only becoming knowable at export time. The expensive half of
linking (copying PDFs, writing the workbook) is still deferred to "Finish &
export" via :meth:`export_linked`, so it reflects human corrections.

The scan streams fine-grained progress into ``status.json`` via ``StatusWriter``;
this controller layers the coarse phase on top and exposes it through
``status()`` / ``run_state()``.
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
from ..detect import link_now, run_scan
from ..io.runstore import RunStore
from ..io.sources import LocalFileSource


class RunController:
    def __init__(self, output_root: Path, settings: Optional[Settings] = None) -> None:
        self.output_root = Path(output_root)
        # 4 threads: OCR shells out to tesseract, so threads give real parallelism
        self.settings = settings or replace(DEFAULTS, workers=4)
        self._lock = threading.Lock()
        self.store: Optional[RunStore] = None
        self.result: Optional[RunResult] = None
        self.phase: str = "idle"          # idle|scanning|linking|finishing|done|stopped|error
        self.error: Optional[str] = None
        self.link_report = None           # set by export_linked (the written workbook)
        self.plan = None                  # the live LinkPlan — what review works from
        self.gstr_path: Optional[Path] = None
        self._thread: Optional[threading.Thread] = None
        self.control: Optional[RunControl] = None   # pause/stop for the live scan
        # The FileSource must outlive the scan: exporting the linked invoice folder
        # pulls PDF bytes, and on a Drive run those are not on disk.
        self.file_source: Any = LocalFileSource()
        # ---- Google Drive source/sink ----
        self.source_mode: str = "local"    # local | drive
        self.drive = None                  # DriveClient once signed in
        self.resumed: bool = False         # this run reopened an interrupted one
        self.upload_folder_name: Optional[str] = None
        self.upload_report: Optional[dict] = None
        # The financial year this run is scoped to (None = every year under the root).
        self.fy: Optional[str] = None
        # The current long post-scan job (export / Drive upload) and its progress.
        self.task: Optional[dict] = None
        # The inputs of the last start, so "Resume" survives a page reload (it used
        # to live in a page-scoped JS variable and vanish on any navigation).
        self.last_input: Optional[dict] = None

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
        with self._lock:
            return self.phase in ("scanning", "linking", "finishing")

    def start(self, invoice_root: str | Path, gstr_path: str | Path | None,
              source_mode: str = "local",
              upload_folder_name: str | None = None,
              fy: str | None = None) -> dict:
        """Validate inputs and kick off scan on a background thread.

        ``source_mode`` is "local" (a filesystem folder) or "drive" (a Google
        Drive folder id/URL, using the already-authenticated client). If an
        interrupted run for the same input exists, it is **reopened and resumed**
        rather than restarted. Returns immediately with ``{ok, run_id, resumed}``.
        """
        if self.busy:
            return {"ok": False, "error": "A run is already in progress."}

        source_mode = source_mode or "local"
        upload_name = (upload_folder_name or "").strip() or None
        fy = (fy or "").strip() or None      # "" (All years) means no scope

        # Resolve the inputs *outside* the lock: on a Drive run this downloads the
        # GSTR workbook, and holding the lock across a network fetch stalled every
        # /run_state poll for its duration.
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
            file_source = LocalFileSource()
            walker = None

        with self._lock:
            if self.phase in ("scanning", "linking", "finishing"):
                return {"ok": False, "error": "A run is already in progress."}

            self.source_mode = source_mode
            self.upload_folder_name = upload_name
            self.file_source = file_source
            self.fy = fy
            # A per-run copy — never mutate self.settings: the controller outlives the
            # run and the next one may pick a different year (or none).
            run_settings = replace(self.settings, fy_scope=fy)
            # Resume an interrupted run for the same input, else mint a fresh one. The
            # input is (root, fy): an all-years run and a one-year run over the same
            # tree are different corpora and must never resume each other.
            existing = RunStore.find_resumable(self.output_root, root_key, fy=fy)
            self.store = existing or RunStore.new(self.output_root, label=fy)
            self.resumed = existing is not None
            self.result = None
            self.plan = None
            self.link_report = None
            self.upload_report = None
            self.gstr_path = gstr
            self.error = None
            self.phase = "scanning"
            self.control = RunControl()
            self.last_input = {
                "invoice": str(invoice_root), "gstr": str(gstr_path or ""),
                "source_mode": source_mode, "upload_folder": upload_name or "",
                # Load-bearing: the Resume button POSTs last_input verbatim. Drop the
                # year here and resuming a stopped one-year run would start a fresh
                # *unscoped* run that rescans the whole tree from zero.
                "fy": fy or "",
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(root, root_key, file_source, walker, run_settings),
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
        """Stop after the in-flight stages finish. The run stays resumable.

        Everything already scanned is in the checkpoint and the partial master is
        still written, so this is a safe exit, not a discard: start the same folder
        again and it picks up where it left off.
        """
        if self.control is None or not self.busy:
            return {"ok": False, "error": "No run in progress."}
        self.control.stop()
        return {"ok": True, "stopping": True}

    def _set_phase(self, name: str) -> None:
        with self._lock:
            self.phase = name

    def _run(self, root, root_key, file_source, walker, settings) -> None:
        try:
            store, result = run_scan(root, self.output_root, settings,
                                     quiet=True, store=self.store,
                                     file_source=file_source, walker=walker,
                                     root_key=root_key, control=self.control,
                                     gstr_path=self.gstr_path,
                                     on_phase=self._set_phase)
            with self._lock:
                self.store, self.result = store, result
                self.plan = store.load_link()
                # Derived from whether the tree was actually exhausted — NOT from
                # "was Stop pressed". A Stop landing as the run drained used to mark
                # the run complete yet show the resume screen, and the resume then
                # silently started a fresh scan.
                self.phase = "done" if result.complete else "stopped"
        except Exception as exc:  # never let the worker thread die silently
            with self._lock:
                self.error = str(exc)
                self.phase = "error"
            try:
                self.output_root.mkdir(parents=True, exist_ok=True)
                (self.output_root / "last_error.txt").write_text(traceback.format_exc())
            except OSError:
                pass

    # ---- linking -----------------------------------------------------------
    def rematch(self) -> Optional[Any]:
        """Re-run the (pure) GSTR match against the current, reviewed documents.

        Called after every review edit, so a corrected GSTIN immediately shows up
        as "now matches B2B row 143".
        """
        if self.gstr_path is None or self.result is None or self.store is None:
            return None
        self.plan = link_now(self.result, self.gstr_path, self.store,
                             self.file_source)
        return self.plan

    def export_linked(self, on_progress=None) -> Optional[Any]:
        """Write the linked workbook + flat invoice folder for the reviewed result.

        The expensive half of linking, deliberately deferred until review is done.
        Callers hold the app write-lock so no review edit mutates ``result`` mid-write.
        """
        from ..io.gstr import write_linked

        if self.gstr_path is None or self.result is None or self.store is None:
            return None
        if self.plan is None:
            self.rematch()
        report = write_linked(self.result, self.plan, self.gstr_path, self.store,
                              self.file_source, on_progress=on_progress,
                              workers=max(4, self.settings.workers))
        with self._lock:
            self.link_report = report
        return report

    # ---- long post-scan jobs (export / Drive upload) ------------------------
    # These are *slow* — on a Drive run each matched invoice is a network download,
    # and each uploaded one a Drive round-trip. Run synchronously inside the POST
    # they froze the whole window for minutes with no sign of life, and every review
    # edit blocked behind the same write lock. So they run on a thread and publish
    # progress that the page polls, exactly like the scan does.
    def task_state(self) -> dict:
        with self._lock:
            t = dict(self.task) if self.task else {"kind": None, "phase": "idle"}
        return t

    @property
    def task_running(self) -> bool:
        return bool(self.task and self.task["phase"] == "running")

    def _set_task(self, **fields) -> None:
        with self._lock:
            if self.task is not None:
                self.task.update(fields)

    def _progress(self, done: int, total: int) -> None:
        self._set_task(done=done, total=total)

    def start_task(self, kind: str, fn) -> dict:
        """Run ``fn(progress)`` on a background thread as the named job."""
        with self._lock:
            if self.task is not None and self.task["phase"] == "running":
                return {"ok": False, "error": f"'{self.task['kind']}' is already running."}
            if self.phase in ("scanning", "linking", "finishing"):
                return {"ok": False, "error": "The scan is still running."}
            self.task = {"kind": kind, "phase": "running", "done": 0, "total": 0,
                         "message": "", "error": None}

        def run() -> None:
            try:
                fn(self._progress)
                self._set_task(phase="done")
            except Exception as exc:
                traceback.print_exc()
                self._set_task(phase="error", error=str(exc))

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "kind": kind}

    def upload_results(self, on_progress=None) -> dict:
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
            # One server-side copy per invoice — a network round-trip each, so report
            # progress rather than sitting silent for minutes on a big run.
            todo = [d for d in self.result.invoices() if d.drive_file_id]
            copied = 0
            if on_progress:
                on_progress(0, len(todo))
            for d in todo:
                self.drive.copy_file(d.drive_file_id, inv_folder, name=d.filename)
                copied += 1
                if on_progress:
                    on_progress(copied, len(todo))
            report = {"ok": True, "folder": name, "folder_id": folder_id,
                      "invoices": copied, "linked_workbook": linked}
        except Exception as exc:
            report = {"ok": False, "error": str(exc)}
        with self._lock:
            self.upload_report = report
        return report

    # ---- views -------------------------------------------------------------
    def status(self) -> dict:
        """Fine-grained scan progress (from status.json) with live control state.

        `paused`/`stopping` are read from the RunControl itself, NOT from
        status.json. status.json is only rewritten when a document *completes* — so
        pausing (which stops documents completing) froze it with `paused: false`
        forever, the button flipped back to "Pause" on the next poll, and Resume
        became unreachable. Control state has to be pulled, not pushed.
        """
        payload: dict[str, Any] = {}
        if self.store is not None and self.store.status_path.exists():
            try:
                payload = json.loads(self.store.status_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = {}
        c, busy = self.control, self.busy
        payload["paused"] = bool(c is not None and busy and c.paused)
        payload["stopping"] = bool(c is not None and busy and c.stopped)
        payload["phase"] = self.phase
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
                "fy": self.fy,
                "drive_signed_in": self.drive is not None,
                "resumed": self.resumed,
                "last_input": self.last_input,
                "busy": self.phase in ("scanning", "linking", "finishing"),
            }
            if self.result is not None:
                st["summary"] = self.result.summary()
            if self.plan is not None:
                st["link"] = self.plan.counts()
            if self.upload_report is not None:
                st["upload"] = self.upload_report
        return st
