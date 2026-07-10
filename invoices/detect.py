"""Orchestration for a `scan` run: assemble the pipeline, run it, canonicalize
vendors by GSTIN, copy flagged PDFs for review, persist, and write a draft Excel.

Keeping the wiring here (not in cli.py) means the web app and tests can trigger
an identical run programmatically.
"""
from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import Settings, DEFAULTS
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

# A walker turns a root (local path or Drive folder id) into seed Documents.
Walker = Callable[[object, Settings], list[Document]]


def build_pipeline(settings: Settings, reporter: Reporter,
                   file_source: FileSource | None = None) -> Pipeline:
    return Pipeline(
        stages=[
            ExtractStage(PdfTextSource(settings, file_source)),
            ClassifyStage(settings),
            ParseStage(),
            ReconcileStage(settings),
            ValidateStage(settings),
        ],
        reporter=reporter,
        workers=settings.workers,
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


def _copy_flagged(result: RunResult, store: RunStore, file_source: FileSource) -> None:
    """Copy each flagged PDF into the run's review folder so the web UI can serve
    it. Bytes come through the FileSource, so a Drive-hosted doc is downloaded
    here (only flagged docs — a minority — incur this fetch)."""
    for d in result.documents:
        if not d.needs_review:
            continue
        dest = store.review_dir / f"{d.id}__{d.filename}"
        try:
            local = file_source.materialize(d)
        except Exception as exc:  # a failed fetch shouldn't sink the run
            record(d, "review", "fetch_failed", str(exc), severity="warn")
            continue
        try:
            shutil.copy2(local, dest)
            d.review_pdf_path = str(dest.resolve())  # absolute so the web app can serve it
        except OSError as exc:
            record(d, "review", "copy_failed", str(exc), severity="warn")
        finally:
            file_source.cleanup(d, local)


def run_scan(root, out_root: Path, settings: Settings = DEFAULTS,
             quiet: bool = False, store: RunStore | None = None,
             file_source: FileSource | None = None,
             walker: Optional[Walker] = None,
             root_key: str | None = None) -> tuple[RunStore, RunResult]:
    """Scan a tree (local path or, via ``walker``/``file_source``, a Drive folder).

    Resumable: if ``store`` already holds a checkpoint (an interrupted run reopened
    by the caller), every source already recorded is skipped and only the
    remainder is processed; the final result is assembled from the full ledger.
    """
    file_source = file_source or LocalFileSource()
    walker = walker or walk
    # A caller (e.g. the desktop RunController) may pre-create/reopen the run dir
    # so it can poll status.json from t=0; otherwise mint a fresh one here.
    store = store or RunStore.new(out_root)
    # root_key identifies the input for resume-matching; for a local path it's the
    # resolved path, for Drive the caller passes the folder id explicitly.
    root_key = root_key if root_key is not None else str(Path(root).resolve())
    store.write_meta(root=root_key, complete=False)

    reporter = MultiReporter(
        StatusWriter(store.status_path),
        None if quiet else RichReporter(),
    )

    started = datetime.now().isoformat(timespec="seconds")
    all_docs = walker(root, settings)
    done_keys = store.done_keys()                      # empty for a fresh run
    todo = [d for d in all_docs if d.source_key not in done_keys]

    pipeline = build_pipeline(settings, reporter, file_source)
    pipeline.run(todo, on_doc_done=store.append_checkpoint,
                 start_done=len(all_docs) - len(todo), total=len(all_docs))

    # Assemble from the durable ledger (already-done + this batch), so a resumed
    # run yields the same complete corpus as an uninterrupted one.
    documents = store.checkpoint_docs()

    result = RunResult(
        run_id=store.run_id,
        root=root_key,
        started_at=started,
        documents=documents,
        stage_metrics=pipeline.metrics(),
    )
    _canonicalize_vendors(result)
    _flag_duplicate_ids(result)
    _copy_flagged(result, store, file_source)
    result.finished_at = datetime.now().isoformat(timespec="seconds")

    store.save(result)
    write_master(result, store.master_path())
    store.write_meta(root=root_key, complete=True)     # mark done (no longer resumable)
    reporter.finish(result.summary())
    return store, result
