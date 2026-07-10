"""The pipeline engine: runs an ordered list of Stages over a list of Documents.

Design goals:
  - Deterministic order, explainable per-doc (events), measurable (metrics).
  - Fault-isolated: one bad PDF flags itself and the run continues.
  - Observable: emits live progress after each document.
  - Scalable: `workers > 1` maps documents across a thread pool (OCR shells out
    to the tesseract binary, so threads give real parallelism).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Sequence

from ..observability.events import CLOCK, record
from ..observability.progress import Reporter
from ..core.interfaces import Stage
from ..core.models import Document, Severity, StageMetric


class Pipeline:
    def __init__(self, stages: Sequence[Stage], reporter: Reporter | None = None,
                 workers: int = 1) -> None:
        self.stages = list(stages)
        self.reporter = reporter or Reporter()
        self.workers = max(1, workers)
        self._timings: dict[str, float] = {s.name: 0.0 for s in self.stages}
        self._counts_by_stage: dict[str, int] = {s.name: 0 for s in self.stages}
        self._errors_by_stage: dict[str, int] = {s.name: 0 for s in self.stages}
        self._lock = threading.Lock()

    # ---- single document ---------------------------------------------------
    def run_one(self, doc: Document) -> Document:
        for stage in self.stages:
            t0 = time.monotonic()
            err = False
            try:
                doc = stage.process(doc)
            except Exception as exc:  # fault isolation — never kill the whole run
                err = True
                doc.error = f"{stage.name}: {exc!r}"
                doc.add_flag("stage_error", f"{stage.name} failed: {exc}", Severity.ERROR)
                record(doc, stage.name, "error", str(exc), Severity.ERROR)
            finally:
                # guarded because worker threads share these accumulators
                with self._lock:
                    self._timings[stage.name] += time.monotonic() - t0
                    self._counts_by_stage[stage.name] += 1
                    if err:
                        self._errors_by_stage[stage.name] += 1
        return doc

    # ---- whole corpus ------------------------------------------------------
    def run(self, docs: list[Document],
            on_doc_done: Optional[Callable[[Document], None]] = None,
            start_done: int = 0, total: Optional[int] = None) -> list[Document]:
        """Process ``docs`` through all stages.

        ``on_doc_done`` (if given) is called with each finished Document as it
        completes — used to append it to the resumable checkpoint. It runs on the
        consuming thread (sequentially, even with workers>1), so it needs no lock.

        ``start_done``/``total`` let a resumed run report progress against the
        *whole* corpus (already-done + this batch), not just this batch.
        """
        CLOCK.reset()
        total = len(docs) if total is None else total
        self.reporter.start(total)
        counts = {"pdfs": start_done, "invoices": 0, "approvals": 0, "ocr": 0,
                  "flagged": 0, "errors": 0}
        last = self.stages[-1].name if self.stages else "-"

        def tally(doc: Document, done: int) -> None:
            counts["pdfs"] = start_done + done
            counts["invoices"] += int(doc.doc_type.value == "invoice")
            counts["approvals"] += int(doc.doc_type.value == "approval")
            counts["ocr"] += int(doc.source.value == "ocr")
            counts["flagged"] += int(doc.needs_review)
            counts["errors"] += int(bool(doc.error))
            # a compact record of the doc that just finished — feeds the live
            # "what's happening" activity feed in the desktop/web UI
            note = {
                "file": doc.filename,
                "type": doc.doc_type.value,
                "source": doc.source.value,
                "conf": round(doc.confidence, 2),
                "flags": len(doc.flags),
                "error": bool(doc.error),
            }
            self.reporter.update(start_done + done, total, last, dict(counts), note)
            if on_doc_done is not None:
                on_doc_done(doc)

        results: list[Document] = list(docs)
        if self.workers == 1:
            for i, doc in enumerate(docs):
                results[i] = self.run_one(doc)
                tally(results[i], i + 1)
        else:
            # OCR shells out to the tesseract binary, so threads give real
            # parallelism. Results kept in input order for determinism.
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                fut_to_i = {ex.submit(self.run_one, d): i for i, d in enumerate(docs)}
                done = 0
                for fut in as_completed(fut_to_i):
                    i = fut_to_i[fut]
                    results[i] = fut.result()
                    done += 1
                    tally(results[i], done)

        self.reporter.stage_timing(dict(self._timings))
        return results

    def metrics(self) -> list[StageMetric]:
        return [
            StageMetric(
                stage=s.name,
                docs_in=self._counts_by_stage[s.name],
                docs_out=self._counts_by_stage[s.name],
                errors=self._errors_by_stage[s.name],
                seconds=round(self._timings[s.name], 4),
            )
            for s in self.stages
        ]
