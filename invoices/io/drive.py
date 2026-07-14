"""Google Drive backend: OAuth + tree traversal + on-demand download/upload.

This lets the app read a Kotak invoice tree that lives on Google Drive and write
results back, without staging 120 GB locally. It plugs into the existing pipeline
through two seams from Phase 1:

  * ``walk_drive`` produces the same seed :class:`Document`s as the local
    ``walk`` (reusing its path-labelling), tagged with a Drive ``file_id``.
  * ``DriveFileSource`` downloads each PDF to a temp file on demand, so the
    PyMuPDF/OCR extract code runs unchanged.

Only PDFs under ``.../Payments/<scope>/...`` are ever downloaded — the scope gate
mirrors the local walk, so out-of-scope subtrees cost a metadata listing but no
content transfer.

Auth is the OAuth 2.0 *installed-app* loopback flow. On first use it opens the
system browser for consent and caches the token; afterwards it refreshes
silently.

The OAuth client is configured with user type **Internal** (one Google Workspace
org). Google waives verification for internal apps, so the *restricted*
``drive.readonly`` scope works with no security assessment, no consent warning,
no test-user list, and — unlike Testing mode — no 7-day refresh-token expiry.
Output upload uses the non-sensitive ``drive.file`` scope.

Because it is one org-wide client, the ``client_secret.json`` ships **inside the
bundle** (see :func:`resolve_client_secret`) — end users sign in and nothing else.
A desktop OAuth client secret is not a true secret (Google's native-app guidance
assumes it can be extracted), and Internal means only org accounts can consent
with it anyway. It is still kept out of git and injected at build time, so
GitHub's secret scanner can't get it auto-revoked.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..config import RE_FY, Settings, DEFAULTS
from ..core.control import RunControl
from ..core.interfaces import FileSource
from ..core.models import Document
from ..stages.walk import _bank_from, _parse_path, keep_fy_dir, seed_document

# drive.readonly (restricted) to read the user's existing tree; drive.file
# (non-sensitive) to create + upload into the app's own output folder.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"   # native Sheet: export, don't download


def folder_id_from(value: str) -> str:
    """Accept a raw folder id or a Drive folder URL and return the id.

    Handles ``https://drive.google.com/drive/folders/<id>?...`` and
    ``...?id=<id>`` links, or a bare id.
    """
    import re

    value = (value or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_\-]+)", value)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_\-]+)", value)
    if m:
        return m.group(1)
    return value


def file_id_from(value: str) -> str:
    """Accept a raw file id or any Drive/Sheets *file* URL and return the id.

    Covers the two links a user can realistically copy for a GSTR-2A workbook:
    an uploaded .xlsx (``/file/d/<id>/view``) and a native Google Sheet
    (``/spreadsheets/d/<id>/edit``), plus ``?id=`` and bare ids.
    """
    import re

    value = (value or "").strip()
    for pat in (r"/file/d/([A-Za-z0-9_\-]+)",
                r"/spreadsheets/d/([A-Za-z0-9_\-]+)",
                r"/document/d/([A-Za-z0-9_\-]+)",
                r"[?&]id=([A-Za-z0-9_\-]+)"):
        m = re.search(pat, value)
        if m:
            return m.group(1)
    return value

# Transient Drive errors we retry with exponential backoff.
_RETRY_STATUS = {403, 429, 500, 502, 503, 504}


def app_support_dir() -> Path:
    """Per-user app dir holding client_secret.json + the cached token.json."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "InvoiceGSTRLinker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_client_secret() -> Optional[Path]:
    """The org's OAuth client shipped with the app, if this is a frozen build.

    PyInstaller unpacks ``datas`` under ``sys._MEIPASS``; the spec places the
    secret at the top of that tree. Source checkouts have no bundled secret and
    fall back to the app-support dir.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    p = Path(base) / "client_secret.json"
    return p if p.exists() else None


def resolve_client_secret(explicit: Optional[Path] = None) -> Optional[Path]:
    """Locate the OAuth client secret: explicit > user override > bundled.

    The app-support copy wins over the bundled one so a developer (or a second
    org) can point the app at a different Cloud project without a rebuild.
    """
    if explicit:
        return Path(explicit)
    override = app_support_dir() / "client_secret.json"
    if override.exists():
        return override
    return bundled_client_secret()


class DriveAuthError(RuntimeError):
    """Raised when Drive credentials are missing or consent fails."""


class DriveClient:
    """Thin wrapper over the Drive v3 API with retry/backoff.

    **Thread-safety.** ``google-api-python-client`` is built on ``httplib2``, which
    is *not* thread-safe: sharing one service object across the scan's worker
    threads means several threads driving one TLS connection, which corrupts
    OpenSSL's heap and hard-crashes the process (SIGTRAP, "memory corruption of
    free block" — no Python traceback, because the damage is in C).

    So the service is **thread-local**: each worker builds its own on first use and
    reuses it thereafter — one connection per thread, not one per request. The
    credentials object is shared, which is fine: a concurrent double-refresh just
    mints two valid access tokens.
    """

    def __init__(self, creds) -> None:
        self._creds = creds
        self._tl = threading.local()

    @property
    def _svc(self):
        svc = getattr(self._tl, "svc", None)
        if svc is None:
            from googleapiclient.discovery import build
            # static_discovery=True uses the discovery doc bundled with the
            # library, so building per-thread costs no network round-trip.
            svc = build("drive", "v3", credentials=self._creds,
                        cache_discovery=False, static_discovery=True)
            self._tl.svc = svc
        return svc

    # ---- auth --------------------------------------------------------------
    @classmethod
    def authenticate(cls, client_secret_path: Optional[Path] = None,
                     token_path: Optional[Path] = None,
                     open_browser: bool = True) -> "DriveClient":
        """Build an authenticated client, running the consent flow if needed."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise DriveAuthError(
                "Google API libraries are not installed "
                "(google-api-python-client, google-auth-oauthlib)."
            ) from exc

        secret = resolve_client_secret(client_secret_path)
        token = Path(token_path) if token_path else app_support_dir() / "token.json"

        creds = None
        if token.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token), SCOPES)
            except (ValueError, OSError):
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    # A cached token is tied to the OAuth client that minted it, so
                    # it dies when the client changes (e.g. the old External/Testing
                    # client -> the org's Internal one) as well as on revoke/expiry.
                    # Re-consent instead of dead-ending the user on an OAuth error.
                    creds = None
            if not creds or not creds.valid:
                if secret is None or not secret.exists():
                    raise DriveAuthError(
                        "This build has no Google OAuth client bundled, so it can't "
                        "connect to Drive. A release build ships the organisation's "
                        f"client; to use your own, save a 'Desktop app' client_secret.json "
                        f"at {app_support_dir() / 'client_secret.json'} "
                        "(Drive API enabled). See docs/DRIVE_SETUP.md."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
                creds = flow.run_local_server(port=0, open_browser=open_browser)
            token.write_text(creds.to_json())

        # Services are built lazily per thread (see _svc) — not here.
        return cls(creds)

    # ---- low-level with retry ---------------------------------------------
    def _execute(self, request):
        from googleapiclient.errors import HttpError

        delay = 1.0
        for attempt in range(6):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "resp", None), "status", None)
                if status not in _RETRY_STATUS or attempt == 5:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError("unreachable")

    # ---- listing / traversal ----------------------------------------------
    def list_children(self, folder_id: str) -> list[dict]:
        """All non-trashed children of a folder (id,name,mimeType,modifiedTime,size)."""
        out: list[dict] = []
        page_token = None
        while True:
            resp = self._execute(self._svc.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,modifiedTime,size)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ))
            out.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def list_fy_folders(self, root_id: str) -> list[str]:
        """FY folder names directly under ``root_id`` — the year picker's options."""
        return sorted(c["name"] for c in self.list_children(root_id)
                      if c.get("mimeType") == FOLDER_MIME
                      and RE_FY.match(c.get("name", "")))

    def walk_pdf_tree(self, root_id: str,
                      control: "RunControl | None" = None,
                      descend: Optional[Callable[[list[str]], bool]] = None
                      ) -> Iterator[tuple[list[str], dict]]:
        """Depth-first yield of ``(folder_parts, file)`` for every PDF in the tree.

        ``folder_parts`` is the list of folder names from (but excluding) the
        root down to the file's parent — the same shape ``walk`` feeds to
        ``_parse_path``. Metadata only; no content is downloaded here.

        ``control`` is checked once per folder listing — the finest granularity
        available, since each listing is a blocking network round-trip.

        ``descend`` (optional) is asked about each child folder *before* it is
        pushed, and receives that folder's OWN parts (``parts + [name]`` — inside
        this loop ``parts`` is still the parent's). It is what makes a year-scoped
        run cheap: the scope gate in ``walk_drive`` runs *after* the listing, so on
        its own it only prevents downloads — without this we would still pay one
        paginated files.list round-trip for every folder of every other year.
        """
        stack: list[tuple[str, list[str]]] = [(root_id, [])]
        while stack:
            if control is not None:
                control.gate()
            folder_id, parts = stack.pop()
            for child in self.list_children(folder_id):
                mime = child.get("mimeType")
                name = child.get("name", "")
                if mime == FOLDER_MIME:
                    child_parts = parts + [name]
                    if descend is None or descend(child_parts):
                        stack.append((child["id"], child_parts))
                elif mime == PDF_MIME or name.lower().endswith(".pdf"):
                    yield parts, child

    # ---- download / upload -------------------------------------------------
    def get_meta(self, file_id: str) -> dict:
        return self._execute(self._svc.files().get(
            fileId=file_id, fields="id,name,mimeType,size",
            supportsAllDrives=True))

    def download_to_temp(self, file_id: str, suffix: str = ".pdf") -> str:
        """Download a binary file to a temp file and return its local path."""
        import tempfile

        from googleapiclient.http import MediaIoBaseDownload

        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="invdrive_")
        os.close(fd)
        try:
            with open(tmp, "wb") as fh:
                downloader = MediaIoBaseDownload(
                    fh, self._svc.files().get_media(
                        fileId=file_id, supportsAllDrives=True))
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return tmp
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def download_workbook_to_temp(self, file_id: str) -> str:
        """Fetch a GSTR-2A workbook as a local .xlsx, whatever it is on Drive.

        A native Google Sheet has no bytes to download — ``get_media`` fails on it
        — so it has to be *exported* to xlsx instead. Users paste whichever link
        they have, so handle both rather than making them convert by hand.
        """
        import tempfile

        from googleapiclient.http import MediaIoBaseDownload

        meta = self.get_meta(file_id)
        mime = meta.get("mimeType", "")
        if mime == FOLDER_MIME:
            raise ValueError(
                f"{meta.get('name', file_id)!r} is a folder, not a GSTR-2A workbook.")

        if mime != SHEET_MIME:
            return self.download_to_temp(file_id, suffix=".xlsx")

        fd, tmp = tempfile.mkstemp(suffix=".xlsx", prefix="invgstr_")
        os.close(fd)
        try:
            with open(tmp, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, self._svc.files().export_media(
                    fileId=file_id, mimeType=XLSX_MIME))
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return tmp
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def copy_file(self, file_id: str, parent_id: str,
                  name: Optional[str] = None) -> str:
        """Server-side copy of a Drive file into ``parent_id`` (no download/upload).

        Lets us assemble the output invoice folder from the originals without
        moving bytes over a slow link. Reads the source (drive.readonly) and
        creates an app-owned copy (drive.file); the original is untouched.
        """
        body: dict = {"parents": [parent_id]}
        if name:
            body["name"] = name
        created = self._execute(self._svc.files().copy(
            fileId=file_id, body=body, fields="id", supportsAllDrives=True))
        return created["id"]

    def create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        meta = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            meta["parents"] = [parent_id]
        created = self._execute(self._svc.files().create(
            body=meta, fields="id", supportsAllDrives=True))
        return created["id"]

    def upload_file(self, local_path: str | Path, parent_id: str,
                    name: Optional[str] = None,
                    mime: str = "application/octet-stream") -> str:
        from googleapiclient.http import MediaFileUpload

        local_path = Path(local_path)
        meta = {"name": name or local_path.name, "parents": [parent_id]}
        media = MediaFileUpload(str(local_path), mimetype=mime, resumable=True)
        created = self._execute(self._svc.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True))
        return created["id"]


class DriveFileSource(FileSource):
    """FileSource backed by Google Drive: downloads each doc to a temp file."""

    def __init__(self, client: DriveClient) -> None:
        self.client = client

    def materialize(self, doc: Document) -> str:
        if not doc.drive_file_id:
            raise ValueError(f"document {doc.id} has no drive_file_id")
        return self.client.download_to_temp(doc.drive_file_id)

    def cleanup(self, doc: Document, path: str) -> None:
        try:
            os.remove(path)  # path is a temp download, safe to delete
        except OSError:
            pass


def walk_drive(root_id: str, settings: Settings = DEFAULTS, *,
               client: DriveClient,
               control: "RunControl | None" = None) -> Iterator[Document]:
    """Drive analogue of :func:`invoices.stages.walk.walk`.

    Yields seed Documents for every in-scope (``Payments/<scope>``) PDF under
    ``root_id``, reusing the local walk's tolerant path labelling and the same
    review flags, but keyed on the Drive file id.

    A generator, and gated: enumerating a large Drive tree is one paginated
    network round-trip per folder and can run for minutes, during which Stop used
    to do nothing at all.
    """
    # One financial year per run — prune the TRAVERSAL, not just the yield, or we
    # still network-list every folder of every other year (minutes, on a 120 GB
    # tree, for nothing). root_id stays the TREE root, so the FY folder is still in
    # folder_parts and _parse_path still sets info.fy — re-rooting at the FY
    # folder's own id would drop it and flag every doc `path_incomplete`.
    descend = None
    if settings.fy_scope:
        def descend(parts: list[str]) -> bool:
            return keep_fy_dir(parts[-1], settings.fy_scope)

    for folder_parts, f in client.walk_pdf_tree(root_id, control=control,
                                                descend=descend):
        bank = _bank_from(folder_parts, settings.bank_scope)
        if not bank or bank.lower() != settings.bank_scope.lower():
            continue  # scope gate — never downloads out-of-scope content

        info = _parse_path(folder_parts)
        info.bank = info.bank or bank
        modified = f.get("modifiedTime", "")

        yield seed_document(
            source_key=f"drive:{f['id']}@{modified}",
            path="drive://" + "/".join(folder_parts + [f["name"]]),
            filename=f["name"],
            size_bytes=int(f.get("size") or 0),
            info=info,
            drive_file_id=f["id"],
            drive_modified_time=modified,
        )
