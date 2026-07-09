"""Minimal desktop GUI: pick an invoice folder + a GSTR-2A file, click Run.

Wraps the same scan -> link pipeline the CLI uses, so a non-technical user never
touches a command line. Packaged into a double-click app with PyInstaller
(packaging/InvoiceGSTRLinker.spec); when frozen it uses a bundled tesseract so the
user installs nothing.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
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


def _run_pipeline(invoice_root: Path, gstr_path: Path):
    """Blocking scan + link (runs in a worker thread). Returns (store, report)."""
    from dataclasses import replace

    from .config import DEFAULTS
    from .detect import run_scan
    from .io.gstr import link_gstr

    settings = replace(DEFAULTS, workers=4)
    store, result = run_scan(invoice_root, OUTPUT_ROOT, settings, quiet=True)
    report = link_gstr(result, gstr_path, store)
    return store, report


def _run_cli(argv: list[str]) -> int:
    """Headless mode:  run_gui --cli <invoice_folder> <gstr.xlsx>

    Same pipeline as the window, no display needed. Used to smoke-test the frozen
    bundle (incl. bundled tesseract) and handy for power users / automation.
    """
    if len(argv) < 2:
        print("usage: --cli <invoice_folder> <gstr.xlsx>")
        return 2
    store, report = _run_pipeline(Path(argv[0]), Path(argv[1]))
    print(f"linked {report.matched}/{report.total} B2B rows "
          f"({report.not_found} not found); output: {store.dir}")
    return 0


def main() -> None:
    configure_bundled_tesseract()
    if sys.argv[1:2] == ["--cli"]:
        raise SystemExit(_run_cli(sys.argv[2:]))
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title("Invoice → GSTR Linker")
    root.geometry("580x340")
    root.resizable(False, False)

    state: dict = {"invoice": None, "gstr": None}
    q: "queue.Queue" = queue.Queue()

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    inv_var = tk.StringVar(value="(none chosen)")
    gstr_var = tk.StringVar(value="(none chosen)")
    status_var = tk.StringVar(value="Choose the invoice folder and the GSTR-2A file, then Run.")

    def choose_invoice():
        d = filedialog.askdirectory(title="Choose the invoice folder (the FY.. tree)")
        if d:
            state["invoice"] = Path(d)
            inv_var.set(d)

    def choose_gstr():
        f = filedialog.askopenfilename(title="Choose the GSTR-2A .xlsx file",
                                       filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")])
        if f:
            state["gstr"] = Path(f)
            gstr_var.set(f)

    ttk.Label(frm, text="1.  Invoice folder").grid(row=0, column=0, sticky="w", pady=6)
    ttk.Label(frm, textvariable=inv_var, foreground="#666").grid(row=0, column=1, sticky="w", padx=8)
    ttk.Button(frm, text="Choose…", command=choose_invoice).grid(row=0, column=2)

    ttk.Label(frm, text="2.  GSTR-2A file").grid(row=1, column=0, sticky="w", pady=6)
    ttk.Label(frm, textvariable=gstr_var, foreground="#666").grid(row=1, column=1, sticky="w", padx=8)
    ttk.Button(frm, text="Choose…", command=choose_gstr).grid(row=1, column=2)

    run_btn = ttk.Button(frm, text="Run")
    run_btn.grid(row=2, column=0, columnspan=3, pady=14, ipadx=20, ipady=4)

    bar = ttk.Progressbar(frm, mode="indeterminate", length=520)
    status = ttk.Label(frm, textvariable=status_var, wraplength=520, justify="left")
    status.grid(row=4, column=0, columnspan=3, sticky="w", pady=6)
    open_btn = ttk.Button(frm, text="Open output folder")

    def poll():
        try:
            kind, *rest = q.get_nowait()
        except queue.Empty:
            root.after(150, poll)
            return
        bar.stop()
        bar.grid_forget()
        run_btn.configure(state="normal")
        if kind == "ok":
            store, report = rest
            msg = f"✓ Linked {report.matched}/{report.total} invoices."
            if report.not_found:
                msg += (f"  {report.not_found} could not be matched "
                        f"(listed on the 'Link Report' sheet).")
            msg += f"\n\nOutput saved to:\n{store.dir}"
            status_var.set(msg)
            open_btn.configure(command=lambda: _open_folder(store.dir))
            open_btn.grid(row=5, column=0, columnspan=3, pady=8)
        else:
            exc, tb = rest
            status_var.set(f"Something went wrong:\n{exc}\n\n"
                           "Check that the folder and GSTR-2A file are correct and try again.")
            try:
                OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
                (OUTPUT_ROOT / "last_error.txt").write_text(tb)
            except Exception:
                pass

    def worker(invoice_root: Path, gstr_path: Path):
        try:
            store, report = _run_pipeline(invoice_root, gstr_path)
            q.put(("ok", store, report))
        except Exception as exc:  # surface a friendly message, keep full trace in a log
            q.put(("err", exc, traceback.format_exc()))

    def on_run():
        if not state["invoice"] or not state["gstr"]:
            status_var.set("Please choose BOTH the invoice folder and the GSTR-2A file first.")
            return
        open_btn.grid_forget()
        run_btn.configure(state="disabled")
        status_var.set("Working… scanning invoices and linking. This can take a minute "
                       "(scanned/photo invoices take longer).")
        bar.grid(row=3, column=0, columnspan=3, pady=4)
        bar.start(12)
        threading.Thread(target=worker, args=(state["invoice"], state["gstr"]),
                         daemon=True).start()
        root.after(150, poll)

    run_btn.configure(command=on_run)
    root.mainloop()


if __name__ == "__main__":
    main()
