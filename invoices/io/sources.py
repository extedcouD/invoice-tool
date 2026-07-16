"""Concrete :class:`FileSource` strategies.

`LocalFileSource` is the only source: the PDF already lives on the local filesystem
at ``doc.path``. The :class:`FileSource` seam is kept so a future remote source could
download to a temp file and let the extract/OCR/review code treat every document as a
local file.
"""
from __future__ import annotations

from ..core.interfaces import FileSource
from ..core.models import Document


class LocalFileSource(FileSource):
    """Passthrough: the document's bytes are already at ``doc.path``."""

    def materialize(self, doc: Document) -> str:
        return doc.path

    def cleanup(self, doc: Document, path: str) -> None:
        # Nothing to release — the file is the user's original, not a temp copy.
        return None
