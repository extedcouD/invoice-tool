"""Desktop app: one native window that scans a folder of invoices, shows exactly
what the pipeline is doing *live*, and lets you review flagged documents — all
in the same place.

Under the hood it hosts the Flask app (`invoices/web`) inside a pywebview window:
Flask serves the UI + live progress + review queue; a tiny JS-API bridge gives
the page native "Choose folder / file" dialogs. If a webview runtime isn't
available (or you pass `--web`), it falls back to opening the UI in your browser.

Packaged into a double-click app with PyInstaller (packaging/InvoiceGSTRLinker.spec);
when frozen it uses a bundled tesseract so the user installs nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

OUTPUT_ROOT = Path.home() / "InvoiceLinker_output"


def configure_bundled_tesseract() -> None:
    """When running as a PyInstaller bundle, point pytesseract at the bundled
    tesseract binary + tessdata so OCR works with nothing installed on the machine."""
    if not getattr(sys, "frozen", False):
        return  # dev mode: use whatever tesseract is on PATH
    base = Path(getattr(sys, "_MEIPASS", "."))
    tdir = base / "tesseract"
    exe = tdir / ("tesseract.exe" if os.name == "nt" else "tesseract")
    if exe.exists():
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = str(exe)
        os.environ["TESSDATA_PREFIX"] = str(tdir / "tessdata")


def _open_folder(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Web host
# --------------------------------------------------------------------------- #
def _serve_flask(app) -> str:
    """Start the Flask app on a free loopback port in a daemon thread.

    Uses `make_server` (rather than `app.run` + a pre-picked port) so we read
    back the *actual* bound port and avoid a bind/rebind race. Returns the URL.
    """
    import logging

    from werkzeug.serving import make_server

    # this is a local single-user UI server; don't spew a request log line per poll
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/"


def _wait_until_up(url: str, timeout: float = 8.0) -> bool:
    """Poll the local server until it answers, so the window doesn't flash an
    'unable to connect' page before Flask has bound its socket."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


class WebviewApi:
    """JS bridge exposed to the page as `window.pywebview.api` — native dialogs
    and 'open output folder'. The heavy lifting (start/poll) goes through Flask."""

    def __init__(self, controller) -> None:
        self.controller = controller
        self.window = None

    def pick_folder(self):
        import webview
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else None

    def pick_gstr(self):
        import webview
        res = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Excel workbook (*.xlsx;*.xls)", "All files (*.*)"),
        )
        return res[0] if res else None

    def open_output(self):
        d = self.controller.store.dir if self.controller.store else OUTPUT_ROOT
        _open_folder(Path(d))
        return str(d)


# --------------------------------------------------------------------------- #
# Headless CLI (frozen-bundle smoke test / automation)
# --------------------------------------------------------------------------- #
def _run_pipeline(invoice_root: Path, gstr_path: Path, fy: str | None = None):
    """Blocking scan + match + write the linked workbook. Returns (store, report).

    The headless path has no reviewer, so it does in one shot what the UI splits
    across review: `run_scan(gstr_path=...)` matches, and `write_linked` exports.
    ``fy`` scopes the scan to one financial-year folder (None = every year).
    """
    from dataclasses import replace

    from .config import DEFAULTS
    from .detect import run_scan
    from .io.gstr import write_linked

    settings = replace(DEFAULTS, workers=4, fy_scope=fy)
    store, result = run_scan(invoice_root, OUTPUT_ROOT, settings, quiet=True,
                             gstr_path=gstr_path)
    plan = store.load_link()
    if plan is None:
        # run_scan deliberately swallows a bad workbook so it can't sink a good
        # scan; headless has no reviewer to tell, so surface it here rather than
        # dying on `None.counts()` inside write_linked.
        raise RuntimeError(
            "the scan succeeded but the GSTR-2A workbook could not be matched: "
            + (store.read_meta().get("link_error") or "unknown error"))
    report = write_linked(result, plan, gstr_path, store)
    return store, report


def _run_cli(argv: list[str]) -> int:
    """Headless mode:  run_gui --cli <invoice_folder> <gstr.xlsx> [<FY xx-yy>]

    Same pipeline as the window, no display needed. Used to smoke-test the frozen
    bundle (incl. bundled tesseract) and handy for power users / automation. The
    optional third arg scopes the scan to one financial year, since one GSTR-2A
    workbook covers one year.
    """
    if len(argv) < 2:
        print("usage: --cli <invoice_folder> <gstr.xlsx> [<FY xx-yy>]")
        return 2
    fy = argv[2] if len(argv) > 2 else None
    store, report = _run_pipeline(Path(argv[0]), Path(argv[1]), fy=fy)
    print(f"linked {report.matched}/{report.total} B2B rows "
          f"({report.not_found} not found); output: {store.dir}")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    configure_bundled_tesseract()
    argv = sys.argv[1:]
    if argv[:1] == ["--cli"]:
        raise SystemExit(_run_cli(argv[1:]))

    from .web.app import create_app
    from .web.runner import RunController

    controller = RunController(OUTPUT_ROOT)
    app = create_app(controller)
    url = _serve_flask(app)
    _wait_until_up(url)

    # Prefer a native window; fall back to the browser if no webview runtime.
    #
    # The fallback must cover `webview.start()`, not just the import: pywebview's
    # only Windows backend is WinForms, which loads the .NET CLR through pythonnet
    # *at start time*. On a locked-down or older Windows box that load fails
    # ("Failed to resolve Python.Runtime.Loader.Initialize") long after `import
    # webview` succeeded — and it used to take the whole app down with it. The web
    # UI is the same either way; only the native folder-picker is lost, and the
    # page already falls back to typed paths.
    if "--web" not in argv:
        try:
            import webview

            api = WebviewApi(controller)
            window = webview.create_window(
                "Invoice → GSTR Linker", url, js_api=api,
                width=1180, height=820, min_size=(920, 660),
            )
            api.window = window
            webview.start()
            return
        except Exception as exc:
            print(f"native window unavailable ({type(exc).__name__}: {exc})\n"
                  "falling back to your web browser.")

    # Browser fallback — keep the process (and Flask thread) alive.
    import webbrowser
    webbrowser.open(url)
    print(f"Invoice → GSTR Linker running at {url}  (Ctrl-C to quit)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
