`pyinstaller packaging/InvoiceGSTRLinker.spec --noconfirm```

# invoices-tool — invoice detection & linking (Phase 1)

Scrapes a human-maintained folder tree, detects which PDFs are **invoices**
(vs approvals / returns / junk), links each to its company + id, extracts the
easy "relevant data", and writes a **master Excel** — with a **local web
review app** for the low-confidence cases and full **pipeline observability**.

Phase 1 scope: _detect + link_. No line-item parsing, no financial analysis.
Fully local (no cloud, no API). Bank scope: **Kotak**.

---

## Install

```bash
pip install -r requirements.txt      # needs the tesseract binary on PATH
tesseract --version                  # 5.x expected (brew install tesseract)
```

## Use

```bash
# 1. scan a tree -> detections.json + draft xlsx, then auto-launch the review app
python -m invoices scan --root "Testing Environment" --workers 8

# just the detection, no UI:
python -m invoices scan --root "Testing Environment" --no-serve --workers 8

# One GSTR-2A workbook covers one financial year, so scope the run to that year and
# run the tool once per year. Each year gets its own run dir, master and linked xlsx;
# the other years' folders are never walked. Omit --fy to scan every year, as before.
python -m invoices years --root "Testing Environment"        # FY 22-23, FY 23-24, ...
python -m invoices scan  --root "Testing Environment" --fy "FY 22-23" \
    --gstr GSTR2A_FY22-23.xlsx --workers 8

# 2. reopen the review app for a run (default: latest under ./out)
python -m invoices review

# 3. (re)write the master workbook from a run's detections.json
python -m invoices export
```

Output per run lands in `out/run_<timestamp>/`:

| file               | what                                                             |
| ------------------ | ---------------------------------------------------------------- |
| `detections.json`  | full result — every document, its fields, flags, and event trace |
| `master_<ts>.xlsx` | **Master** (one row/invoice) · **Exceptions** · **Summary**      |
| `status.json`      | live progress snapshot (polled by the dashboard)                 |
| `review_pdfs/`     | copies of the flagged PDFs                                       |

## The review app

- **Dashboard** (`/`) — counts, doc-type mix, per-stage timing/throughput, live progress bar while a scan runs.
- **Review queue** (`/review`) — flagged docs, worst-first. Only low-confidence rows land here.
- **Doc view** (`/doc/<id>`) — PDF preview beside editable fields; **Confirm & save** writes the correction back and re-exports the workbook. Below it, the **pipeline trace**: every decision the pipeline made about this PDF and why.

---

## Build the desktop app (macOS Apple Silicon)

Bundles Python, all deps, and the Tesseract OCR engine into a double-click
`.app` — the end user installs nothing. Run this **natively on an Apple Silicon
(arm64) Mac** and the resulting bundle is Apple Silicon:

```bash
brew install tesseract                        # must be on PATH at build time
pip install -r requirements.txt pyinstaller
pyinstaller packaging/InvoiceGSTRLinker.spec --noconfirm
# -> dist/InvoiceGSTRLinker.app

# zip it for distribution (same layout the CI publishes)
cd dist && zip -r ../InvoiceGSTRLinker-macOS-AppleSilicon.zip InvoiceGSTRLinker.app
```

Prefer not to build locally? Push a `v*` tag or run the **build-desktop-apps**
GitHub Action (`.github/workflows/build-desktop-apps.yml`) — its `macos-14`
runner builds the Apple Silicon app (and a Windows `.exe`) in the cloud.

---

## Architecture

A single `Document` flows through an ordered list of `Stage`s; each stage
enriches it and records `Event`s explaining what it did.

```
walk ─▶ Document(s) ─▶ [ extract ─▶ classify ─▶ parse ─▶ reconcile ─▶ validate ] ─▶ canonicalize ─▶ Excel
                              │         │          │          │           │
                            text/OCR  invoice?   fields    folder↔pdf   confidence
                                                           cross-check   + flags
```

| layer         | dir                  | responsibility                                                                              |
| ------------- | -------------------- | ------------------------------------------------------------------------------------------- |
| domain        | `core/models.py`     | `Document`, `Fields`, `Event`, `Flag`, `RunResult` (pydantic)                               |
| engine        | `core/pipeline.py`   | runs stages, times them, isolates faults, maps documents across a thread pool (`--workers`) |
| contracts     | `core/interfaces.py` | `Stage`, `TextSource` ABCs                                                                  |
| extractors    | `extractors.py`      | default baseline + pluggable**per-vendor** field extractors                                 |
| stages        | `stages/`            | walk · extract · classify · parse · reconcile · validate                                    |
| io            | `io/`                | `pdf.py` (PyMuPDF + Tesseract) · `runstore.py` · `excel.py`                                 |
| matching      | `matching/vendor.py` | normalize + rapidfuzz + GSTIN canonicalization                                              |
| observability | `observability/`     | events · progress reporters (rich + status.json)                                            |
| web           | `web/`               | Flask review app + dashboard + trace viewer                                                 |

**Patterns:** pipeline (stages), strategy (text vs OCR behind `TextSource`),
registry (field extractors), DI (stages receive deps), config-driven (all
thresholds/regexes in `config.py`). Runs are resumable — the run dir is the
source of truth, so `review`/`export` never re-scan.

### Design decisions baked in

- **id = filename** (canonical); the body invoice-no is parsed only to cross-check.
- **Classification is content-scored**, not filename-based (folder naming is unreliable). Filename is a weak tiebreak.
- **OCR fallback is mandatory** — ~20% of invoices are scanned images.
- **Folder date ≠ invoice date** on purpose (folder = payment date), so it is _not_ flagged as a mismatch.
- Any flag routes a doc to review; a transparent additive confidence model explains every deduction.

## Tuning

Everything lives in `invoices/config.py`: classification signal weights and
threshold, fuzzy thresholds (`90` auto / `70` soft), OCR DPI, confidence
deductions, and the Master column order.

### Adding a per-vendor extractor

When a vendor's layout defeats the baseline, register an extractor in
`invoices/extractors.py`. It runs after the baseline and overrides only the
fields it sets; match on GSTIN, vendor name, or a content signature:

```python
@register_vendor("acme", match=gstin_prefix("29AACCB"), priority=10)
def acme(text, doc):
    m = re.search(r"Ref#\s*(\S+)", text)
    if m: doc.fields.invoice_no_content = m.group(1)
```

A worked example ships as `zephyr-logistics` — it recovers an invoice whose
"Bill No / Dated / Amount Payable" layout the baseline can't parse. Exercise it
with the dirt generator below.

## Observability

- Live: `rich` terminal progress during `scan`; the dashboard polls `status.json`.
- Per-doc: the **event trace** on every `Document` (classification score+reasons, which fields parsed, why flagged, confidence breakdown) — visible in the web trace viewer and stored in `detections.json`.
- Aggregate: per-stage timing/throughput/error counts on the dashboard; run rollups in the Summary sheet.

## Testing against messy data

The clean sample is uniform; real filing is not. `scripts/introduce_dirt.py`
plants realistic mess into an isolated `Aug-2022` month (clean files untouched)
and writes a manifest of expected behaviour:

```bash
python scripts/introduce_dirt.py --root "Testing Environment"   # plant
python -m invoices scan --root "Testing Environment" --no-serve --workers 8
python scripts/introduce_dirt.py --root "Testing Environment" --clean   # remove
```

Planted cases (each asserted in `tests/test_dirt.py`):

| case                                                      | what it exercises                              |
| --------------------------------------------------------- | ---------------------------------------------- |
| invoice with a non-`Invoice_` filename                    | content classifier, not filename               |
| invoice named`Approval_*` / approval named `Invoice_*`    | content wins;`filename_content_mismatch`       |
| extra nested`Rescans/` folder                             | tolerant path parsing →`company_mismatch`      |
| invoice under the wrong company folder                    | fuzzy`company_mismatch`                        |
| duplicate invoice id across folders                       | `duplicate_id` detection                       |
| **non-standard vendor layout** (Bill No / Amount Payable) | **per-vendor extractor recovers it** — no flag |
| ISO-format date folder                                    | `path_incomplete`                              |
| corrupt / non-PDF bytes                                   | fault isolation (`stage_error`, run continues) |
| empty company folder / stray`.txt`/`.jpg`                 | skipped, no crash                              |

## Tests

```bash
python -m pytest tests/ -q
```

- `tests/test_pipeline.py` — unit tests (fuzzy match, field parsing, per-vendor
  extractor) + golden precision/recall & field-completeness on the clean set.
- `tests/test_dirt.py` — asserts every planted edge case above (skips if no
  scan+manifest present).

So to scan it now, just point --root there:
python3 scripts/introduce_dirt.py --root "dummy/Testing Environment" # optional edge cases
python3 -m invoices scan --root "dummy/Testing Environment" --workers 8
