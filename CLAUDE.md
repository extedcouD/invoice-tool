# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`invoices` is a **local, no-cloud** tool that walks a human-maintained folder tree of
Kotak-bank payment PDFs, decides which PDFs are invoices (vs approvals / GSTR-2A / junk),
links each to its company + id, extracts a Phase-1 field subset, and writes a master Excel —
with a Flask review app for low-confidence cases. Scope is deliberately narrow: **detect +
link only**. No line-item parsing, no financial analysis, bank scope hard-coded to Kotak.

## Commands

```bash
pip install -r requirements.txt          # also requires the `tesseract` binary on PATH (5.x)
tesseract --version                      # OCR fallback is mandatory; ~20% of invoices are scans

# scan a tree -> out/run_<ts>/ (detections.json + draft xlsx), then auto-launch the review app
python -m invoices scan --root "Testing Environment" --workers 8
python -m invoices scan --root "Testing Environment" --no-serve --workers 8   # detection only
python -m invoices review                # reopen review app for the latest run under ./out
python -m invoices export                # (re)write master xlsx from a run's detections.json

python -m pytest tests/ -q                                       # all tests
python -m pytest tests/test_pipeline.py::test_money_and_date -q  # a single test
```

**Testing against messy data** — the golden/dirt tests (`test_dirt.py` and the
`test_golden_*` cases in `test_pipeline.py`) **skip unless a scan has already run** and left
`out/run_*/detections.json`; the dirt tests additionally need the manifest. Full loop:

```bash
python scripts/introduce_dirt.py --root "Testing Environment"          # plant edge cases + manifest
python -m invoices scan --root "Testing Environment" --no-serve --workers 8
python -m pytest tests/ -q
python scripts/introduce_dirt.py --root "Testing Environment" --clean  # remove planted dirt
```

Note: the sample tree (`Testing Environment/`) is **not checked into the repo** — you must
point `--root` at your own copy. Dirt is isolated to a synthetic `Aug-2022` month so clean
files are never touched.

## Architecture

One mutable `Document` (pydantic, `core/models.py`) is threaded through an ordered list of
`Stage`s (`core/interfaces.py`). Each stage enriches the document in place and calls
`observability.events.record(...)` to append an `Event` explaining what it did and why. **That
event trail is the observability spine** — it drives the web trace viewer and is persisted in
`detections.json`. When adding logic, record an event for any decision a reviewer would want
to see.

Flow (wired in `detect.py::build_pipeline`, run by `core/pipeline.py`):

```
walk() ─▶ seed Documents ─▶ extract ─▶ classify ─▶ parse ─▶ reconcile ─▶ validate
             (a function,      (text/OCR) (invoice?) (fields)  (folder↔pdf)  (confidence
              not a Stage)                                      cross-check)   + flags)
   then post-pipeline cross-document passes in detect.py:
     _canonicalize_vendors (GSTIN as vendor key) · _flag_duplicate_ids · _copy_flagged
```

Key structural facts (each requires reading several files to reconstruct):

- **`walk()` is not a Stage.** It's a standalone function (`stages/walk.py`) that discovers
  candidate PDFs under `.../Payments/Kotak/...`, tolerantly labels each path component by
  regex (not fixed depth), and produces seed `Document`s. Everything after it is a `Stage`.
- **Stages short-circuit on non-invoices.** `parse`/`reconcile`/`validate` return early unless
  `doc_type == INVOICE` — except `parse` always sets `invoice_id` from the filename first.
- **The run directory is the single source of truth.** `review` and `export` load
  `detections.json` and never re-scan. `RunStore` (`io/runstore.py`) owns the layout.
- **Fault isolation.** `Pipeline.run_one` wraps each stage in try/except: a failure flags
  `stage_error` on that one doc and the run continues. Never let a stage crash the whole run.
- **`workers > 1` uses a `ThreadPoolExecutor`, not processes.** OCR shells out to the
  `tesseract` binary, so threads give real parallelism. Results are kept in input order; the
  shared metric accumulators in `Pipeline` are guarded by `self._lock`.
- **Config-driven, DI-friendly.** All thresholds, regexes, signal weights, and the Master
  column order live in `config.py` (a frozen `Settings` dataclass + module constants). Stages
  receive `Settings` by constructor injection — don't bury magic values in stage logic.

## Where to make common changes

- **Tune detection/matching/confidence** → `config.py`: classification signal weights +
  `INVOICE_SCORE_THRESHOLD`; fuzzy tiers (`FUZZY_AUTO_ACCEPT=90` / `FUZZY_SOFT_FLAG=70`);
  OCR DPI; per-reason confidence deductions in `stages/validate.py`; `MASTER_COLUMNS`.
- **A vendor's layout defeats the baseline** → register a per-vendor extractor in
  `extractors.py` via `@register_vendor(name, match=..., priority=...)`. It runs *after*
  `default_extract` and overrides only the fields it sets. Match on `gstin_prefix`,
  `vendor_name_contains`, or `text_matches`. Worked example: `_zephyr`. No pipeline edits.
- **Add/reorder a pipeline step** → implement `Stage` (unique `name`, `process(doc)->doc`) and
  wire it into `build_pipeline` in `detect.py`.
- **New progress sink** (websocket, metrics, ...) → implement the `Reporter` interface in
  `observability/progress.py` and add it to the `MultiReporter` in `detect.py::run_scan`.

## Design decisions baked in (don't "fix" these)

- **id = filename** is canonical; the body invoice-no is parsed only to cross-check
  (`id_mismatch` flag). Classification is **content-scored**, not filename-based — the filename
  is a weak tiebreak, and filename-vs-content disagreement is surfaced as
  `filename_content_mismatch`, not silently trusted.
- **Folder date ≠ invoice date on purpose** — the folder is the *payment* date, so `reconcile`
  deliberately does **not** flag them as mismatched.
- **Any flag routes a doc to review.** Confidence is a transparent additive-deduction model in
  `validate`; a low score adds an explicit `low_confidence` flag on top of any hard flags.
