# PyInstaller spec — builds a double-click desktop app that bundles Python, all
# deps, AND the tesseract OCR engine, so the end user installs nothing.
#
# Build (same command on macOS and Windows):
#     pyinstaller packaging/InvoiceGSTRLinker.spec --noconfirm
#
# tesseract must be installed on the BUILD machine (CI does this: `brew install
# tesseract` / `choco install tesseract`). This spec locates it, copies the binary
# (+ Windows DLLs) and eng.traineddata into the bundle; invoices/gui.py points
# pytesseract at them at runtime.
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # packaging/ -> repo root
IS_WIN = os.name == "nt"


def _find_tesseract() -> Path:
    exe = shutil.which("tesseract")
    if exe:
        return Path(exe).resolve()
    for c in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"):
        if Path(c).exists():
            return Path(c).resolve()
    raise SystemExit("tesseract not found on the build machine — install it first "
                     "(brew install tesseract / choco install tesseract).")


def _find_tessdata(tess_exe: Path) -> Path:
    cands = []
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        cands += [Path(env), Path(env) / "tessdata"]
    cands += [tess_exe.parent / "tessdata"]           # Windows layout
    cands += [tess_exe.parent.parent / "share" / "tessdata"]  # brew/unix layout
    if not IS_WIN:
        try:
            prefix = subprocess.check_output(["brew", "--prefix", "tesseract"], text=True).strip()
            cands.append(Path(prefix) / "share" / "tessdata")
        except Exception:
            pass
    for c in cands:
        if (c / "eng.traineddata").exists():
            return c
    raise SystemExit(f"eng.traineddata not found; looked in: {[str(c) for c in cands]}")


tess_exe = _find_tesseract()
tessdata = _find_tessdata(tess_exe)

# Adding the tesseract binary via `binaries` makes PyInstaller follow and rewrite its
# shared-library dependencies (libtesseract/leptonica/libarchive on macOS).
binaries = [(str(tess_exe), "tesseract")]
if IS_WIN:  # on Windows the DLLs sit next to the exe — bundle them all
    for dll in tess_exe.parent.glob("*.dll"):
        binaries.append((str(dll), "tesseract"))

# Bundle the WHOLE tessdata tree, not just eng.traineddata: pytesseract invokes
# tesseract with the `txt` output config, which lives in tessdata/configs/ — without
# it OCR fails with "read_params_file: Can't open txt".
datas = []
for f in tessdata.rglob("*"):
    if f.is_file():
        rel = f.relative_to(tessdata).parent
        dest = "tesseract/tessdata" if str(rel) == "." else os.path.join("tesseract", "tessdata", str(rel))
        datas.append((str(f), dest))

# The desktop app hosts the Flask UI, so its Jinja templates must ride along in
# the bundle (Flask loads them from invoices/web/templates at runtime).
for f in (ROOT / "invoices" / "web" / "templates").glob("*.html"):
    datas.append((str(f), os.path.join("invoices", "web", "templates")))

# pywebview: bundle its platform backends (cocoa / edgechromium / gtk / …) and
# any data files. collect_submodules picks up the backend that's imported lazily.
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
datas += collect_data_files("webview")
hiddenimports = ["pytesseract"] + collect_submodules("webview")

a = Analysis(
    [str(ROOT / "run_gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pandas", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "notebook", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="InvoiceGSTRLinker",
    console=False,            # windowed app, no terminal
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="InvoiceGSTRLinker")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="InvoiceGSTRLinker.app",
        bundle_identifier="com.invoicetool.gstrlinker",
        info_plist={"NSHighResolutionCapable": True},
    )
