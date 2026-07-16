"""Find a PDF the way a human filed it: by its folder path, not by its fields.

The matcher keys on (GSTIN, invoice number). When a B2B row finds no PDF it is
usually because *one of those two reads is wrong* — so the reviewer cannot search
for the file by the very fields that failed. What they do know is where the file
lives: "it's under Apex, August, the 15th". This module is that second way in.

Two operations, both pure over the run's Documents (no I/O, no scan), so the
review UI can call them on every keystroke:

* :meth:`PathIndex.search` — typo-tolerant search over the full relative path
  *and* the parsed fields. Powers the autocomplete: "apx aug 003" finds
  ``Apex Business Consultants/Aug-2022/15-08-2022/APEX-22-23-003.pdf``.
* :meth:`PathIndex.browse` — one level of the original folder tree, so the
  reviewer can navigate to the file instead of describing it.

Both deliberately range over **every** PDF, not just the detected invoices. A row
with no PDF very often points at a file the classifier scored as an approval or
missed entirely; hiding those is hiding the answer. Hits carry ``is_invoice`` so
the UI can say so, and binding one promotes it (see ``web/app.py::bind_row``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from rapidfuzz import fuzz, process

from ..core.models import Document

# Below this a document is noise, not a near-miss. It is a *mean* over the query's
# tokens (a token the document doesn't have scores 0), so 62 means "most of what
# you typed is in this path".
SCORE_CUTOFF = 62.0

# What counts as the same word despite a typo. 'apx'/'apex' = 85.7, 'busines'/
# 'business' = 93.3, so one slip in a short word still clears this — while
# '003'/'004' (66.7) correctly does not.
TOKEN_SCORE = 75.0

# Tokens this short ('22', '08') must match exactly: at two characters, fuzzy
# similarity is meaningless and everything matches everything.
SHORT_TOKEN = 2


def rel_parts(doc: Document, root: str | None = None) -> list[str]:
    """The doc's path relative to the scan root, as folder components + filename.

    A local run stores an absolute path. Falls back to the raw parts if the path
    doesn't sit under ``root`` (a run reopened from a moved folder, say) — a degraded
    breadcrumb beats an exception.
    """
    p = doc.path or doc.filename
    path = Path(p)
    if root:
        for base in (Path(root), Path(root).resolve()):
            try:
                return list(path.relative_to(base).parts)
            except ValueError:
                continue
    return list(path.parts)


def _norm(s: str) -> str:
    """Fold a path or a query into space-separated alphanumeric tokens.

    Separators are noise here — a user typing "apex 22 23 003" must reach
    ``APEX/22-23/003.pdf``, and one typing "15-08" must reach ``15-08-2022``.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


@dataclass
class Hit:
    """One candidate PDF, with the folder trail that explains why it surfaced."""
    doc: Document
    score: float
    rel: list[str] = field(default_factory=list)

    @property
    def folder(self) -> str:
        return "/".join(self.rel[:-1])

    @property
    def is_invoice(self) -> bool:
        return self.doc.is_invoice


class PathIndex:
    """Precomputed haystacks for one run's documents.

    Built once per (run, document count) and reused across requests: the search
    is a typeahead, so it runs on every keystroke over what may be tens of
    thousands of PDFs. `rapidfuzz.process.extract` does the scan in C over the
    prebuilt strings, which is the only reason that is affordable.
    """

    def __init__(self, docs: Iterable[Document], root: str | None = None):
        self.docs: list[Document] = list(docs)
        self.root = root or ""
        self.rel: list[list[str]] = [rel_parts(d, root) for d in self.docs]
        self._hay: list[str] = [_norm(self._searchable(d, r))
                                for d, r in zip(self.docs, self.rel)]
        # Inverted index: token -> the documents whose path/fields contain it.
        # Search fuzzy-matches each query word against the *vocabulary* (small,
        # and shared — every August PDF contributes one "aug"), then walks the
        # postings. Scoring each document directly would mean re-fuzzing tens of
        # thousands of long strings on every keystroke.
        self._postings: dict[str, list[int]] = {}
        for i, hay in enumerate(self._hay):
            for tok in set(hay.split()):
                self._postings.setdefault(tok, []).append(i)
        self._vocab: list[str] = list(self._postings)
        # A stable filing order for the flat "Browse PDFs" list, computed once.
        self._order: list[int] = sorted(
            range(len(self.docs)), key=lambda i: "/".join(self.rel[i]).lower())
        # Full-text haystacks, built lazily on the first content search only:
        # unlike the ranked `search`, the flat browse greps the actual PDF text,
        # which is far larger than the path+fields and not worth folding in until
        # someone asks for it.
        self._content_hay: Optional[list[str]] = None

    @staticmethod
    def _searchable(d: Document, rel: Sequence[str]) -> str:
        """Path first — it is what the reviewer knows — then the parsed fields.

        The fields stay in the haystack because a search box that only understood
        paths would be a *downgrade* for the reviewer who does remember the
        invoice number.
        """
        f = d.fields
        return " ".join(str(x) for x in (
            *rel, d.path_info.company, d.path_info.month, d.path_info.date_folder,
            d.path_info.fy, f.invoice_id, f.invoice_no_content, f.vendor_name_pdf,
            f.vendor_gstin,
        ) if x)

    # ---- search ----------------------------------------------------------
    def _token_hits(self, qt: str) -> dict[int, float]:
        """doc index -> how well one query word matches that document's best word.

        Deliberately *not* `fuzz.WRatio` over the whole path. WRatio folds in
        `partial_token_set_ratio`, which returns 100 as soon as any single word
        overlaps — and every PDF in the tree shares words like "2022". It scored
        an unrelated Zephyr invoice 85 for the query "apex aug 003".
        """
        if len(qt) <= SHORT_TOKEN:
            matched = [(qt, 100.0)] if qt in self._postings else []
        else:
            best: dict[int, float] = {}
            for _t, s, k in process.extract(qt, self._vocab, scorer=fuzz.ratio,
                                            limit=None, score_cutoff=TOKEN_SCORE):
                best[k] = max(best.get(k, 0.0), float(s))
            # A word still being typed ("consul" -> "consultants") is a substring,
            # not a near-miss; ratio alone would score it 70 and drop it. Shaded
            # slightly so a completed word always outranks a prefix of it.
            for _t, s, k in process.extract(qt, self._vocab,
                                            scorer=fuzz.partial_ratio,
                                            limit=None, score_cutoff=TOKEN_SCORE):
                best[k] = max(best.get(k, 0.0), float(s) * 0.95)
            matched = [(self._vocab[k], s) for k, s in best.items()]

        out: dict[int, float] = {}
        for tok, s in matched:
            for i in self._postings[tok]:
                if s > out.get(i, 0.0):
                    out[i] = s
        return out

    def search(self, q: str, limit: int = 20,
               invoices_only: bool = False) -> list[Hit]:
        """Fuzzy-rank documents against a free-text path/field query, best first.

        A document scores the *mean* of how well it answers each word typed, with
        a missing word scoring zero. That is what makes multi-word queries behave:
        "apex aug 003" ranks the one PDF carrying all three above the sibling
        invoice 004 in the same folder, which answers two of the three — while
        still listing 004, because the reviewer may have misremembered the number.
        """
        qn = _norm(q)
        qts = qn.split()
        if not qts:
            return []

        per = [self._token_hits(t) for t in qts]
        totals: dict[int, float] = {}
        for hits_for_token in per:
            for i, s in hits_for_token.items():
                totals[i] = totals.get(i, 0.0) + s

        hits: list[Hit] = []
        for i, total in totals.items():
            d = self.docs[i]
            if invoices_only and not d.is_invoice:
                continue
            score = total / len(qts)
            if score < SCORE_CUTOFF:
                continue
            if qn in self._hay[i]:          # a literal fragment beats a lookalike
                score = min(100.0, score + 12.0)
            hits.append(Hit(doc=d, score=round(score, 1), rel=self.rel[i]))

        # Detected invoices first at equal score: they bind without a promotion.
        hits.sort(key=lambda h: (-h.score, not h.is_invoice, h.doc.id))
        return hits[:limit]

    # ---- flat, paginated listing (content-searchable) --------------------
    def _content_haystacks(self) -> list[str]:
        """Lowercased ``path + filename + extracted text`` per document, once.

        This is the only place a PDF's *full text* enters a haystack. The ranked
        :meth:`search` deliberately stays on paths + parsed fields for speed, but
        the flat browse lets a reviewer grep the body — so a term that only ever
        appears inside the PDF (a PO number, a bill-to name) still finds it.
        """
        if self._content_hay is None:
            self._content_hay = [
                (" ".join(self.rel[i]) + " " + (self.docs[i].text or "")).lower()
                for i in range(len(self.docs))
            ]
        return self._content_hay

    def flat(self, q: str = "", page: int = 1, page_size: int = 50) -> dict:
        """One page of *every* PDF in the run, filtered by a plain substring query.

        Unlike :meth:`search` this is exhaustive and paginated rather than a
        fuzzy top-N: it is the "show me all of them, and let me grep the text"
        view. ``q`` is split on whitespace and every term must appear (as a
        substring) in the file's path, name or extracted text — so it doubles as
        a filename, path *and* content search from one box.
        """
        terms = [t for t in q.lower().split() if t]
        if terms:
            hay = self._content_haystacks()
            order = [i for i in self._order if all(t in hay[i] for t in terms)]
        else:
            order = self._order
        total = len(order)
        page_size = max(1, page_size)
        pages = (total + page_size - 1) // page_size
        page = min(max(1, page), max(1, pages))
        start = (page - 1) * page_size
        return {
            "total": total, "page": page, "pages": pages, "page_size": page_size,
            "files": [Hit(doc=self.docs[i], score=0.0, rel=self.rel[i])
                      for i in order[start:start + page_size]],
        }

    # ---- browse ----------------------------------------------------------
    def browse(self, prefix: Sequence[str] = ()) -> dict:
        """One level of the original tree under ``prefix``.

        Returns the sub-folders (each with the number of PDFs *anywhere* beneath
        it, so an empty branch is visibly empty before you click into it) and the
        PDFs filed directly at this level.
        """
        pre = [p for p in prefix if p]
        depth = len(pre)
        folders: dict[str, dict] = {}
        files: list[Hit] = []

        for d, rel in zip(self.docs, self.rel):
            if list(rel[:depth]) != pre:
                continue
            rest = rel[depth:]
            if len(rest) <= 1:                      # the file itself sits here
                files.append(Hit(doc=d, score=0.0, rel=rel))
                continue
            name = rest[0]
            f = folders.setdefault(name, {"name": name, "path": pre + [name],
                                          "pdfs": 0, "invoices": 0})
            f["pdfs"] += 1
            if d.is_invoice:
                f["invoices"] += 1

        files.sort(key=lambda h: h.doc.filename.lower())
        return {
            "prefix": pre,
            "crumbs": [{"name": n, "path": pre[:i + 1]} for i, n in enumerate(pre)],
            "folders": sorted(folders.values(), key=lambda f: f["name"].lower()),
            "files": files,
        }

    # ---- where to start browsing -----------------------------------------
    def suggest_folders(self, supplier: Optional[str], limit: int = 3) -> list[dict]:
        """Top-level folders whose name resembles the row's supplier.

        The folder tree is filed by *company*, and a B2B row names its supplier —
        so the reviewer almost never has to start browsing from the root. This is
        the auto-suggestion that makes navigation a couple of clicks instead of a
        descent.
        """
        if not supplier:
            return []
        from . import vendor

        want = vendor.normalize(supplier)
        if not want:
            return []
        top = self.browse(()).get("folders", [])
        scored = [(f, fuzz.token_set_ratio(want, vendor.normalize(f["name"])))
                  for f in top]
        return [dict(f, score=round(float(s), 1))
                for f, s in sorted(scored, key=lambda t: -t[1]) if s >= 70][:limit]
