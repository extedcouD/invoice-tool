"""Concrete :class:`FileSource` strategies.

`LocalFileSource` is the default and preserves the tool's original behavior
(the PDF already lives on the local filesystem at ``doc.path``). Remote sources
(e.g. `DriveFileSource` in :mod:`invoices.io.drive`) download to a temp file so
the extract/OCR/review code can treat every document as a local file.
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
