"""The pipeline engine: runs an ordered list of Stages over a list of Documents.

Design goals:
  - Deterministic order, explainable per-doc (events), measurable (metrics).
  - Fault-isolated: one bad PDF flags itself and the run continues.
  - Observable: emits live progress after each document.
  - Scalable: `workers > 1` maps documents across a process pool.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

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
    def run(self, docs: list[Document]) -> list[Document]:
        CLOCK.reset()
        total = len(docs)
        self.reporter.start(total)
        counts = {"pdfs": 0, "invoices": 0, "approvals": 0, "ocr": 0,
                  "flagged": 0, "errors": 0}
        last = self.stages[-1].name if self.stages else "-"

        def tally(doc: Document, done: int) -> None:
            counts["pdfs"] = done
            counts["invoices"] += int(doc.doc_type.value == "invoice")
            counts["approvals"] += int(doc.doc_type.value == "approval")
            counts["ocr"] += int(doc.source.value == "ocr")
            counts["flagged"] += int(doc.needs_review)
            counts["errors"] += int(bool(doc.error))
            self.reporter.update(done, total, last, dict(counts))

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
