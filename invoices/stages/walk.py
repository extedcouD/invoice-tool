"""Folder scraping + tolerant path parsing.

`walk()` discovers candidate PDFs under the Kotak subtree and produces seed
`Document`s with a best-effort `PathInfo`. Parsing is positional but forgiving:
each path component is *labelled* by regex rather than assumed at a fixed depth,
so an extra/missing folder level doesn't derail the whole record.
"""
from __future__ import annotations

from pathlib import Path

from ..config import (
    RE_DATE, RE_FY, RE_MONTH, Settings, DEFAULTS,
    CANDIDATE_SUFFIXES, IGNORE_FILENAMES, IGNORE_SUFFIXES,
)
from ..core.models import Document, PathInfo
from ..observability.events import record


def _parse_path(rel_parts: list[str]) -> PathInfo:
    """Label each folder component; the file's own name is excluded upstream."""
    info = PathInfo()
    labelled_idx: set[int] = set()

    for i, part in enumerate(rel_parts):
        if RE_FY.match(part):
            info.fy = part; labelled_idx.add(i)
        elif RE_MONTH.match(part):
            info.month = part; labelled_idx.add(i)
        elif RE_DATE.match(part):
            info.date_folder = part; labelled_idx.add(i)
        elif part.lower() == "payments":
            labelled_idx.add(i)
        elif part.lower() in ("kotak",):
            info.bank = part; labelled_idx.add(i)

    # Company = the deepest still-unlabelled component (the folder holding the
    # PDF, e.g. "Apex Business Consultants Pvt Ltd"). Anything else unlabelled
    # is recorded so nothing is silently dropped.
    unlabelled = [(i, p) for i, p in enumerate(rel_parts) if i not in labelled_idx]
    if unlabelled:
        info.company = unlabelled[-1][1]
        info.unmatched = [p for i, p in unlabelled[:-1]]
    return info


def _bank_from(rel_parts: list[str], scope: str) -> str | None:
    """The bank folder is the component immediately after 'Payments'."""
    for i, p in enumerate(rel_parts):
        if p.lower() == "payments" and i + 1 < len(rel_parts):
            return rel_parts[i + 1]
    # fallback: any component equal to the scoped bank name
    for p in rel_parts:
        if p.lower() == scope.lower():
            return p
    return None


def walk(root: Path, settings: Settings = DEFAULTS) -> list[Document]:
    root = Path(root)
    docs: list[Document] = []
    idx = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in IGNORE_FILENAMES:
            continue
        if path.suffix.lower() in IGNORE_SUFFIXES:
            continue
        if path.suffix.lower() not in CANDIDATE_SUFFIXES:
            continue

        rel_parts = list(path.relative_to(root).parts)
        folder_parts = rel_parts[:-1]  # exclude the filename itself

        # Scope gate: only PDFs under .../Payments/<scope>/...
        bank = _bank_from(folder_parts, settings.bank_scope)
        if not bank or bank.lower() != settings.bank_scope.lower():
            continue

        info = _parse_path(folder_parts)
        info.bank = info.bank or bank

        resolved = str(path.resolve())
        doc = Document(
            id=f"d{idx:05d}",
            path=resolved,
            filename=path.name,
            size_bytes=path.stat().st_size,
            source_key=f"local:{resolved}",   # stable id for resume skip-list
            path_info=info,
        )
        record(doc, "walk", "discovered",
               f"fy={info.fy} month={info.month} date={info.date_folder} company={info.company}",
               company=info.company, fy=info.fy)
        if info.company is None:
            doc.add_flag("no_company_folder", "could not derive company from path")
        if not (info.fy and info.month and info.date_folder):
            doc.add_flag("path_incomplete", "missing FY/month/date folder level")
        docs.append(doc)
        idx += 1

    return docs
