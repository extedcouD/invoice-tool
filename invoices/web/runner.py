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
        # pulls PDF bytes through it.
        self.file_source: Any = LocalFileSource()
        self.source_mode: str = "local"    # local | continue
        self.resumed: bool = False         # this run reopened an interrupted one
        # The financial year this run is scoped to (None = every year under the root).
        self.fy: Optional[str] = None
        # The current long post-scan job (the export) and its progress.
        self.task: Optional[dict] = None
        # The inputs of the last start, so "Resume" survives a page reload (it used
        # to live in a page-scoped JS variable and vanish on any navigation).
        self.last_input: Optional[dict] = None

    # ---- lifecycle ---------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self.phase in ("scanning", "linking", "finishing")

    def start(self, invoice_root: str | Path, gstr_path: str | Path | None,
              source_mode: str = "local",
              fy: str | None = None) -> dict:
        """Validate inputs and kick off scan on a background thread.

        ``source_mode`` is "local" (a filesystem folder) or "continue"
        (``invoice_root`` is an existing run folder to append newly-added PDFs to —
        its root/fy/gstr come from run_meta.json). If an interrupted run for the same
        input exists, it is **reopened and resumed** rather than restarted. Returns
        immediately with ``{ok, run_id, resumed}``.
        """
        if self.busy:
            return {"ok": False, "error": "A run is already in progress."}

        source_mode = source_mode or "local"
        fy = (fy or "").strip() or None      # "" (All years) means no scope

        # Resolve the inputs *outside* the lock: opening a run dir touches the disk,
        # and holding the lock across it stalled every /run_state poll for its duration.
        continue_store: Optional[RunStore] = None
        if source_mode == "continue":
            # "Continue a saved run": ``invoice_root`` is a run folder. Its root/fy/gstr
            # come from run_meta.json, and opening the store explicitly bypasses the
            # completeness gate so a *finished* run can be extended — the pipeline skips
            # every source_key already in the checkpoint, so only new PDFs are scanned.
            try:
                continue_store = RunStore.open_existing(str(invoice_root))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            d = continue_store.describe()
            if not d.get("root"):
                return {"ok": False, "error": "That run folder has no recorded root."}
            root = Path(d["root"])
            if not root.is_dir():
                return {"ok": False, "error": f"The run's tree {d['root']} no longer "
                        "exists. Matches are keyed on absolute paths, so it must be "
                        "where it was scanned."}
            root_key = d["root"]
            fy = d["fy"]
            gstr = Path(d["gstr"]) if d["gstr"] else None
            file_source = LocalFileSource()
            walker = None
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
            self.file_source = file_source
            self.fy = fy
            # A per-run copy — never mutate self.settings: the controller outlives the
            # run and the next one may pick a different year (or none).
            run_settings = replace(self.settings, fy_scope=fy)
            # Continue mode opens an explicit run dir (finished or not). Otherwise
            # resume an interrupted run for the same input, else mint a fresh one. The
            # input is (root, fy): an all-years run and a one-year run over the same
            # tree are different corpora and must never resume each other.
            existing = (None if continue_store
                        else RunStore.find_resumable(self.output_root, root_key, fy=fy,
                                                     pages=run_settings.explode_pages))
            self.store = continue_store or existing or RunStore.new(self.output_root, label=fy)
            self.resumed = continue_store is not None or existing is not None
            self.result = None
            self.plan = None
            self.link_report = None
            self.gstr_path = gstr
            self.error = None
            self.phase = "scanning"
            self.control = RunControl()
            self.last_input = {
                "invoice": str(invoice_root), "gstr": str(gstr_path or ""),
                "source_mode": source_mode,
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

    # ---- long post-scan job (the export) ------------------------------------
    # "Finish & export" is *slow* — write_linked copies every matched invoice's
    # bytes and rewrites the workbook cell by cell. Run synchronously inside the POST
    # it froze the whole window for seconds with no sign of life, and every review
    # edit blocked behind the same write lock. So it runs on a thread and publishes
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
                "resumed": self.resumed,
                "last_input": self.last_input,
                "busy": self.phase in ("scanning", "linking", "finishing"),
            }
            if self.result is not None:
                st["summary"] = self.result.summary()
            if self.plan is not None:
                st["link"] = self.plan.counts()
        return st
