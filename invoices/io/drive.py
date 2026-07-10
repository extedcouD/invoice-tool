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
silently. For an internal (Testing-mode) OAuth client the restricted
``drive.readonly`` scope works without Google verification, but refresh tokens
expire after ~7 days, so users re-consent about weekly. Output upload uses the
non-sensitive ``drive.file`` scope (app-created files only).

Prerequisite: a Google Cloud project with the Drive API enabled and a "Desktop
app" OAuth client; drop its ``client_secret.json`` in the app-support dir
(see :func:`app_support_dir`).
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

from ..config import Settings, DEFAULTS
from ..core.interfaces import FileSource
from ..core.models import Document
from ..stages.walk import _bank_from, _parse_path

# drive.readonly (restricted) to read the user's existing tree; drive.file
# (non-sensitive) to create + upload into the app's own output folder.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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


class DriveAuthError(RuntimeError):
    """Raised when Drive credentials are missing or consent fails."""


class DriveClient:
    """Thin wrapper over the Drive v3 API with retry/backoff."""

    def __init__(self, service) -> None:
        self._svc = service

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
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise DriveAuthError(
                "Google API libraries are not installed "
                "(google-api-python-client, google-auth-oauthlib)."
            ) from exc

        secret = Path(client_secret_path) if client_secret_path else \
            app_support_dir() / "client_secret.json"
        token = Path(token_path) if token_path else app_support_dir() / "token.json"

        creds = None
        if token.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token), SCOPES)
            except (ValueError, OSError):
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not secret.exists():
                    raise DriveAuthError(
                        f"Google OAuth client secret not found at {secret}. Create a "
                        "'Desktop app' OAuth client in Google Cloud (Drive API enabled) "
                        "and save its client_secret.json there. See docs/DRIVE_SETUP.md."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
                creds = flow.run_local_server(port=0, open_browser=open_browser)
            token.write_text(creds.to_json())

        # static_discovery=True uses the discovery doc bundled with the library
        # (no extra network fetch — important inside the packaged app).
        service = build("drive", "v3", credentials=creds,
                        cache_discovery=False, static_discovery=True)
        return cls(service)

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

    def walk_pdf_tree(self, root_id: str) -> Iterator[tuple[list[str], dict]]:
        """Depth-first yield of ``(folder_parts, file)`` for every PDF in the tree.

        ``folder_parts`` is the list of folder names from (but excluding) the
        root down to the file's parent — the same shape ``walk`` feeds to
        ``_parse_path``. Metadata only; no content is downloaded here.
        """
        stack: list[tuple[str, list[str]]] = [(root_id, [])]
        while stack:
            folder_id, parts = stack.pop()
            for child in self.list_children(folder_id):
                mime = child.get("mimeType")
                name = child.get("name", "")
                if mime == FOLDER_MIME:
                    stack.append((child["id"], parts + [name]))
                elif mime == PDF_MIME or name.lower().endswith(".pdf"):
                    yield parts, child

    # ---- download / upload -------------------------------------------------
    def download_to_temp(self, file_id: str) -> str:
        """Download a file to a temp .pdf and return its local path."""
        import tempfile

        from googleapiclient.http import MediaIoBaseDownload

        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="invdrive_")
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
               client: DriveClient) -> list[Document]:
    """Drive analogue of :func:`invoices.stages.walk.walk`.

    Produces seed Documents for every in-scope (``Payments/<scope>``) PDF under
    ``root_id``, reusing the local walk's tolerant path labelling and the same
    review flags, but keyed on the Drive file id.
    """
    from ..observability.events import record

    docs: list[Document] = []
    idx = 0
    for folder_parts, f in client.walk_pdf_tree(root_id):
        bank = _bank_from(folder_parts, settings.bank_scope)
        if not bank or bank.lower() != settings.bank_scope.lower():
            continue  # scope gate — never downloads out-of-scope content

        info = _parse_path(folder_parts)
        info.bank = info.bank or bank
        display_path = "drive://" + "/".join(folder_parts + [f["name"]])
        modified = f.get("modifiedTime", "")

        doc = Document(
            id=f"d{idx:05d}",
            path=display_path,
            filename=f["name"],
            size_bytes=int(f.get("size") or 0),
            source_key=f"drive:{f['id']}@{modified}",
            drive_file_id=f["id"],
            drive_modified_time=modified,
            path_info=info,
        )
        record(doc, "walk", "discovered",
               f"fy={info.fy} month={info.month} date={info.date_folder} "
               f"company={info.company}", company=info.company, fy=info.fy)
        if info.company is None:
            doc.add_flag("no_company_folder", "could not derive company from path")
        if not (info.fy and info.month and info.date_folder):
            doc.add_flag("path_incomplete", "missing FY/month/date folder level")
        docs.append(doc)
        idx += 1
    return docs
