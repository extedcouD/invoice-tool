"""Progress reporters — how the pipeline tells you what it's doing *right now*.

Two implementations, composed via `MultiReporter`:
  - `RichReporter`   : live terminal table/bar while `scan` runs.
  - `StatusWriter`   : writes run/status.json each tick so the web dashboard
                       (or a second terminal) can poll live progress.

The pipeline only depends on the `Reporter` interface, so adding a new sink
(websocket, Prometheus, ...) later is a drop-in.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table


class Reporter:
    """No-op base; safe to use when no reporting is wanted.

    `note` (optional) describes the document that just finished — file name,
    detected type, text source, confidence, flag count — so a sink can render a
    live "what's happening right now" feed, not just a percentage.

    `discovered` is called as the walker streams PDFs in. Until it reports
    `complete=True` the corpus total is still growing, so a percentage is a lie —
    sinks should show a count ("discovering… 4,213 files") instead.
    """

    def start(self, total: int) -> None: ...
    def discovered(self, n: int, complete: bool) -> None: ...
    def update(self, done: int, total: int, stage: str, counts: dict,
               note: Optional[dict] = None) -> None: ...
    def stage_timing(self, timings: dict[str, float]) -> None: ...
    def finish(self, summary: dict) -> None: ...


class MultiReporter(Reporter):
    def __init__(self, *reporters: Reporter) -> None:
        self._rs = [r for r in reporters if r is not None]

    def start(self, total: int) -> None:
        for r in self._rs:
            r.start(total)

    def discovered(self, n: int, complete: bool) -> None:
        for r in self._rs:
            r.discovered(n, complete)

    def update(self, done: int, total: int, stage: str, counts: dict,
               note: Optional[dict] = None) -> None:
        for r in self._rs:
            r.update(done, total, stage, counts, note)

    def stage_timing(self, timings: dict[str, float]) -> None:
        for r in self._rs:
            r.stage_timing(timings)

    def finish(self, summary: dict) -> None:
        for r in self._rs:
            r.finish(summary)


class StatusWriter(Reporter):
    """Persists a small status.json snapshot for out-of-process observers.

    Also derives the ETA. Two things make that less trivial than ``total-done``:

    * A **resumed** run starts with ``done`` already at the checkpoint count, so
      throughput must be measured from work completed *this session* — otherwise
      the first document appears to have taken the whole elapsed time.
    * **Paused** time is excluded (via ``control``), so stepping away for lunch
      doesn't permanently poison the estimate.

    The rate is a session average rather than an instantaneous one: over a scan
    long enough for an ETA to matter, it's far steadier, and it doesn't lurch
    every time a scanned PDF drops into OCR.
    """

    def __init__(self, path: Path, control: object | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._total = 0
        self._control = control          # RunControl, for paused-time + paused flag
        self._t0 = time.monotonic()
        self._done0: Optional[int] = None  # `done` before this session's first doc
        self._done = 0
        self._counts: dict = {}
        self._stage = "starting"
        self._discovering = True
        self._last_discovery_write = 0.0
        # newest-first tail of finished docs, for the live activity feed
        self._recent: deque[dict] = deque(maxlen=40)

    def _elapsed(self) -> float:
        """Wall-clock this session, minus any time spent paused."""
        paused = 0.0
        if self._control is not None:
            paused = self._control.paused_seconds()
        return max(0.0, (time.monotonic() - self._t0) - paused)

    def _eta(self, done: int, total: int) -> tuple[Optional[float], Optional[float]]:
        """(seconds remaining, docs per second) — both None until measurable.

        Meaningless while the walker is still discovering, because ``total`` is
        still growing: an ETA against a partial corpus would tick *up*.
        """
        if self._done0 is None or total <= 0 or self._discovering:
            return None, None
        processed = done - self._done0
        elapsed = self._elapsed()
        if processed <= 0 or elapsed <= 0:
            return None, None
        rate = processed / elapsed
        return (max(0, total - done) / rate if rate > 0 else None), rate

    def _paused(self) -> bool:
        return bool(self._control is not None and getattr(self._control, "paused", False))

    def _write(self, payload: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)  # atomic-ish swap

    def _snapshot(self, **over) -> dict:
        eta, rate = self._eta(self._done, self._total)
        payload = {
            "state": "running",
            "done": self._done,
            "total": self._total,
            "discovering": self._discovering,
            "discovered": self._total,
            "stage": self._stage,
            "counts": self._counts,
            # No honest percentage exists until the corpus is fully enumerated.
            "pct": (round(100 * self._done / self._total, 1)
                    if self._total and not self._discovering else 0.0),
            "current": None,
            "recent": list(self._recent),
            "paused": self._paused(),
            "elapsed_s": round(self._elapsed(), 1),
            "eta_s": round(eta) if eta is not None else None,
            "rate": round(rate, 2) if rate is not None else None,
        }
        payload.update(over)
        return payload

    def start(self, total: int) -> None:
        self._total = total
        self._t0 = time.monotonic()
        self._done0 = None
        self._done = 0
        self._discovering = True
        self._write(self._snapshot(stage="starting"))

    def discovered(self, n: int, complete: bool) -> None:
        """Publish the growing file count during the walk.

        Without this, a big Drive tree shows `0 / 0` and a frozen bar for minutes
        while the walker enumerates it — the run looks hung.
        """
        self._total = n
        self._discovering = not complete
        now = time.monotonic()
        if complete or now - self._last_discovery_write >= 0.4:
            self._last_discovery_write = now
            self._write(self._snapshot(
                stage="discovering" if not complete else self._stage))

    def update(self, done: int, total: int, stage: str, counts: dict,
               note: Optional[dict] = None) -> None:
        if self._done0 is None:
            # First completion of this session: everything already counted in
            # `done` came from the checkpoint, not from time we spent.
            self._done0 = done - 1
        if note:
            self._recent.appendleft(note)
        self._done, self._total, self._stage, self._counts = done, total, stage, counts
        self._write(self._snapshot(current=(note or {}).get("file")))

    def finish(self, summary: dict) -> None:
        self._discovering = False
        self._done = self._total
        self._write(self._snapshot(state="done", stage="finished",
                                   summary=summary, pct=100.0, eta_s=0,
                                   paused=False))


class RichReporter(Reporter):
    """Live terminal progress: a bar plus a running counts table."""

    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or Console()
        self._progress: Optional[Progress] = None
        self._task = None
        self._live: Optional[Live] = None
        self._counts: dict = {}
        self._stage = ""
        self._discovering = True

    def _render(self) -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_row(self._progress)
        t = Table(show_header=True, header_style="bold cyan", box=None)
        t.add_column("metric"); t.add_column("value", justify="right")
        t.add_row("current stage",
                  "discovering files…" if self._discovering else (self._stage or "-"))
        for k in ("pdfs", "invoices", "approvals", "ocr", "flagged", "errors"):
            if k in self._counts:
                t.add_row(k, str(self._counts[k]))
        grid.add_row(t)
        return grid

    def start(self, total: int) -> None:
        self._progress = Progress(
            TextColumn("[bold blue]scanning"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=self.console,
        )
        # total=None renders an indeterminate bar while the walker is still running
        self._task = self._progress.add_task("scan", total=total or None)
        self._live = Live(self._render(), console=self.console, refresh_per_second=8)
        self._live.start()

    def discovered(self, n: int, complete: bool) -> None:
        self._discovering = not complete
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, total=n if complete else None)
        if self._live is not None:
            self._live.update(self._render())

    def update(self, done: int, total: int, stage: str, counts: dict,
               note: Optional[dict] = None) -> None:
        self._stage, self._counts = stage, counts
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, completed=done,
                                  total=None if self._discovering else total)
        if self._live is not None:
            self._live.update(self._render())

    def finish(self, summary: dict) -> None:
        if self._live is not None:
            self._live.stop()
        self.console.print(
            f"[bold green]done[/] — {summary.get('invoices', 0)} invoices, "
            f"{summary.get('flagged', 0)} flagged, "
            f"{summary.get('ocr_docs', 0)} via OCR "
            f"(avg conf {summary.get('avg_invoice_confidence', 0)})"
        )
