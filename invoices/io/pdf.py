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
from ..core.interfaces import TextSource
from ..core.models import Document


class PdfTextSource(TextSource):
    def __init__(self, settings: Settings = DEFAULTS) -> None:
        self.s = settings

    def _ocr_page(self, page: "fitz.Page") -> str:
        pix = page.get_pixmap(dpi=self.s.ocr_dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang=self.s.ocr_lang)

    def extract(self, doc: Document) -> tuple[str, str]:
        with fitz.open(doc.path) as pdf:
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

        if not text:
            return "", "none"
        return text, ("ocr" if used_ocr else "text")
