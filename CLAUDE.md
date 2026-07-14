# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`invoices` is a tool that walks a human-maintained folder tree of
Kotak-bank payment PDFs, decides which PDFs are invoices (vs approvals / GSTR-2A / junk),
links each to its company + id, extracts a Phase-1 field subset, and writes a master Excel —
with a Flask review app for low-confidence cases. Scope is deliberately narrow: **detect +
link only**. No line-item parsing, no financial analysis, bank scope hard-coded to Kotak.

The default source is a **local** folder, but the desktop app can also read the tree
**directly from Google Drive** and publish results back to a new Drive folder (see the
FileSource seam + `io/drive.py` below). Large scans **checkpoint and resume**, so a run over a
120 GB Drive tree survives interruption.

## Commands

```bash
pip install -r requirements.txt          # also requires the `tesseract` binary on PATH (5.x)
tesseract --version                      # OCR fallback is mandatory; ~20% of invoices are scans

# scan a tree -> out/run_<ts>/ (detections.json + draft xlsx), then auto-launch the review app
python -m invoices scan --root "Testing Environment" --workers 8
python -m invoices scan --root "Testing Environment" --no-serve --workers 8   # detection only

# pass the return too, and the review app opens on the gap that matters:
# which B2B rows found no invoice PDF (and which invoices found no row)
python -m invoices scan --root "Testing Environment" --gstr GSTR2A_Return.xlsx --workers 8

# one financial year per run (the returns arrive one workbook per year) — the other
# years' folders are never walked. Omit --fy to scan every year, as before.
python -m invoices years --root "Testing Environment"          # list the FY folders
python -m invoices scan --root "Testing Environment" --fy "FY 22-23" \
    --gstr GSTR2A_FY22-23.xlsx --workers 8
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
        (a gated generator,  (text/OCR) (invoice?) (fields)  (folder↔pdf)  (confidence
         not a Stage)                                         cross-check)   + flags)
   then post-pipeline cross-document passes in detect.py:
     _canonicalize_vendors (GSTIN as vendor key) · _flag_duplicate_ids
     · match against GSTR-2A (io/gstr.py) · _copy_flagged
```

Key structural facts (each requires reading several files to reconstruct):

- **`walk()` is not a Stage — and it is a generator.** It's a standalone function
  (`stages/walk.py`) that discovers candidate PDFs under `.../Payments/Kotak/...`, tolerantly
  labels each path component by regex (not fixed depth), and **yields** seed `Document`s.
  `Pipeline.run` *pulls* from it with a bounded window of in-flight work, so discovery and
  processing overlap: the first PDF is processed while the tree is still being enumerated.
  Both walkers take a `RunControl` and `gate()` per directory / per Drive `files.list` page —
  without that, Stop was a literal no-op for the minutes a 120 GB Drive walk takes.
- **A run is scoped to one financial year, and the walkers *prune* rather than filter**
  (`Settings.fy_scope` → `keep_fy_dir`, shared by both walkers so they can't drift). `--root`
  stays the **tree** root even for a one-year run: re-rooting at the FY folder — the obvious
  shortcut — drops the FY component from the walk-relative path, so `_parse_path` sets no
  `info.fy` and **every** doc comes out flagged `path_incomplete` (a 100%-flagged review queue
  and a blank `fy` column). Locally the prune is an in-place `dirnames[:]` edit that must sit
  *above* the bank gate, which `continue`s for the root dir — the one dir where the FY folders
  are visible. On Drive it must reach `walk_pdf_tree`'s `descend` predicate: `walk_drive`'s
  scope gate runs *after* the listing, so on its own it only prevents downloads, and we'd still
  pay one `files.list` round-trip per folder of every other year. Year names are matched through
  `norm_fy` (`RE_FY` accepts `FY22-23` and `fy 22 - 23`) — an exact `==` would silently prune the
  whole tree and "succeed" with zero invoices, which looks exactly like a broken tool.
- **`Document.id` is a stable hash of `source_key`**, not a walk index. An index-based id is
  only unique within one enumeration, so on a resumed run over a changed tree a fresh document
  could collide with a different, already-checkpointed one.
- **Stages short-circuit on non-invoices.** `parse`/`reconcile`/`validate` return early unless
  `doc_type == INVOICE` — except `parse` always sets `invoice_id` from the filename first.
- **The run directory is the single source of truth.** `review` and `export` load
  `detections.json` and never re-scan. `RunStore` (`io/runstore.py`) owns the layout.
- **Where the bytes come from is abstracted (`FileSource`, `core/interfaces.py`).** `walk()`
  seeds Documents for local paths; `extract` (`io/pdf.py`) calls `FileSource.materialize(doc)`
  to get a *local* path before `fitz.open`, so OCR/parse never know the source. `LocalFileSource`
  (`io/sources.py`) is a passthrough; `DriveFileSource` (`io/drive.py`) downloads each PDF to a
  temp file. To scan Drive, `run_scan` takes a `walker=walk_drive` + `file_source=DriveFileSource`
  — the pipeline is otherwise unchanged.
- **Linking is pipeline state, not just an output format** (`io/gstr.py`). It is split in three:
  `read_b2b_rows` (parse the return once) → **`match`** (pure, in-memory, no I/O) →
  `write_linked` (copy PDFs + write `<stem>_linked.xlsx`). Because `match` is pure it is cheap
  to re-run after *every* review edit, which is what lets the UI say "the GSTIN you just fixed
  now matches B2B row 143". `apply_plan` stamps the outcome onto each `Document`
  (`link_status` / `gstr_row` / `gstr_ref`), so it reaches `detections.json`, `MASTER_COLUMNS`
  and the trace viewer. The `LinkPlan` is persisted to `link.json`, and the reviewer's manual
  row→PDF bindings to `run_meta.json` — so a reopened run can still link.
  **Matching runs at the end of the scan; only the expensive write is deferred to
  "Finish & export".** Never compute a doc→row mapping and reduce it to a count — the two-sided
  gap (rows with no PDF, invoices with no row) *is* the product.
- **A human resolving a row searches by *path*, because the fields are what failed**
  (`matching/paths.py`). `match` keys on (GSTIN, invoice no), so a row reaching the "no PDF"
  tab usually means one of those two was misread — offering only a field search there offers
  the reviewer the very thing that already failed. `PathIndex` is a second way in, pure over
  the run's Documents (no I/O, no scan): `search` is typo-tolerant over the full original
  folder path *and* the fields, `browse` walks the original tree a level at a time, and
  `suggest_folders` uses the B2B row's supplier name to open the browser at the right company
  folder. It backs `/api/link/search` + `/api/link/browse`, and is cached per (run, doc count,
  edit generation) in `web/app.py` because the search is a typeahead — it runs on every
  keystroke over what may be 50k documents. Scoring is a **mean over the query's words** off an
  inverted index, *not* `fuzz.WRatio` over the whole path: WRatio folds in
  `partial_token_set_ratio`, which returns 100 as soon as any single word overlaps, and every
  PDF in a tree shares words like "2022" — it scored an unrelated Zephyr invoice 85 for the
  query "apex aug 003".
- **`write_linked` pulls bytes through the `FileSource`.** On a Drive run `doc.path` is a
  display string, not a file — a plain `shutil.copy2(doc.path)` fails for every matched row.
- **Scans are resumable, and `RunResult.complete` is the one fact that decides it.** Each
  finished doc is appended to `checkpoint.jsonl` immediately; `run_meta.json` marks a run
  complete. `RunStore.find_resumable()` reopens an interrupted run for the same input, and
  `Pipeline.run` skips sources already in the checkpoint (keyed by `Document.source_key`) while
  still *counting* them, so a resumed run's progress bar is against the whole tree. The final
  `detections.json` is assembled from the full ledger. A run is complete iff the walk was
  exhausted and not cancelled — deriving "stopped" from `control.stopped` instead once marked a
  run complete *and* offered Resume, which then silently rescanned from zero.
  **A run's input identity is `(root, fy)`, not `root`** — `run_meta.json` is
  `{root, fy, complete}` and `find_resumable()` matches on both. On root alone, a fresh
  "FY 23-24" run would reopen an interrupted "FY 22-23" checkpoint and `checkpoint_docs()` would
  assemble one `detections.json` spanning two years. The year is a *separate* meta key and never
  a suffix on `root`, because `root` is also what every path is made relative to (`io/excel._rel`,
  `PathIndex`) and so has to stay a real path. In the app, `run_state().last_input.fy` is
  load-bearing for the same reason Resume exists at all: Resume POSTs `last_input` **verbatim**,
  so dropping the year there restarts a one-year run as an unscoped rescan of the whole tree.
- **Pause/stop state is *pulled*, never pushed** (`RunController.status()`). `status.json` is
  only rewritten when a document completes, and pausing stops documents completing — so a
  pushed `paused` flag froze at `false` forever and Resume became unreachable. `status()` reads
  `paused`/`stopping` off the live `RunControl`. `gate()` is checked between stages
  (`Pipeline.run_one`), per OCR page (`io/pdf.py`) and inside both walkers; it must stay
  *outside* the fault-isolation `try/except`, or a Stop is mislabelled a `stage_error`.
- **The desktop app (`gui.py`) hosts the web UI, it is not a separate UI.** It starts
  the Flask app (`web/app.py`) in a thread and shows it in a native pywebview window
  (browser fallback via `--web`). A shared `RunController` (`web/runner.py`) owns the
  live job (phase `idle→scanning→[linking]→finishing→done|stopped`); the page's `home.html` picks
  the folder/GSTR via a `WebviewApi` native-dialog bridge, POSTs `/api/start`, then polls
  `/status` (fine-grained progress: `current` file + `recent[]` feed + counts + `discovering`,
  written by `StatusWriter`) and `/run_state` (coarse phase) to drive one screen:
  setup → live activity feed → integrated review queue. Don't reintroduce a
  progress-less spinner — the live feed *is* the observability surface for end users.
  **The app stays navigable while it scans**: `/review` and `/dashboard` render the live
  checkpoint (they used to 302 home for the entire run), a run bar in `base.html` carries
  pause/stop onto every page, and all page state is rehydrated from `run_state()`. Review is
  deliberately **read-only until the scan lands** (`_locked()` in `web/app.py`) — `run_scan`
  rebuilds `detections.json` from the checkpoint at the end, so a mid-scan edit would be lost.
  The setup screen also offers a **Google Drive** source: "Connect Google Drive"
  (`/api/drive/auth` → `RunController.authenticate_drive`, installed-app OAuth), a folder
  link/id, and an output-folder name; on finish, `/api/drive/upload` publishes the master +
  detected invoices to a new Drive folder (invoices via server-side `files.copy` — originals are
  never modified). OAuth needs a user-supplied `client_secret.json` in the app-support dir
  (`io/drive.app_support_dir()`); the frozen bundle can be smoke-tested with `--selftest`.
- **Fault isolation.** `Pipeline.run_one` wraps each stage in try/except: a failure flags
  `stage_error` on that one doc and the run continues. Never let a stage crash the whole run.
- **`workers > 1` uses a `ThreadPoolExecutor`, not processes.** OCR shells out to the
  `tesseract` binary, so threads give real parallelism. Work is submitted in a bounded window
  (~`workers × 4`) rather than all at once, results are sorted back into a deterministic order,
  and the shared metric accumulators in `Pipeline` are guarded by `self._lock`.
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
- **Scope a run to one financial year** → `Settings.fy_scope` (`config.py`) + `keep_fy_dir` /
  `norm_fy` / `list_fy_folders` (`stages/walk.py`), used by *both* walkers. Pure, so
  `tests/test_scope.py` asserts the prune (and that the other year is never even listed on
  Drive) with no scan and no network.
- **Change how a B2B row is matched to a PDF** → `io/gstr.py::_resolve` / `_pick_best` (keys and
  tie-breaks) and `suggest` (the candidates offered when a human has to resolve a row by hand).
  `match` is pure, so `tests/test_linking.py` asserts outcomes directly — no scan needed.
- **Change how a human *finds* a PDF for an unmatched row** → `matching/paths.py`: `TOKEN_SCORE`
  (what counts as the same word despite a typo) and `SCORE_CUTOFF` (how much of the query a path
  must answer), `_searchable` (what goes in the haystack), `browse`. Pure, so
  `tests/test_paths.py` asserts ranking directly — no scan needed.
- **New progress sink** (websocket, metrics, ...) → implement the `Reporter` interface in
  `observability/progress.py` and add it to the `MultiReporter` in `detect.py::run_scan`.
  Note `Reporter.discovered(n, complete)` — the corpus total is unknown until the walk ends.

## Design decisions baked in (don't "fix" these)

- **id = filename** is canonical; the body invoice-no is parsed only to cross-check
  (`id_mismatch` flag). Classification is **content-scored**, not filename-based — the filename
  is a weak tiebreak, and filename-vs-content disagreement is surfaced as
  `filename_content_mismatch`, not silently trusted.
- **Folder date ≠ invoice date on purpose** — the folder is the *payment* date, so `reconcile`
  deliberately does **not** flag them as mismatched.
- **One financial year per run — never merge years.** The client's returns arrive one GSTR-2A
  workbook per year, and a run takes exactly one workbook, so a whole-tree scan could only ever
  be matched against one year's return — every other year's invoices would land in the "no B2B
  row" bucket, which is noise, not signal. Each year is its own run dir / master / linked
  workbook. "All years" (`--fy` omitted) stays the default so nothing breaks.
- **Any flag routes a doc to review.** Confidence is a transparent additive-deduction model in
  `validate`; a low score adds an explicit `low_confidence` flag on top of any hard flags.
- **Matching is tolerant of a bad GSTIN read, not of a bad number.** `_resolve` falls back to
  invoice-number-only when the composite `(GSTIN, number)` key misses, so a mis-OCR'd GSTIN
  doesn't lose the link. It only refuses to guess when one number maps to *conflicting* GSTINs —
  that surfaces as `ambiguous` for a human to settle, never a silent pick.
- **The review queue is the linking cockpit, not a confidence queue.** Its first tab is "GSTR
  rows with no PDF" — the client's actual question. Low-confidence flags are the *third* tab.
  A human resolving a row (manual bind, or correcting a field) is the point of the whole app,
  so every such action re-runs `match` and reports what changed.
- **A human may bind a row to a PDF the classifier did *not* call an invoice — and that
  promotes it.** Search and browse deliberately range over every PDF in the tree, because a row
  with no PDF very often points at one scored an approval or missed outright; hiding those hides
  the answer. But `match` resolves `manual_links` against `result.invoices()` only, so binding a
  non-invoice would otherwise be a silent no-op (and `write_linked` would write `NOT FOUND` over
  it). `bind_row` therefore takes the human's pick as the assertion that it *is* an invoice:
  it sets `doc_type=INVOICE` and records a `promoted` event saying a human, not the classifier,
  decided that. The UI warns before the bind; don't make it bind silently.
