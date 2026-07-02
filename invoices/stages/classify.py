"""Classify stage: is this PDF an invoice? Content-scored, filename as tiebreak.

We never trust the filename alone (human-maintained, unreliable). Each positive
and negative signal contributes a weight; the net score and its reasons are
recorded so a human can see exactly why a doc was (mis)classified.
"""
from __future__ import annotations

from ..config import (
    FILENAME_HINT_WEIGHT, INVOICE_SIGNALS, NOT_INVOICE_SIGNALS,
    RE_FILENAME_APPROVAL, RE_FILENAME_INVOICE, Settings, DEFAULTS,
)
from ..core.interfaces import Stage
from ..core.models import Document, DocType
from ..observability.events import record


class ClassifyStage(Stage):
    name = "classify"

    def __init__(self, settings: Settings = DEFAULTS) -> None:
        self.s = settings

    def process(self, doc: Document) -> Document:
        text = doc.text or ""
        reasons: list[str] = []
        pos = neg = 0.0

        for rx, w, why in INVOICE_SIGNALS:
            if rx.search(text):
                pos += w; reasons.append(f"+{w} {why}")
        for rx, w, why in NOT_INVOICE_SIGNALS:
            if rx.search(text):
                neg += w; reasons.append(f"-{w} {why}")

        # Filename tiebreak (weak).
        fn_hint = 0.0
        if RE_FILENAME_INVOICE.match(doc.filename):
            fn_hint = FILENAME_HINT_WEIGHT; reasons.append(f"+{fn_hint} filename~invoice")
        elif RE_FILENAME_APPROVAL.match(doc.filename):
            fn_hint = -FILENAME_HINT_WEIGHT; reasons.append(f"{fn_hint} filename~approval")

        score = pos - neg + fn_hint
        doc.classify_score = round(score, 2)

        # Decide type.
        if any(rx.search(text) for rx, _, _ in NOT_INVOICE_SIGNALS[:1]):  # approval form marker
            doc.doc_type = DocType.APPROVAL
        elif any(rx.search(text) for rx, _, why in NOT_INVOICE_SIGNALS if "gstr" in why.lower()):
            doc.doc_type = DocType.GSTR2A
        elif score >= self.s.invoice_score_threshold:
            doc.doc_type = DocType.INVOICE
        elif not text:
            doc.doc_type = DocType.UNKNOWN
        else:
            doc.doc_type = DocType.OTHER

        # If the filename says invoice but content disagrees, that's exactly the
        # kind of unreliable-structure case we must surface.
        if RE_FILENAME_INVOICE.match(doc.filename) and doc.doc_type != DocType.INVOICE:
            doc.add_flag("filename_content_mismatch",
                         f"filename looks like invoice but classified {doc.doc_type.value} "
                         f"(score {score:.1f})")
        if RE_FILENAME_APPROVAL.match(doc.filename) and doc.doc_type == DocType.INVOICE:
            doc.add_flag("filename_content_mismatch",
                         "filename looks like approval but content scored as invoice")

        record(doc, self.name, f"type={doc.doc_type.value}",
               f"score={score:.1f} :: {'; '.join(reasons) or 'no signals'}",
               score=score, doc_type=doc.doc_type.value)
        return doc
