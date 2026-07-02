"""Abstractions the engine depends on. Concrete stages/readers implement these,
so they can be swapped, reordered, or unit-tested in isolation (DI-friendly).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import Document


class Stage(ABC):
    """A pipeline step. Receives a Document, mutates/enriches it, returns it.

    Stages must be side-effect-light w.r.t. shared state so the engine can map
    them over documents in parallel. Recording Events on the doc is expected.
    """

    #: short, stable name used in metrics and the event trail
    name: str = "stage"

    @abstractmethod
    def process(self, doc: Document) -> Document: ...

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Stage {self.name}>"


class TextSource(ABC):
    """Strategy for turning a PDF into text (native text layer vs OCR)."""

    @abstractmethod
    def extract(self, doc: Document) -> tuple[str, str]:
        """Return (text, source_kind). source_kind in {'text','ocr','none'}."""
