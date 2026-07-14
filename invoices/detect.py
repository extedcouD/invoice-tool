"""Orchestration for a `scan` run: assemble the pipeline, run it, canonicalize
vendors by GSTIN, copy flagged PDFs for review, persist, and write a draft Excel.

Keeping the wiring here (not in cli.py) means the web app and tests can trigger
an identical run programmatically.
"""
from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import Settings, DEFAULTS
from .core.control import RunControl
from .core.interfaces import FileSource
from .core.models import Document, DocType, RunResult
from .core.pipeline import Pipeline
from .io.excel import write_master
from .io.pdf import PdfTextSource
from .io.runstore import RunStore
from .io.sources import LocalFileSource
from .matching import vendor as vendormatch
from .observability.events import record
from .observability.progress import MultiReporter, RichReporter, StatusWriter, Reporter
from .stages.classify import ClassifyStage
from .stages.extract import ExtractStage
from .stages.parse import ParseStage
from .stages.reconcile import ReconcileStage
from .stages.validate import ValidateStage
from .stages.walk import walk

# A walker streams seed Documents from a root (local path or Drive folder id),
# checking in with the RunControl as it goes.
Walker = Callable[..., Iterable[Document]]


def build_pipeline(settings: Settings, reporter: Reporter,
                   file_source: FileSource | None = None,
                   control: RunControl | None = None) -> Pipeline:
    return Pipeline(
        stages=[
            ExtractStage(PdfTextSource(settings, file_source, control=control)),
            ClassifyStage(settings),
            ParseStage(),
            ReconcileStage(settings),
            ValidateStage(settings),
        ],
        reporter=reporter,
        workers=settings.workers,
        control=control,
    )


def _canonicalize_vendors(result: RunResult) -> None:
    """Use GSTIN as the unique vendor key to spot misfiled folders.

    Same GSTIN => same legal vendor. If a folder's company name disagrees with
    the dominant name for that GSTIN, it's likely a filing slip — surface it.
    """
    by_gstin: dict[str, list] = defaultdict(list)
    for d in result.invoices():
        if d.fields.vendor_gstin:
            by_gstin[d.fields.vendor_gstin].append(d)

    for gstin, docs in by_gstin.items():
        names = Counter(d.path_info.company for d in docs if d.path_info.company)
        if not names:
            continue
        canonical = names.most_common(1)[0][0]
        for d in docs:
            record(d, "canonicalize", "vendor_key",
                   f"gstin={gstin} canonical='{canonical}'", canonical=canonical)
            comp = d.path_info.company
            if comp and vendormatch.score(comp, canonical) < DEFAULTS.fuzzy_soft_flag:
                d.add_flag("vendor_folder_variant",
                           f"folder '{comp}' differs from GSTIN {gstin} vendor '{canonical}'")


def _flag_duplicate_ids(result: RunResult) -> None:
    """Same invoice id in two folders => a filing duplicate. Flag every copy."""
    by_id: dict[str, list] = defaultdict(list)
    for d in result.invoices():
        if d.fields.invoice_id:
            by_id[d.fields.invoice_id].append(d)
    for inv_id, docs in by_id.items():
        if len(docs) > 1:
            others = [Path(x.path).name for x in docs]
            for d in docs:
                d.add_flag("duplicate_id",
                           f"invoice id '{inv_id}' appears in {len(docs)} places: {others}")
                record(d, "canonicalize", "duplicate_id",
                       f"{inv_id} x{len(docs)}", severity="warn")


def _copy_one_flagged(d: Document, store: RunStore, file_source: FileSource) -> None:
    dest = store.review_dir / f"{d.id}__{d.filename}"
    try:
        local = file_source.materialize(d)
    except Exception as exc:  # a failed fetch shouldn't sink the run
        record(d, "review", "fetch_failed", str(exc), severity="warn")
        return
    try:
        shutil.copy2(local, dest)
        d.review_pdf_path = str(dest.resolve())  # absolute so the web app can serve it
    except OSError as exc:
        record(d, "review", "copy_failed", str(exc), severity="warn")
    finally:
        file_source.cleanup(d, local)


def _copy_flagged(result: RunResult, store: RunStore, file_source: FileSource,
                  workers: int = 4) -> None:
    """Pre-copy flagged PDFs into the run's review folder so the web UI can serve
    them. Bytes come through the FileSource, so a Drive-hosted doc is downloaded
    here.

    Done in parallel: on a Drive run this is one network download per flagged doc,
    and serially it was the single longest part of the shutdown path. It is also
    only an optimization — ``/pdf/<id>`` materializes on demand for anything not
    copied — so a stopped run skips it entirely.
    """
    flagged = [d for d in result.documents if d.needs_review]
    if not flagged:
        return
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(lambda d: _copy_one_flagged(d, store, file_source), flagged))


def run_scan(root, out_root: Path, settings: Settings = DEFAULTS,
             quiet: bool = False, store: RunStore | None = None,
             file_source: FileSource | None = None,
             walker: Optional[Walker] = None,
             root_key: str | None = None,
             control: "RunControl | None" = None,
             gstr_path: Path | None = None,
             on_phase: Optional[Callable[[str], None]] = None) -> tuple[RunStore, RunResult]:
    """Scan a tree (local path or, via ``walker``/``file_source``, a Drive folder).

    Resumable: if ``store`` already holds a checkpoint (an interrupted run reopened
    by the caller), every source already recorded is skipped and only the
    remainder is processed; the final result is assembled from the full ledger.

    ``control`` (optional) lets the caller pause/stop the scan. A stopped run still
    writes its partial detections + master from the docs that *did* finish, but is
    deliberately left marked **incomplete** so the next run over the same input
    resumes it instead of starting over.

    ``gstr_path`` (optional): match the detected invoices against the GSTR-2A return
    as soon as the scan ends. Matching is pure and in-memory, so this is cheap — and
    it is what lets the review UI open on "74 B2B rows have no PDF" instead of that
    only becoming knowable after review, at export time.

    ``on_phase`` reports the coarse phase ("scanning" / "linking" / "finishing") so
    the UI can stop claiming to scan while it is really writing a workbook.
    """
    file_source = file_source or LocalFileSource()
    walker = walker or walk
    # A caller (e.g. the desktop RunController) may pre-create/reopen the run dir
    # so it can poll status.json from t=0; otherwise mint a fresh one here.
    store = store or RunStore.new(out_root, label=settings.fy_scope)
    # root_key identifies the input for resume-matching; for a local path it's the
    # resolved path, for Drive the caller passes the folder id explicitly. The FY the
    # run was scoped to is a *second* half of that identity (see find_resumable) —
    # an all-years run and a one-year run over the same tree are different corpora.
    root_key = root_key if root_key is not None else str(Path(root).resolve())
    store.write_meta(root=root_key, complete=False, fy=settings.fy_scope)

    def phase(name: str) -> None:
        if on_phase is not None:
            on_phase(name)

    reporter = MultiReporter(
        StatusWriter(store.status_path, control=control),
        None if quiet else RichReporter(),
    )

    started = datetime.now().isoformat(timespec="seconds")
    phase("scanning")

    # The walker is a generator: the pipeline pulls from it, so discovery and
    # processing overlap and Stop is honoured *during* the walk.
    pipeline = build_pipeline(settings, reporter, file_source, control=control)
    pipeline.run(walker(root, settings, control=control),
                 on_doc_done=store.append_checkpoint,
                 skip_keys=store.done_keys())          # empty for a fresh run

    # Assemble from the durable ledger (already-done + this batch), so a resumed
    # run yields the same complete corpus as an uninterrupted one.
    phase("finishing")
    documents = store.checkpoint_docs()
    documents.sort(key=lambda d: d.path)   # deterministic regardless of completion order

    # Complete iff we actually reached the end of the tree. This is the single
    # source of truth for resumability: deriving it from "did the user press Stop"
    # instead meant a Stop landing as the run drained marked the run complete but
    # showed the resume screen — and the resume then silently rescanned from zero.
    complete = not pipeline.cancelled and pipeline.discovery_complete

    result = RunResult(
        run_id=store.run_id,
        root=root_key,
        started_at=started,
        complete=complete,
        documents=documents,
        stage_metrics=pipeline.metrics(),
    )
    _canonicalize_vendors(result)
    _flag_duplicate_ids(result)

    if gstr_path is not None:
        phase("linking")
        try:
            link_now(result, gstr_path, store)
            store.update_meta(link_error=None)
        except Exception as exc:   # a bad workbook must not sink a good scan
            store.update_meta(link_error=str(exc))

    # Only an optimization (see _copy_flagged), and the longest part of the
    # shutdown path on Drive — a stopped run skips it and gets the user out.
    if not pipeline.cancelled:
        phase("finishing")
        _copy_flagged(result, store, file_source, workers=settings.workers)

    result.finished_at = datetime.now().isoformat(timespec="seconds")
    store.save(result)
    write_master(result, store.master_path())
    store.write_meta(root=root_key, complete=complete, fy=settings.fy_scope)
    reporter.finish(result.summary())
    return store, result


def link_now(result: RunResult, gstr_path: Path, store: RunStore,
             file_source: FileSource | None = None):
    """(Re)match the detected invoices against the GSTR-2A return and persist.

    Pure matching only — no PDF copying, no workbook written. Cheap enough to call
    after every single review edit, which is what lets the UI tell a reviewer
    "the GSTIN you just fixed now matches B2B row 143".
    """
    from .io.gstr import apply_plan, match, read_b2b_rows

    gstr_path = _keep_gstr_with_run(Path(gstr_path), store)
    rows = read_b2b_rows(gstr_path)
    plan = match(rows, result.invoices(), store.manual_links())
    apply_plan(result, plan)
    store.save_link(plan)
    return plan


def _keep_gstr_with_run(gstr_path: Path, store: RunStore) -> Path:
    """Copy the return into the run dir and remember it by absolute path.

    The run dir is the single source of truth, and the workbook has to survive with
    it: on a Drive run the caller hands us a *temp* download that will vanish, and a
    path typed at the CLI is relative to whatever cwd that invocation had. Either
    way, reopening the run later (`invoices review`) could no longer find the return
    and "Finish & export" would claim none was ever chosen.
    """
    kept = store.dir / gstr_path.name
    try:
        if not kept.exists() or not gstr_path.samefile(kept):
            shutil.copy2(gstr_path, kept)
    except OSError:
        kept = gstr_path          # unreadable/unwritable — carry on with the original
    store.update_meta(gstr_path=str(Path(kept).resolve()))
    return kept
