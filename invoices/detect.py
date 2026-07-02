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

from .config import Settings, DEFAULTS
from .core.models import DocType, RunResult
from .core.pipeline import Pipeline
from .io.excel import write_master
from .io.pdf import PdfTextSource
from .io.runstore import RunStore
from .matching import vendor as vendormatch
from .observability.events import record
from .observability.progress import MultiReporter, RichReporter, StatusWriter, Reporter
from .stages.classify import ClassifyStage
from .stages.extract import ExtractStage
from .stages.parse import ParseStage
from .stages.reconcile import ReconcileStage
from .stages.validate import ValidateStage
from .stages.walk import walk


def build_pipeline(settings: Settings, reporter: Reporter) -> Pipeline:
    return Pipeline(
        stages=[
            ExtractStage(PdfTextSource(settings)),
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


def _copy_flagged(result: RunResult, store: RunStore) -> None:
    for d in result.documents:
        if not d.needs_review:
            continue
        src = Path(d.path)
        dest = store.review_dir / f"{d.id}__{src.name}"
        try:
            shutil.copy2(src, dest)
            d.review_pdf_path = str(dest.resolve())  # absolute so the web app can serve it
        except OSError as exc:
            record(d, "review", "copy_failed", str(exc), severity="warn")


def run_scan(root: Path, out_root: Path, settings: Settings = DEFAULTS,
             quiet: bool = False) -> tuple[RunStore, RunResult]:
    root = Path(root)
    store = RunStore.new(out_root)

    reporter = MultiReporter(
        StatusWriter(store.status_path),
        None if quiet else RichReporter(),
    )

    started = datetime.now().isoformat(timespec="seconds")
    docs = walk(root, settings)
    pipeline = build_pipeline(settings, reporter)
    docs = pipeline.run(docs)

    result = RunResult(
        run_id=store.run_id,
        root=str(root.resolve()),
        started_at=started,
        documents=docs,
        stage_metrics=pipeline.metrics(),
    )
    _canonicalize_vendors(result)
    _flag_duplicate_ids(result)
    _copy_flagged(result, store)
    result.finished_at = datetime.now().isoformat(timespec="seconds")

    store.save(result)
    write_master(result, store.master_path())
    reporter.finish(result.summary())
    return store, result
