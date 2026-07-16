"""The pipeline engine: runs an ordered list of Stages over a stream of Documents.

Design goals:
  - Deterministic order, explainable per-doc (events), measurable (metrics).
  - Fault-isolated: one bad PDF flags itself and the run continues.
  - Observable: emits live progress after each document.
  - Scalable: `workers > 1` maps documents across a thread pool (OCR shells out
    to the tesseract binary, so threads give real parallelism).
  - **Streaming**: documents are *pulled* from the walker as capacity frees up,
    with a bounded window of in-flight work. The first PDF starts processing
    immediately instead of after the whole tree has been enumerated — which on a
    120 GB tree was minutes of dead air with pause/stop doing nothing.

Cancellation is cooperative and checked *between stages* (see `run_one`), so a
Stop lands within one stage rather than one whole document — a scanned PDF is one
tesseract subprocess per page and can run for a long time.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable, Iterable, Optional, Sequence

from ..observability.events import CLOCK, record
from ..observability.progress import Reporter
from ..core.control import RunCancelled, RunControl
from ..core.interfaces import Stage
from ..core.models import Document, Severity, StageMetric


class Pipeline:
    def __init__(self, stages: Sequence[Stage], reporter: Reporter | None = None,
                 workers: int = 1, control: RunControl | None = None) -> None:
        self.stages = list(stages)
        self.reporter = reporter or Reporter()
        self.workers = max(1, workers)
        self.control = control
        self.cancelled = False   # set if the user stopped the run mid-flight
        self.discovered = 0      # PDFs the walker has produced so far
        self.skipped = 0         # of those, already in the checkpoint (a resumed run)
        self.completed = 0       # processed in this session
        self.discovery_complete = False
        self._timings: dict[str, float] = {s.name: 0.0 for s in self.stages}
        self._counts_by_stage: dict[str, int] = {s.name: 0 for s in self.stages}
        self._errors_by_stage: dict[str, int] = {s.name: 0 for s in self.stages}
        self._lock = threading.Lock()

    # ---- single document ---------------------------------------------------
    def run_one(self, doc: Document) -> Document:
        for stage in self.stages:
            # Check in *between* stages, and outside the try/except below — which
            # would otherwise swallow RunCancelled and mistake a user's Stop for a
            # stage failure. Per-stage (not per-document) is what makes Stop feel
            # immediate on a 30-page scan.
            if self.control is not None:
                self.control.gate()
            t0 = time.monotonic()
            err = False
            try:
                doc = stage.process(doc)
            except RunCancelled:
                # A Stop is not a stage failure. Stages gate internally too (the
                # OCR page loop), so this must escape the fault-isolation net
                # below rather than be recorded as a broken document.
                raise
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
    def run(self, docs: Iterable[Document],
            on_doc_done: Optional[Callable[[Document], None]] = None,
            skip_keys: Optional[set[str]] = None) -> list[Document]:
        """Process a *stream* of Documents through all stages.

        ``docs`` is consumed lazily — typically the walker generator itself, so
        discovery and processing overlap.

        ``skip_keys`` are ``Document.source_key``s already in the checkpoint. They
        are counted toward the corpus total (so a resumed run's progress is against
        the *whole* tree, not just the remainder) but never re-processed.

        ``on_doc_done`` is called with each finished Document as it completes —
        used to append it to the resumable checkpoint. It runs on the consuming
        thread (sequentially, even with workers>1), so it needs no lock.

        A cancelled doc is never tallied and never handed to ``on_doc_done``, so it
        stays out of the checkpoint and a later resume picks it up again.
        """
        CLOCK.reset()
        skip = skip_keys or set()
        counts = {"pdfs": 0, "invoices": 0, "approvals": 0, "ocr": 0,
                  "flagged": 0, "errors": 0}
        last = self.stages[-1].name if self.stages else "-"
        results: list[Document] = []
        self.reporter.start(0)   # total is not knowable until the walk finishes

        def tally(doc: Document) -> None:
            self.completed += 1
            done = self.skipped + self.completed
            counts["pdfs"] = done
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
            self.reporter.update(done, self.discovered, last, dict(counts), note)
            if on_doc_done is not None:
                on_doc_done(doc)

        it = iter(docs)

        def pull() -> Optional[Document]:
            """Next doc needing work, or None once the walker is exhausted.

            Already-checkpointed sources are counted and dropped here rather than
            pre-filtered, because with a streaming walker there is no complete list
            to filter.
            """
            for doc in it:
                self.discovered += 1
                already_done = bool(doc.source_key) and doc.source_key in skip
                if already_done:
                    self.skipped += 1
                self.reporter.discovered(self.discovered, False)
                if not already_done:
                    return doc
            self.discovery_complete = True
            self.reporter.discovered(self.discovered, True)
            return None

        window = max(self.workers * 4, 8)   # enough to keep every worker fed
        inflight: set[Future] = set()

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            try:
                while True:
                    while not self.discovery_complete and len(inflight) < window:
                        try:
                            doc = pull()
                        except RunCancelled:   # the walker itself tripped the gate
                            self.cancelled = True
                            break
                        if doc is None:
                            break
                        inflight.add(ex.submit(self.run_one, doc))
                    if self.cancelled or not inflight:
                        break

                    finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        try:
                            results.append(fut.result())
                        except RunCancelled:
                            self.cancelled = True
                            continue
                        tally(results[-1])
                    if self.cancelled:
                        break
            finally:
                # Drop queued work outright; the few already running trip the gate
                # between stages and unwind in well under a second.
                for f in inflight:
                    f.cancel()

        self.reporter.stage_timing(dict(self._timings))
        # Completion order is nondeterministic with workers>1; doc ids are assigned
        # in walk order, so this restores the documented deterministic ordering.
        results.sort(key=lambda d: d.id)
        return results

    @property
    def total(self) -> int:
        """Size of the whole corpus (only final once discovery completes)."""
        return self.discovered

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
