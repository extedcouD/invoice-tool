"""PDF text extraction with an OCR fallback (Strategy pattern).

`PdfTextSource` first tries the native text layer via PyMuPDF; if a page is
effectively empty (scanned image), it renders the page at OCR_DPI and runs
Tesseract. This is why ~20% image-only invoices in the corpus still parse.
"""
from __future__ import annotations

import io

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

from ..config import Settings, DEFAULTS
from ..core.interfaces import FileSource, TextSource
from ..core.models import Document
from .sources import LocalFileSource


class PdfTextSource(TextSource):
    def __init__(self, settings: Settings = DEFAULTS,
                 file_source: FileSource | None = None) -> None:
        self.s = settings
        # Where the PDF bytes come from. Local by default; a DriveFileSource
        # downloads to a temp file so the fitz.open below is unchanged.
        self.file_source = file_source or LocalFileSource()

    def _ocr_page(self, page: "fitz.Page") -> str:
        pix = page.get_pixmap(dpi=self.s.ocr_dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang=self.s.ocr_lang)

    def extract(self, doc: Document) -> tuple[str, str]:
        local = self.file_source.materialize(doc)
        try:
            with fitz.open(local) as pdf:
                doc.page_count = pdf.page_count
                native_parts: list[str] = []
                used_ocr = False
                for page in pdf:
                    txt = page.get_text() or ""
                    if len(txt.strip()) >= self.s.min_text_chars:
                        native_parts.append(txt)
                    elif self.s.ocr_enabled:
                        native_parts.append(self._ocr_page(page))
                        used_ocr = True
                    else:
                        native_parts.append(txt)
                text = "\n".join(native_parts).strip()
        finally:
            self.file_source.cleanup(doc, local)

        if not text:
            return "", "none"
        return text, ("ocr" if used_ocr else "text")
