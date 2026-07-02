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
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table


class Reporter:
    """No-op base; safe to use when no reporting is wanted."""

    def start(self, total: int) -> None: ...
    def update(self, done: int, total: int, stage: str, counts: dict) -> None: ...
    def stage_timing(self, timings: dict[str, float]) -> None: ...
    def finish(self, summary: dict) -> None: ...


class MultiReporter(Reporter):
    def __init__(self, *reporters: Reporter) -> None:
        self._rs = [r for r in reporters if r is not None]

    def start(self, total: int) -> None:
        for r in self._rs:
            r.start(total)

    def update(self, done: int, total: int, stage: str, counts: dict) -> None:
        for r in self._rs:
            r.update(done, total, stage, counts)

    def stage_timing(self, timings: dict[str, float]) -> None:
        for r in self._rs:
            r.stage_timing(timings)

    def finish(self, summary: dict) -> None:
        for r in self._rs:
            r.finish(summary)


class StatusWriter(Reporter):
    """Persists a small status.json snapshot for out-of-process observers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._total = 0

    def _write(self, payload: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)  # atomic-ish swap

    def start(self, total: int) -> None:
        self._total = total
        self._write({"state": "running", "done": 0, "total": total,
                     "stage": "starting", "counts": {}})

    def update(self, done: int, total: int, stage: str, counts: dict) -> None:
        self._write({"state": "running", "done": done, "total": total,
                     "stage": stage, "counts": counts,
                     "pct": round(100 * done / total, 1) if total else 0.0})

    def finish(self, summary: dict) -> None:
        self._write({"state": "done", "done": self._total, "total": self._total,
                     "stage": "finished", "summary": summary})


class RichReporter(Reporter):
    """Live terminal progress: a bar plus a running counts table."""

    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or Console()
        self._progress: Optional[Progress] = None
        self._task = None
        self._live: Optional[Live] = None
        self._counts: dict = {}
        self._stage = ""

    def _render(self) -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_row(self._progress)
        t = Table(show_header=True, header_style="bold cyan", box=None)
        t.add_column("metric"); t.add_column("value", justify="right")
        t.add_row("current stage", self._stage or "-")
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
        self._task = self._progress.add_task("scan", total=total)
        self._live = Live(self._render(), console=self.console, refresh_per_second=8)
        self._live.start()

    def update(self, done: int, total: int, stage: str, counts: dict) -> None:
        self._stage, self._counts = stage, counts
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, completed=done, total=total)
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
