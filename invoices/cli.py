"""Command-line entrypoint.

    python -m invoices scan   --root "Testing Environment"   # walk + detect + draft xlsx (+ auto-launch review)
    python -m invoices review --run out/run_<ts>            # launch web review/dashboard
    python -m invoices export --run out/run_<ts>            # (re)write master xlsx from detections.json
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import DEFAULTS
from .detect import run_scan
from .io.excel import write_master
from .io.runstore import RunStore


def _latest_run(out_root: Path) -> Path | None:
    runs = sorted(Path(out_root).glob("run_*"))
    return runs[-1] if runs else None


def _resolve_run(args) -> RunStore:
    run = args.run
    if not run:
        run = _latest_run(args.out)
        if not run:
            sys.exit(f"no runs found under {args.out}")
        print(f"[using latest run: {run}]")
    return RunStore(Path(run))


def _resolve_fy(root: Path, fy: str | None) -> str | None:
    """Validate --fy against the FY folders actually under --root, and return the
    folder's REAL name (so run_meta and the run-id label stay canonical).

    A typo'd year would otherwise prune the entire tree and 'succeed' with zero
    invoices — indistinguishable from a broken scan, so fail loudly instead.
    """
    from .stages.walk import list_fy_folders, norm_fy

    if not fy:
        return None
    years = list_fy_folders(root)
    hit = next((y for y in years if norm_fy(y) == norm_fy(fy)), None)
    if hit is None:
        sys.exit(f"no financial-year folder matching {fy!r} under {root}\n"
                 f"  available: {', '.join(years) or '(none)'}")
    return hit


def cmd_scan(args) -> None:
    root = Path(args.root)
    fy = _resolve_fy(root, getattr(args, "fy", None))
    settings = replace(
        DEFAULTS,
        workers=args.workers,
        ocr_enabled=not args.no_ocr,
        fy_scope=fy,
    )
    gstr = Path(args.gstr) if getattr(args, "gstr", None) else None
    store, result = run_scan(root, Path(args.out), settings,
                             quiet=args.quiet, gstr_path=gstr)
    s = result.summary()
    print(f"\nrun: {store.dir}" + (f"   [{fy} only]" if fy else ""))
    print(f"  detections: {store.detections_path}")
    print(f"  draft xlsx: {store.master_path()}")
    print(f"  invoices={s['invoices']} flagged={s['flagged']} "
          f"ocr={s['ocr_docs']} avg_conf={s['avg_invoice_confidence']}")
    plan = store.load_link()
    if plan is not None:
        c = plan.counts()
        print(f"  GSTR-2A: matched {c['matched']}/{c['total']} B2B rows "
              f"({c['not_found']} no PDF, {c['ambiguous']} ambiguous, "
              f"{c['unreferenced']} invoice(s) with no row)")
        print("  open the review app to resolve them, then Finish & export")
    if not args.no_serve:
        from .web.app import serve
        print("\nlaunching review app (Ctrl-C to stop)…")
        serve(store, port=args.port, open_browser=not args.no_browser)


def cmd_years(args) -> None:
    from .stages.walk import list_fy_folders

    years = list_fy_folders(Path(args.root))
    if not years:
        sys.exit(f"no 'FY xx-yy' folders under {args.root}")
    print("\n".join(years))


def cmd_review(args) -> None:
    store = _resolve_run(args)
    from .web.app import serve
    serve(store, port=args.port, open_browser=not args.no_browser)


def cmd_export(args) -> None:
    store = _resolve_run(args)
    result = store.load()
    path = write_master(result, store.master_path())
    print(f"wrote {path}  ({len(result.invoices())} invoices, {len(result.flagged())} flagged)")


def cmd_link_gstr(args) -> None:
    from .io.gstr import link_gstr

    store = _resolve_run(args)
    result = store.load()
    rep = link_gstr(result, Path(args.gstr), store,
                    sheet_name=args.sheet, ref_header=args.ref_column)
    print(f"\nlinked {rep.matched}/{rep.total} B2B rows "
          f"({rep.not_found} NOT FOUND, {rep.ambiguous} ambiguous, "
          f"{rep.copy_failed} copy-failed)")
    print(f"  workbook: {rep.out_path}")
    print(f"  invoices: {rep.flat_dir}  ({rep.matched} files)")
    if rep.duplicate_filings:
        print(f"  note: {rep.duplicate_filings} row(s) matched a duplicate-filed invoice")
    if rep.unreferenced_invoices:
        print(f"  note: {rep.unreferenced_invoices} detected invoice(s) had no B2B row")
    for row, gstin, invno in rep.unmatched_rows:
        print(f"    NOT FOUND  row {row}: {gstin or '-'}  {invno or '-'}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invoices", description="Invoice detection & linking")
    p.add_argument("--out", default="out", type=Path, help="output root (default: ./out)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # shared web options, valid after the subcommand (scan/review)
    web = argparse.ArgumentParser(add_help=False)
    web.add_argument("--port", default=5000, type=int, help="review app port")
    web.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")

    sc = sub.add_parser("scan", parents=[web], help="scan a folder tree and detect invoices")
    sc.add_argument("--root", required=True, help="root folder to scan (FY.. tree)")
    sc.add_argument("--fy", help="scan only this financial-year folder, e.g. 'FY 22-23' "
                                 "(default: every FY under --root). One GSTR-2A workbook "
                                 "covers one year, so scope the run to that year.")
    sc.add_argument("--gstr", help="GSTR-2A workbook to match against (enables the "
                                   "linking review: which B2B rows have no PDF)")
    sc.add_argument("--workers", type=int, default=1)
    sc.add_argument("--no-ocr", action="store_true", help="disable OCR fallback")
    sc.add_argument("--quiet", action="store_true", help="no live terminal progress")
    sc.add_argument("--no-serve", action="store_true", help="don't auto-launch review app")
    sc.set_defaults(func=cmd_scan)

    rv = sub.add_parser("review", parents=[web], help="launch the web review app for a run")
    rv.add_argument("--run", help="run dir (default: latest under --out)")
    rv.set_defaults(func=cmd_review)

    yr = sub.add_parser("years", help="list the financial-year folders under a root")
    yr.add_argument("--root", required=True, help="root folder (FY.. tree)")
    yr.set_defaults(func=cmd_years)

    ex = sub.add_parser("export", help="(re)write master xlsx from a run")
    ex.add_argument("--run", help="run dir (default: latest under --out)")
    ex.set_defaults(func=cmd_export)

    from .io.gstr import DEFAULT_REF_HEADER, DEFAULT_SHEET
    lk = sub.add_parser("link-gstr",
                        help="link detected invoices into a GSTR-2A B2B sheet + flatten PDFs")
    lk.add_argument("--run", help="run dir (default: latest under --out)")
    lk.add_argument("--gstr", required=True, help="path to the GSTR-2A return .xlsx template")
    lk.add_argument("--sheet", default=DEFAULT_SHEET, help="B2B sheet name")
    lk.add_argument("--ref-column", default=DEFAULT_REF_HEADER,
                    help="header for the new invoice-reference column")
    lk.set_defaults(func=cmd_link_gstr)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
