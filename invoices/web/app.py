"""Flask app: setup + live progress + review queue + per-document trace viewer.

The app is a *view* over a :class:`RunController` (`web/runner.py`), which owns
the state of the current scan(+link) job. It can be served before any run
exists — the home page lets you choose the invoice folder + GSTR file, kicks off
the job, streams live progress, and then hands you straight into the integrated
review queue. Nothing here re-scans; it renders whatever the controller holds.

Two things it deliberately does *not* do any more:

* **It does not lock you out while a scan runs.** `/review` and `/dashboard` used
  to redirect home whenever `controller.result` was None — which is the entire
  duration of a scan — so the nav bar was dead exactly when you most wanted to
  look around. They now render the live checkpoint instead.
* **It does not hide the linking.** The review queue is built around the GSTR-2A
  match: which B2B rows found no PDF, and which invoices found no row.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)

from ..core.models import DocType, RunResult
from ..io.excel import write_master
from ..io.runstore import RunStore
from ..matching.paths import PathIndex
from ..observability.events import record
from .runner import RunController


def create_app(controller: RunController) -> Flask:
    app = Flask(__name__)
    lock = threading.Lock()
    partial_cache: dict = {"size": -1, "result": None}

    def result():
        """The finished run, or — while one is still scanning — the live checkpoint.

        Everything is checkpointed as it completes, so there is no reason to show
        the user nothing. Cached on checkpoint size so a 500ms poll doesn't reparse
        a 50k-line ledger on every request.
        """
        if controller.result is not None:
            return controller.result
        store = controller.store
        if store is None or not store.checkpoint_path.exists():
            return None
        try:
            size = store.checkpoint_path.stat().st_size
        except OSError:
            return None
        if partial_cache["size"] != size:
            docs = store.checkpoint_docs()
            docs.sort(key=lambda d: d.path)
            partial_cache["size"] = size
            partial_cache["result"] = RunResult(
                run_id=store.run_id, root=store.read_meta().get("root", ""),
                complete=False, documents=docs)
        return partial_cache["result"]

    def doc_by_id(doc_id: str):
        r = result()
        return next((d for d in r.documents if d.id == doc_id), None) if r else None

    def plan():
        return controller.plan

    # `gen` is bumped by every edit; the key also carries the document count so a
    # still-growing checkpoint reindexes on its own.
    index_cache: dict = {"key": None, "index": None, "gen": 0}

    def index():
        """The path search/browse index over *every* PDF in the run.

        Cached, because the search behind it is a typeahead: it runs on every
        keystroke over what may be tens of thousands of documents.
        """
        r = result()
        if r is None:
            return None
        key = (r.run_id, len(r.documents), index_cache["gen"])
        if index_cache["key"] != key:
            index_cache["index"] = PathIndex(r.documents, r.root)
            index_cache["key"] = key
        return index_cache["index"]

    # ---- pages -----------------------------------------------------------
    @app.route("/")
    def home():
        return render_template("home.html", state=controller.run_state())

    @app.route("/dashboard")
    def dashboard():
        r = result()
        if not r:
            return redirect(url_for("home"))
        return render_template("dashboard.html", r=r, summary=r.summary(),
                               metrics=r.stage_metrics, store=controller.store,
                               state=controller.run_state())

    @app.route("/review")
    def review():
        """The linking cockpit: unmatched B2B rows | unlinked invoices | flagged."""
        r = result()
        if not r:
            return redirect(url_for("home"))
        p = plan()
        invoices = r.invoices()
        by_id = {d.id: d for d in r.documents}

        tab = request.args.get("tab") or ("rows" if p else "flagged")
        rows = p.unresolved_rows() if p else []
        unlinked = [by_id[i] for i in (p.unreferenced if p else []) if i in by_id]
        # worst-first: hard flags & low confidence at the top
        flagged = sorted(r.flagged(), key=lambda d: (d.confidence, -len(d.flags)))

        return render_template(
            "review_list.html", tab=tab, plan=p,
            counts=(p.counts() if p else None),
            rows=rows, unlinked=unlinked, flagged=flagged, by_id=by_id,
            total=len(r.documents), invoice_count=len(invoices),
            locked=_locked(), state=controller.run_state())

    @app.route("/doc/<doc_id>")
    def doc_detail(doc_id):
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
        p = plan()
        return render_template("doc_detail.html", d=d,
                               row=(p.by_doc.get(d.id) if p else None),
                               state=controller.run_state(), locked=_locked(),
                               linked=request.args.get("linked"))

    @app.route("/link/row/<int:row>")
    def link_row(row):
        """Find a PDF for one unmatched B2B row.

        Three ways in, because the reason a row is unmatched decides which one can
        possibly work: the field-based near-misses (`suggest`), a typo-tolerant
        search over the original folder paths, and a browser of the folder tree
        itself. The latter two are the only ones that can reach a PDF whose GSTIN
        or number was misread — i.e. most of this queue.
        """
        from ..io.gstr import B2BRow, suggest

        r, p = result(), plan()
        if not r or not p:
            return redirect(url_for("review"))
        rm = next((x for x in p.rows if x.row == row), None)
        if rm is None:
            abort(404)

        invoices = r.invoices()
        scored = suggest(B2BRow(row=rm.row, gstin=rm.gstin,
                                invoice_no=rm.invoice_no), invoices, limit=5)
        # An ambiguous row already has its candidates — show exactly those.
        if rm.status == "ambiguous":
            by_id = {d.id: d for d in invoices}
            scored = [(by_id[i], None) for i in rm.candidates if i in by_id]

        idx = index()
        folders = idx.suggest_folders(rm.supplier) if idx else []
        return render_template("link_row.html", rm=rm, scored=scored,
                               folder_hints=folders, q=(request.args.get("q") or ""),
                               state=controller.run_state())

    # ---- data / actions --------------------------------------------------
    @app.route("/status")
    def status():
        return jsonify(controller.status())

    @app.route("/run_state")
    def run_state():
        return jsonify(controller.run_state())

    @app.route("/api/start", methods=["POST"])
    def api_start():
        data = request.get_json(silent=True) or request.form
        invoice = (data.get("invoice") or "").strip()
        gstr = (data.get("gstr") or "").strip() or None
        source_mode = (data.get("source_mode") or "local").strip()
        upload_folder = (data.get("upload_folder") or "").strip() or None
        fy = (data.get("fy") or "").strip() or None     # "" = All years
        if not invoice:
            where = ("Google Drive folder" if source_mode == "drive"
                     else "invoice folder")
            return jsonify({"ok": False, "error": f"Choose the {where} first."}), 400
        partial_cache["size"] = -1          # a new run invalidates the cached ledger
        res = controller.start(invoice, gstr, source_mode=source_mode,
                               upload_folder_name=upload_folder, fy=fy)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/years")
    def api_years():
        """The FY folders under a root — the year picker's options.

        The client's returns arrive one workbook per financial year, so a run is
        scoped to one year; "All years" ("") still walks the whole tree. Only the
        server can see the tree — local *or* Drive — so the list comes from here.
        """
        root = (request.args.get("root") or "").strip()
        mode = (request.args.get("source_mode") or "local").strip()
        if not root:
            return jsonify({"ok": True, "years": []})
        if mode == "drive":
            if controller.drive is None:
                return jsonify({"ok": False, "years": [],
                                "error": "Connect Google Drive first."}), 400
            from ..io.drive import folder_id_from
            try:
                years = controller.drive.list_fy_folders(folder_id_from(root))
            except Exception as exc:
                return jsonify({"ok": False, "years": [], "error": str(exc)}), 400
            return jsonify({"ok": True, "years": years})
        from ..stages.walk import list_fy_folders
        return jsonify({"ok": True,
                        "years": list_fy_folders(Path(root).expanduser())})

    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        res = controller.pause()
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        res = controller.resume_run()
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        """Stop the scan. Safe: the run stays resumable from its checkpoint."""
        res = controller.stop()
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/drive/auth", methods=["POST"])
    def api_drive_auth():
        """Sign in to Google Drive (opens the system browser for consent)."""
        res = controller.authenticate_drive()
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/drive/upload", methods=["POST"])
    def api_drive_upload():
        """Publish the master + detected invoices to a new Drive folder.

        Backgrounded: it is one Drive round-trip per invoice, so on a real run this
        takes minutes. The page polls /api/task for progress.
        """
        def job(progress) -> None:
            rep = controller.upload_results(on_progress=progress)
            # upload_results reports failure by returning, not raising — surface it as
            # a failed task, or the UI would call a failed upload "done".
            if not rep.get("ok"):
                raise RuntimeError(rep.get("error") or "Upload failed.")

        res = controller.start_task("upload", job)
        return jsonify(res), (200 if res.get("ok") else 409)

    @app.route("/api/docs")
    def api_docs():
        r = result()
        return jsonify([json.loads(d.model_dump_json()) for d in r.documents] if r else [])

    def _hit(h) -> dict:
        """One candidate PDF, flattened for the picker UI.

        `is_invoice` is carried through on purpose: a PDF the classifier didn't
        call an invoice is still bindable (that is often *why* the row went
        unmatched), but the UI has to say so before the reviewer commits.
        """
        d = h.doc
        return {
            "id": d.id, "filename": d.filename,
            "folder": h.folder, "rel": h.rel, "score": h.score,
            "invoice_id": d.fields.invoice_id, "gstin": d.fields.vendor_gstin,
            "vendor": d.fields.vendor_name_pdf, "date": d.fields.invoice_date,
            "company": d.path_info.company, "confidence": round(d.confidence, 2),
            "is_invoice": d.is_invoice, "doc_type": d.doc_type.value,
            "gstr_row": d.gstr_row,
        }

    @app.route("/api/link/search")
    def api_link_search():
        """Typo-tolerant search over the original folder paths *and* the fields.

        This is the second way into a PDF. The matcher keys on (GSTIN, invoice no),
        so when a row goes unmatched at least one of those is usually misread — and
        searching by them again is searching by the thing that already failed. What
        the reviewer still knows is where the file was filed.
        """
        idx = index()
        if idx is None:
            return jsonify({"hits": []})
        q = (request.args.get("q") or "").strip()
        return jsonify({"q": q, "hits": [_hit(h) for h in idx.search(q, limit=20)]})

    @app.route("/api/link/browse")
    def api_link_browse():
        """One level of the original folder tree, for navigating to a PDF by hand."""
        idx = index()
        if idx is None:
            return jsonify({"folders": [], "files": [], "crumbs": [], "prefix": []})
        prefix = [p for p in (request.args.get("prefix") or "").split("/") if p]
        b = idx.browse(prefix)
        return jsonify({
            "prefix": b["prefix"], "crumbs": b["crumbs"], "folders": b["folders"],
            "files": [_hit(h) for h in b["files"]],
        })

    @app.route("/pdf/<doc_id>")
    def pdf(doc_id):
        """Serve a document's PDF, fetching it on demand if it wasn't pre-copied.

        The on-demand path is what lets a *stopped* run skip the bulk pre-copy of
        flagged PDFs (the longest part of the shutdown on Drive) without costing
        the reviewer anything.
        """
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
        for cand in (d.review_pdf_path, d.path):
            if not cand:
                continue
            p = Path(cand)
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            if p.exists():
                return send_file(str(p), mimetype="application/pdf")
        try:
            local = controller.file_source.materialize(d)
        except Exception:
            abort(404)
        return send_file(local, mimetype="application/pdf")

    _EDITABLE_FIELDS = ("invoice_id", "vendor_name_pdf", "vendor_gstin",
                        "invoice_date", "bill_to_name", "bill_to_gstin")
    _MONEY_FIELDS = ("taxable_value", "total_value")

    def _persist(r) -> None:
        # detections.json only — NOT the master workbook. The master is O(corpus) to
        # write (~1s per 20k documents in openpyxl) and rewriting it on every single
        # click made each bind/approve feel broken, for a file nobody reads until the
        # end. `/finish` and `/export` both rewrite it from this same result.
        controller.store.save(r)
        # An edit can change a vendor name or promote a doc to an invoice, both of
        # which the path index has baked in — drop it rather than serve stale hits.
        index_cache["gen"] += 1

    def _locked() -> bool:
        """Review is read-only while a scan — or a long export/upload — is running.

        Mid-scan you are looking at the live checkpoint, and `run_scan` rebuilds
        detections.json from that checkpoint when it finishes — so an edit made now
        would be silently thrown away at the end. Browse freely, edit when it lands.

        An export is also excluded: it reads `result` while writing the workbook, so
        an edit landing mid-write would be half-captured.
        """
        return controller.busy or controller.task_running

    @app.route("/doc/<doc_id>/confirm", methods=["POST"])
    def confirm(doc_id):
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
        if _locked():
            return redirect(url_for("doc_detail", doc_id=doc_id, locked=1))
        with lock:
            form = request.form
            if "company" in form:
                d.path_info.company = form["company"].strip() or None
            for k in _EDITABLE_FIELDS:
                if k in form:
                    setattr(d.fields, k, form[k].strip() or None)
            for k in _MONEY_FIELDS:
                if k in form and form[k].strip():
                    try:
                        setattr(d.fields, k, round(float(form[k].replace(",", "")), 2))
                    except ValueError:
                        pass
            d.reviewed = True
            record(d, "review", "confirmed", "human-confirmed via web UI")
            # Re-match immediately: correcting a GSTIN or an invoice number is
            # *exactly* how an unmatched B2B row gets matched, and the reviewer
            # should see that happen rather than guess whether it helped.
            before = d.gstr_row
            controller.rematch()
            _persist(result())
        landed = d.gstr_row if d.gstr_row != before else None
        if landed:
            return redirect(url_for("doc_detail", doc_id=doc_id, linked=landed))
        return redirect(url_for("review", tab=request.args.get("tab", "flagged")))

    @app.route("/doc/<doc_id>/reject", methods=["POST"])
    def reject(doc_id):
        """Mark a document as not an invoice — it leaves the master and the match."""
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
        if _locked():
            return redirect(url_for("doc_detail", doc_id=doc_id, locked=1))
        with lock:
            d.doc_type = DocType.OTHER
            d.reviewed = True
            record(d, "review", "rejected", "human marked this as not an invoice",
                   severity="warn")
            controller.rematch()
            _persist(result())
        return redirect(url_for("review", tab=request.args.get("tab", "flagged")))

    @app.route("/approve_all", methods=["POST"])
    def approve_all():
        if _locked():
            return redirect(url_for("review", tab="flagged", locked=1))
        with lock:
            r = result()
            docs = r.flagged()   # snapshot before we mutate `reviewed`
            for d in docs:
                d.reviewed = True
                record(d, "review", "confirmed", "bulk-approved via web UI")
            if docs:
                _persist(r)
        return redirect(url_for("review", tab="flagged"))

    @app.route("/link/row/<int:row>/bind", methods=["POST"])
    def bind_row(row):
        """Bind a B2B row to a PDF by hand (or, with no doc_id, unbind it).

        The PDF need not be a *detected* invoice. Searching and browsing reach
        every PDF in the tree, and the whole reason a row lands here is often that
        its invoice was scored an approval or missed — so refusing to bind those
        would send the reviewer to the one place the answer isn't. `match()` and
        `write_linked` resolve bindings against `result.invoices()` only, so a
        human picking a non-invoice is taken as the assertion that it *is* one:
        promote it, and record that a human — not the classifier — said so.
        """
        doc_id = (request.form.get("doc_id") or "").strip() or None
        if _locked():
            return redirect(url_for("review", tab="rows", locked=1))
        with lock:
            d = doc_by_id(doc_id) if doc_id else None
            if doc_id and d is None:
                abort(404)
            if d is not None and not d.is_invoice:
                was = d.doc_type.value
                d.doc_type = DocType.INVOICE
                d.reviewed = True
                record(d, "review", "promoted",
                       f"human bound this to B2B row {row}; was classified "
                       f"'{was}', now treated as an invoice",
                       severity="warn", row=row, was=was)
            controller.store.set_manual_link(row, doc_id)
            controller.rematch()
            _persist(result())
        return redirect(url_for("review", tab="rows"))

    @app.route("/finish", methods=["POST"])
    def finish():
        """Kick off the export on a background thread; the page polls /api/task.

        This used to run inline. On a Drive run it downloads every matched invoice,
        so the request could take *minutes* — the window sat frozen with no progress,
        the user assumed nothing had happened, and every review edit blocked behind
        the same write lock. Now it returns immediately and reports progress.
        """
        r = result()
        if r is None or controller.store is None:
            return jsonify({"ok": False, "error": "No run to export."}), 400
        if _locked():
            return jsonify({"ok": False,
                            "error": "A scan or export is already running."}), 409

        def job(progress) -> None:
            with lock:
                write_master(r, controller.store.master_path())
                if controller.gstr_path:
                    controller.export_linked(on_progress=progress)

        res = controller.start_task("export", job)
        return jsonify(res), (200 if res.get("ok") else 409)

    @app.route("/finish")
    def finish_page():
        """The report page, rendered once the export task has finished."""
        r = result()
        if r is None or controller.store is None:
            return redirect(url_for("home"))
        task = controller.task_state()
        error = task.get("error")
        if error:
            error = f"Linking failed: {error}"
        elif not controller.gstr_path:
            error = ("No GSTR-2A workbook was chosen for this run, so there is "
                     "nothing to link — only the master workbook was written.")
        return render_template("finish.html", report=controller.link_report,
                               error=error,
                               master_path=str(controller.store.master_path()),
                               store=controller.store, remaining=len(r.flagged()),
                               state=controller.run_state())

    @app.route("/api/task")
    def api_task():
        """Progress of the current long job (export / Drive upload)."""
        return jsonify(controller.task_state())

    @app.route("/api/open_output", methods=["POST"])
    def api_open_output():
        """Reveal the run folder in Finder/Explorer.

        Server-side (not just the pywebview bridge) so it also works in the browser
        fallback — the app *is* the local machine here, so there is nothing to be
        gained by making the user copy a path out of the page by hand.
        """
        if controller.store is None:
            return jsonify({"ok": False, "error": "No run yet."}), 400
        d = controller.store.dir.resolve()
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            elif os.name == "nt":
                os.startfile(str(d))            # noqa: S606 - local desktop app
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "path": str(d)}), 500
        return jsonify({"ok": True, "path": str(d)})

    @app.route("/export", methods=["POST"])
    def export():
        if _locked():
            return jsonify({"ok": False, "error": "A scan is still running."}), 409
        with lock:
            path = write_master(result(), controller.store.master_path())
        return jsonify({"ok": True, "path": str(path)})

    return app


def serve(store: RunStore, port: int = 5000, open_browser: bool = True) -> None:
    """CLI entry point: serve a *completed* run (from `scan`/`review`).

    Wraps the finished store in a controller already in the ``done`` phase, so
    the same app renders the dashboard/review without re-scanning.
    """
    controller = RunController(store.dir.parent)
    controller.store = store
    controller.result = store.load()
    # An interrupted run must still look interrupted when reopened, or the UI shows
    # "✓ Scan complete" for a tree it never finished walking.
    controller.phase = "done" if controller.result.complete else "stopped"
    # Restore the GSTR context from the run dir. Without this a reopened run could
    # not link at all — "Finish & export" always claimed no workbook was chosen.
    controller.gstr_path = store.gstr_path()
    controller.plan = store.load_link()
    # Restore the year this run was scoped to, and the inputs the setup screen (and
    # the Resume button) rehydrate from. Without the year, resuming a reopened
    # one-year run would post no scope and rescan the whole tree from zero.
    meta = store.read_meta()
    controller.fy = store.fy()
    controller.last_input = {
        "invoice": meta.get("root", ""), "gstr": str(controller.gstr_path or ""),
        "source_mode": "local", "upload_folder": "", "fy": controller.fy or "",
    }

    app = create_app(controller)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(port=port, debug=False, use_reloader=False)
