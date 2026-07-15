"""Folder scraping + tolerant path parsing.

`walk()` discovers candidate PDFs under the Kotak subtree and produces seed
`Document`s with a best-effort `PathInfo`. Parsing is positional but forgiving:
each path component is *labelled* by regex rather than assumed at a fixed depth,
so an extra/missing folder level doesn't derail the whole record.

It is a **generator**, and it checks in with the :class:`RunControl` as it goes:
the pipeline pulls documents from it lazily, so the first PDF is processed while
the rest of the tree is still being enumerated, and Stop works *during* discovery
(on a 120 GB tree that phase alone runs for minutes).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterator, Optional

from ..config import (
    RE_DATE, RE_FY, RE_MONTH, Settings, DEFAULTS,
    CANDIDATE_SUFFIXES, IGNORE_FILENAMES, IGNORE_SUFFIXES,
)
from ..core.control import RunControl
from ..core.models import Document, PathInfo
from ..observability.events import record


def doc_id_for(source_key: str) -> str:
    """A stable id derived from the source, not from walk position.

    An index-based id (`d00007`) is only unique within one enumeration: on a
    resumed run over a tree that has since gained or lost a file, a freshly
    walked document can collide with a different, already-checkpointed one. A
    content-free hash of the source key is stable across runs and can't collide.
    """
    return "d" + hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:10]


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


def norm_fy(name: str | None) -> str:
    """Compare FY folder names ignoring case and spacing: 'FY 22-23' == 'fy22 - 23'.

    RE_FY accepts all three spellings, so an exact `==` would silently prune the
    whole tree for someone who typed `--fy FY22-23` at a folder named "FY 22-23" —
    a clean, successful, zero-invoice run, which looks exactly like a broken tool.
    """
    return "".join((name or "").split()).upper()


def keep_fy_dir(name: str, fy_scope: str | None) -> bool:
    """Descend into this folder? Only the scoped year, when one is set.

    Keyed on the *shape* (RE_FY) at any depth rather than on a fixed depth, so a
    tree that nests its years still prunes correctly and a tree with no FY level is
    left alone.
    """
    if not fy_scope or not RE_FY.match(name):
        return True
    return norm_fy(name) == norm_fy(fy_scope)


def list_fy_folders(root: Path | str) -> list[str]:
    """The FY folder names directly under ``root`` — the year picker's options."""
    try:
        return sorted(p.name for p in Path(root).iterdir()
                      if p.is_dir() and RE_FY.match(p.name))
    except OSError:
        return []


def seed_document(source_key: str, path: str, filename: str, size_bytes: int,
                  info: PathInfo, **extra) -> Document:
    """Build the seed Document + its discovery event and path flags."""
    doc = Document(
        id=doc_id_for(source_key),
        path=path,
        filename=filename,
        size_bytes=size_bytes,
        source_key=source_key,
        path_info=info,
        **extra,
    )
    record(doc, "walk", "discovered",
           f"fy={info.fy} month={info.month} date={info.date_folder} "
           f"company={info.company}", company=info.company, fy=info.fy)
    if info.company is None:
        doc.add_flag("no_company_folder", "could not derive company from path")
    if not (info.fy and info.month and info.date_folder):
        doc.add_flag("path_incomplete", "missing FY/month/date folder level")
    return doc


def _is_candidate(name: str) -> bool:
    suffix = Path(name).suffix.lower()
    return (name not in IGNORE_FILENAMES
            and suffix not in IGNORE_SUFFIXES
            and suffix in CANDIDATE_SUFFIXES)


def walk(root: Path, settings: Settings = DEFAULTS,
         control: Optional[RunControl] = None) -> Iterator[Document]:
    """Yield seed Documents for every in-scope PDF under ``root``.

    Streams (os.walk, sorted at each level) rather than materializing the tree, so
    the pipeline can start on the first PDF immediately. Deterministic order.
    """
    root = Path(root)

    for dirpath, dirnames, filenames in os.walk(root):
        if control is not None:
            control.gate()          # a Stop during discovery lands here
        dirnames.sort()             # deterministic descent
        # One financial year per run: PRUNE the other years' subtrees rather than
        # filter their documents out, so we never descend them at all. The edit
        # must be in place (`dirnames[:]`) — os.walk reads the list back after this
        # iteration, and still does so when the bank gate below `continue`s (which
        # it always does for the root dir, the only place the FY folders are
        # visible — hence this sits *above* that gate).
        #
        # `root` stays the TREE root on purpose: re-rooting at the FY folder would
        # drop the FY component from folder_parts, so _parse_path would set no
        # info.fy and every single doc would come out flagged `path_incomplete`.
        if settings.fy_scope:
            dirnames[:] = [d for d in dirnames if keep_fy_dir(d, settings.fy_scope)]
        here = Path(dirpath)
        folder_parts = list(here.relative_to(root).parts)

        # Scope gate: only PDFs under .../Payments/<scope>/...
        bank = _bank_from(folder_parts, settings.bank_scope)
        if not bank or bank.lower() != settings.bank_scope.lower():
            continue

        info_base = _parse_path(folder_parts)
        info_base.bank = info_base.bank or bank

        for name in sorted(filenames):
            if not _is_candidate(name):
                continue
            path = here / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            resolved = str(path.resolve())
            yield seed_document(
                source_key=f"local:{resolved}",   # stable key for the resume skip-list
                path=resolved,
                filename=name,
                size_bytes=size,
                info=info_base.model_copy(deep=True),
            )
