"""Extract stage: fill `doc.text` and `doc.source` via the TextSource strategy."""
from __future__ import annotations

from ..core.interfaces import Stage, TextSource
from ..core.models import Document, TextSourceKind
from ..observability.events import record


class ExtractStage(Stage):
    name = "extract"

    def __init__(self, source: TextSource) -> None:
        self.source = source

    def process(self, doc: Document) -> Document:
        text, kind = self.source.extract(doc)
        doc.text = text
        doc.source = TextSourceKind(kind)
        if kind == "none":
            doc.add_flag("no_text", "no extractable text even after OCR")
            record(doc, self.name, "no_text",
                   f"pages={doc.page_count}", severity="warn")
        else:
            record(doc, self.name, "extracted",
                   f"{kind}, {len(text)} chars, {doc.page_count} page(s)",
                   source=kind, chars=len(text))
        return doc
