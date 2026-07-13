"""Cooperative pause/stop for a running scan.

The pipeline checks in with a :class:`RunControl` once per document — coarse
enough to cost nothing, fine enough that a click feels instant (a document is
~a second, even with OCR).

**Stop is not abort.** A stopped run is just an interrupted one: docs finished so
far are already in ``checkpoint.jsonl``, and the run is deliberately left marked
incomplete, so pointing the app at the same folder again resumes it through the
existing ``RunStore.find_resumable`` path. Cancelled docs are *never* checkpointed
— if they were, a resume would skip files that were never actually processed.

Paused time is tracked here so the ETA can exclude it (a 10-minute coffee break
shouldn't make the scan look 10 minutes slower).
"""
from __future__ import annotations

import threading
import time


class RunCancelled(Exception):
    """Raised inside a worker when the user stopped the run."""


class RunControl:
    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()               # set == running
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._paused_at: float | None = None
        self._paused_total: float = 0.0

    # ---- commands (from the UI thread) -------------------------------------
    def pause(self) -> None:
        with self._lock:
            if self._paused_at is None and not self._stop.is_set():
                self._paused_at = time.monotonic()
                self._resume.clear()

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
            self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        # Release anyone parked in gate() so they can raise and unwind, rather
        # than sit blocked on a pause that will never be lifted.
        self.resume()

    # ---- state (for status.json / the UI) ----------------------------------
    @property
    def paused(self) -> bool:
        return not self._resume.is_set() and not self._stop.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def paused_seconds(self) -> float:
        """Total wall-clock spent paused, including any pause still open."""
        with self._lock:
            total = self._paused_total
            if self._paused_at is not None:
                total += time.monotonic() - self._paused_at
            return total

    # ---- the worker check-in -----------------------------------------------
    def gate(self) -> None:
        """Block while paused; raise :class:`RunCancelled` if stopped."""
        if self._stop.is_set():
            raise RunCancelled()
        if not self._resume.is_set():
            self._resume.wait()
        if self._stop.is_set():   # stop may have arrived while we were parked
            raise RunCancelled()
