"""Event recording — the spine of observability.

Every stage calls `record(...)` to leave an explainable breadcrumb on a
document. Timestamps are relative seconds from run start (a `RunClock`), which
keeps them meaningful without leaking wall-clock into the domain models.
"""
from __future__ import annotations

import time

from ..core.models import Document, Event, Severity


class RunClock:
    """Monotonic seconds-since-start clock, shared for a run."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 4)

    def reset(self) -> None:
        self._t0 = time.monotonic()


# A process-wide default clock; the pipeline resets it at the start of a run.
CLOCK = RunClock()


def record(
    doc: Document,
    stage: str,
    action: str,
    detail: str = "",
    severity: Severity = Severity.INFO,
    **data,
) -> Event:
    """Append an event to the document and return it."""
    ev = Event(
        stage=stage,
        action=action,
        detail=detail,
        severity=severity,
        t=CLOCK.elapsed(),
        data=data,
    )
    doc.events.append(ev)
    return ev
