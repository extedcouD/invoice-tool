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


def cmd_scan(args) -> None:
    settings = replace(
        DEFAULTS,
        workers=args.workers,
        ocr_enabled=not args.no_ocr,
    )
    store, result = run_scan(Path(args.root), Path(args.out), settings, quiet=args.quiet)
    s = result.summary()
    print(f"\nrun: {store.dir}")
    print(f"  detections: {store.detections_path}")
    print(f"  draft xlsx: {store.master_path()}")
    print(f"  invoices={s['invoices']} flagged={s['flagged']} "
          f"ocr={s['ocr_docs']} avg_conf={s['avg_invoice_confidence']}")
    if not args.no_serve:
        from .web.app import serve
        print("\nlaunching review app (Ctrl-C to stop)…")
        serve(store, port=args.port, open_browser=not args.no_browser)


def cmd_review(args) -> None:
    store = _resolve_run(args)
    from .web.app import serve
    serve(store, port=args.port, open_browser=not args.no_browser)


def cmd_export(args) -> None:
    store = _resolve_run(args)
    result = store.load()
    path = write_master(result, store.master_path())
    print(f"wrote {path}  ({len(result.invoices())} invoices, {len(result.flagged())} flagged)")


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
    sc.add_argument("--workers", type=int, default=1)
    sc.add_argument("--no-ocr", action="store_true", help="disable OCR fallback")
    sc.add_argument("--quiet", action="store_true", help="no live terminal progress")
    sc.add_argument("--no-serve", action="store_true", help="don't auto-launch review app")
    sc.set_defaults(func=cmd_scan)

    rv = sub.add_parser("review", parents=[web], help="launch the web review app for a run")
    rv.add_argument("--run", help="run dir (default: latest under --out)")
    rv.set_defaults(func=cmd_review)

    ex = sub.add_parser("export", help="(re)write master xlsx from a run")
    ex.add_argument("--run", help="run dir (default: latest under --out)")
    ex.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
