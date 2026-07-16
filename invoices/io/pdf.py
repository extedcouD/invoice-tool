"""PDF text extraction with an OCR fallback (Strategy pattern).

`PdfTextSource` first tries the native text layer via PyMuPDF; if a page is
effectively empty (scanned image), it renders the page at OCR_DPI and runs
Tesseract. This is why ~20% image-only invoices in the corpus still parse.

OCR itself is a **two-rung escalation ladder**, because a flatbed scan and a
phone photo of the same invoice are different problems:

  1. cheap pass  — render at OCR_DPI (greyscale), one Tesseract call with
                   `image_to_data` so we get per-word confidence for free.
  2. photo pass  — fires *only* when the cheap pass comes back below
                   `ocr_photo_min_confidence`: pull the embedded image at native
                   resolution, auto-orient (OSD), flatten uneven lighting and
                   binarize, then OCR again. We keep whichever pass scored higher.

The photo rung is where the cost is, so it is gated on the cheap rung's
confidence: a clean-scan corpus never pays for it. Everything the photo pass
does is wrapped so a preprocessing failure degrades to the cheap result rather
than crashing the page.
"""
from __future__ import annotations

import io

import fitz  # PyMuPDF
import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

from ..config import Settings, DEFAULTS
from ..core.control import RunControl
from ..core.interfaces import FileSource, TextSource
from ..core.models import Document
from ..observability.events import record
from .sources import LocalFileSource


def _assemble(data: dict) -> tuple[str, float]:
    """Reconstruct text + mean word-confidence from a `image_to_data` DICT.

    Tesseract reports one row per token; conf is -1 for structural rows. We keep
    real words, group them back into lines by (block, paragraph, line) so the
    downstream regexes still see line structure, and average the >=0 confidences.
    """
    words = data.get("text", [])
    confs = data.get("conf", [])
    lines: dict[tuple, list[str]] = {}
    order: list[tuple] = []
    good: list[float] = []
    for i, w in enumerate(words):
        if not w or not w.strip():
            continue
        try:
            c = float(confs[i])
        except (TypeError, ValueError):
            c = -1.0
        if c >= 0:
            good.append(c)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(w)
    text = "\n".join(" ".join(lines[k]) for k in order)
    mean = sum(good) / len(good) if good else 0.0
    return text, mean


def _otsu_threshold(arr: np.ndarray) -> float:
    """Global Otsu threshold over a 0-255 array (used post background-flatten)."""
    hist, _ = np.histogram(arr, bins=256, range=(0, 255))
    total = arr.size
    if total == 0:
        return 127.0
    levels = np.arange(256)
    sum_all = float((levels * hist).sum())
    w_b = 0.0
    sum_b = 0.0
    best_var = -1.0
    threshold = 127.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var = var
            threshold = float(t)
    return threshold


def write_page_pdf(src: str, page_index: int, dest) -> None:
    """Write a single page of ``src`` as a standalone 1-page PDF at ``dest``.

    Used when copying a page-invoice (one page of a bundled multi-invoice PDF) to a
    deliverable, so the flat linked copy and the review copy are just that invoice's
    page rather than the whole multi-invoice file.
    """
    with fitz.open(src) as s, fitz.open() as out:
        out.insert_pdf(s, from_page=page_index, to_page=page_index)
        out.save(str(dest))


class PdfTextSource(TextSource):
    def __init__(self, settings: Settings = DEFAULTS,
                 file_source: FileSource | None = None,
                 control: RunControl | None = None) -> None:
        self.s = settings
        # Where the PDF bytes come from — the local filesystem, via the FileSource
        # seam so fitz.open below never needs to know the source.
        self.file_source = file_source or LocalFileSource()
        # Extraction is by far the longest stage — one tesseract subprocess per
        # scanned page — so it checks in per page rather than making the user wait
        # out a whole 30-page document after clicking Stop.
        self.control = control

    # -- Tesseract -------------------------------------------------------- #
    def _run_tess(self, img: "Image.Image") -> tuple[str, float]:
        out = pytesseract.image_to_data(
            img, lang=self.s.ocr_lang, config=self.s.ocr_config,
            output_type=pytesseract.Output.DICT,
        )
        return _assemble(out)

    def _ocr_cheap(self, page: "fitz.Page") -> tuple[str, float]:
        # Greyscale pixmap: one channel instead of three — smaller to move into
        # Tesseract, and Tesseract greyscales internally anyway.
        pix = page.get_pixmap(dpi=self.s.ocr_dpi, colorspace=fitz.csGRAY)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return self._run_tess(img)

    # -- Photo escalation ------------------------------------------------- #
    def _photo_image(self, pdf: "fitz.Document", page: "fitz.Page") -> "Image.Image":
        """Best source pixels for the photo pass.

        Prefer the embedded image at *native* resolution — rasterising the page
        at a fixed DPI would throw away the phone's megapixels — but only when a
        single image covers most of the page (i.e. it really is a photo). Cap the
        long edge so Tesseract time stays bounded.
        """
        img: Image.Image | None = None
        try:
            page_area = abs(page.rect.width * page.rect.height) or 1.0
            best_xref, best_area = 0, 0.0
            for info in page.get_image_info(xrefs=True):
                x0, y0, x1, y1 = info["bbox"]
                area = abs((x1 - x0) * (y1 - y0))
                if area > best_area and info.get("xref"):
                    best_area, best_xref = area, info["xref"]
            if best_xref and best_area / page_area > 0.5:
                raw = pdf.extract_image(best_xref)
                img = Image.open(io.BytesIO(raw["image"]))
        except Exception:
            img = None
        if img is None:
            pix = page.get_pixmap(dpi=self.s.ocr_dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
        return self._downscale(img.convert("RGB"))

    def _downscale(self, img: "Image.Image") -> "Image.Image":
        long_edge = max(img.size)
        cap = self.s.ocr_photo_max_dim
        if long_edge > cap:
            scale = cap / long_edge
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.LANCZOS,
            )
        return img

    def _orient(self, img: "Image.Image") -> tuple["Image.Image", int]:
        """Auto-rotate to upright via Tesseract OSD (the `osd` traineddata)."""
        try:
            osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
            rot = int(osd.get("rotate", 0)) % 360
        except Exception:
            rot = 0
        if rot:
            img = img.rotate(-rot, expand=True)  # PIL is CCW-positive; OSD 'rotate' is CW-to-upright
        return img, rot

    def _binarize(self, img: "Image.Image") -> "Image.Image":
        """Flatten uneven lighting, then binarize.

        The camera-shot killer is a lighting gradient: Tesseract's own global
        threshold turns the dark corner into a black blob. We divide the image by
        a heavily-blurred copy of itself (a background estimate) so illumination
        becomes uniform, *then* an Otsu threshold works page-wide.
        """
        g = ImageOps.autocontrast(ImageOps.grayscale(img))
        bg = g.filter(ImageFilter.GaussianBlur(radius=self.s.ocr_photo_bg_radius))
        arr = np.asarray(g, dtype=np.float32)
        bgarr = np.asarray(bg, dtype=np.float32) + 1.0
        norm = np.clip(arr / bgarr * 255.0, 0, 255)
        thr = _otsu_threshold(norm)
        binary = np.where(norm > thr, 255, 0).astype(np.uint8)
        return Image.fromarray(binary)

    def _ocr_photo(self, pdf: "fitz.Document", page: "fitz.Page") -> tuple[str, float, str]:
        img = self._photo_image(pdf, page)
        img, rot = self._orient(img)
        img = self._binarize(img)
        text, conf = self._run_tess(img)
        how = "photo-preproc" + (f", rot {rot}°" if rot else "")
        return text, conf, how

    # -- Orchestration ---------------------------------------------------- #
    def _ocr(self, doc: Document, pdf: "fitz.Document", page: "fitz.Page") -> tuple[str, float]:
        """OCR one image page through the escalation ladder."""
        text, conf = self._ocr_cheap(page)
        pno = page.number + 1
        if self.s.ocr_photo_enabled and conf < self.s.ocr_photo_min_confidence:
            if self.control is not None:
                self.control.gate()  # the photo pass is more Tesseract calls — stay stoppable
            try:
                hi_text, hi_conf, how = self._ocr_photo(pdf, page)
            except Exception as exc:  # preprocessing must never crash a page
                record(doc, "extract", "ocr_photo_error",
                       f"page {pno}: {type(exc).__name__}", severity="warn")
                hi_text, hi_conf = text, conf
            if hi_conf > conf:
                record(doc, "extract", "ocr_photo",
                       f"page {pno}: {how}, conf {conf:.0f}->{hi_conf:.0f}",
                       conf_before=round(conf, 1), conf_after=round(hi_conf, 1))
                text, conf = hi_text, hi_conf
            else:
                record(doc, "extract", "ocr_photo_nogain",
                       f"page {pno}: no improvement (conf {conf:.0f})", severity="info")
        return text, conf

    def extract(self, doc: Document) -> tuple[str, str]:
        local = self.file_source.materialize(doc)
        low_conf_pages = 0
        try:
            with fitz.open(local) as pdf:
                doc.page_count = pdf.page_count
                # A page-invoice reads ONLY its own page; a whole-file doc reads and
                # concatenates every page, exactly as before.
                if doc.page_index is not None:
                    if doc.page_index >= pdf.page_count:
                        doc.add_flag(
                            "page_out_of_range",
                            f"page {doc.page_index + 1} of {pdf.page_count} "
                            "(source file changed since discovery?)")
                        return "", "none"
                    pages = [pdf[doc.page_index]]
                else:
                    pages = list(pdf)
                native_parts: list[str] = []
                used_ocr = False
                for page in pages:
                    txt = page.get_text() or ""
                    if len(txt.strip()) >= self.s.min_text_chars:
                        native_parts.append(txt)
                    elif self.s.ocr_enabled:
                        if self.control is not None:
                            self.control.gate()
                        text, conf = self._ocr(doc, pdf, page)
                        native_parts.append(text)
                        used_ocr = True
                        if conf < self.s.ocr_photo_min_confidence:
                            low_conf_pages += 1
                    else:
                        native_parts.append(txt)
                text = "\n".join(native_parts).strip()
        finally:
            self.file_source.cleanup(doc, local)

        if low_conf_pages:
            doc.add_flag("low_ocr_confidence",
                         f"{low_conf_pages} page(s) OCR'd below the confidence floor")

        if not text:
            return "", "none"
        return text, ("ocr" if used_ocr else "text")
