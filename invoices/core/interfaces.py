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


class FileSource(ABC):
    """Strategy for making a document's PDF bytes available as a local file.

    Decouples *where* a PDF lives from the extract/OCR/review code, which only ever
    needs a readable local path. The local source is a no-op passthrough of
    ``doc.path``; the seam is kept so a future remote source could download to a temp
    file in :meth:`materialize` and remove it in :meth:`cleanup`.
    """

    @abstractmethod
    def materialize(self, doc: Document) -> str:
        """Return a local filesystem path to this document's PDF bytes."""

    def cleanup(self, doc: Document, path: str) -> None:  # noqa: B027 - optional hook
        """Release anything :meth:`materialize` allocated (no-op for local)."""
