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
import threading
import webbrowser
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)

from ..core.models import DocType, RunResult
from ..io.excel import write_master
from ..io.runstore import RunStore
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
        """Find a PDF for one unmatched B2B row: ranked suggestions + a search box."""
        from ..io.gstr import B2BRow, suggest

        r, p = result(), plan()
        if not r or not p:
            return redirect(url_for("review"))
        rm = next((x for x in p.rows if x.row == row), None)
        if rm is None:
            abort(404)

        invoices = r.invoices()
        q = (request.args.get("q") or "").strip()
        if q:
            ql = q.lower()
            cands = [d for d in invoices
                     if ql in (d.fields.invoice_id or "").lower()
                     or ql in (d.fields.invoice_no_content or "").lower()
                     or ql in (d.fields.vendor_name_pdf or "").lower()
                     or ql in (d.fields.vendor_gstin or "").lower()
                     or ql in d.filename.lower()][:25]
            scored = [(d, None) for d in cands]
        else:
            scored = suggest(B2BRow(row=rm.row, gstin=rm.gstin,
                                    invoice_no=rm.invoice_no), invoices, limit=5)
        # An ambiguous row already has its candidates — show exactly those.
        if rm.status == "ambiguous" and not q:
            by_id = {d.id: d for d in invoices}
            scored = [(by_id[i], None) for i in rm.candidates if i in by_id]

        return render_template("link_row.html", rm=rm, scored=scored, q=q,
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
        if not invoice:
            where = ("Google Drive folder" if source_mode == "drive"
                     else "invoice folder")
            return jsonify({"ok": False, "error": f"Choose the {where} first."}), 400
        partial_cache["size"] = -1          # a new run invalidates the cached ledger
        res = controller.start(invoice, gstr, source_mode=source_mode,
                               upload_folder_name=upload_folder)
        return jsonify(res), (200 if res.get("ok") else 400)

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
        """Publish the master + detected invoices to a new Drive folder."""
        res = controller.upload_results()
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/docs")
    def api_docs():
        r = result()
        return jsonify([json.loads(d.model_dump_json()) for d in r.documents] if r else [])

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
        controller.store.save(r)
        write_master(r, controller.store.master_path())

    def _locked() -> bool:
        """Review is read-only while a scan is running.

        Mid-scan you are looking at the live checkpoint, and `run_scan` rebuilds
        detections.json from that checkpoint when it finishes — so an edit made now
        would be silently thrown away at the end. Browse freely, edit when it lands.
        """
        return controller.busy

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
        """Bind a B2B row to a detected invoice by hand (or unbind it)."""
        doc_id = (request.form.get("doc_id") or "").strip() or None
        if _locked():
            return redirect(url_for("review", tab="rows", locked=1))
        with lock:
            controller.store.set_manual_link(row, doc_id)
            controller.rematch()
            _persist(result())
        return redirect(url_for("review", tab="rows"))

    @app.route("/finish", methods=["POST"])
    def finish():
        """Finalize review: write the master, then the linked workbook + flat folder.

        Runs *after* review so corrections reach the linked workbook, and renders a
        page naming every file it wrote.
        """
        r = result()
        if r is None or controller.store is None:
            return redirect(url_for("home"))
        if _locked():
            return redirect(url_for("review", locked=1))
        report, error = None, None
        with lock:
            master = write_master(r, controller.store.master_path())
            if controller.gstr_path:
                try:
                    report = controller.export_linked()
                except Exception as exc:  # surface on-page, don't 500
                    error = f"Linking failed: {exc}"
            else:
                error = ("No GSTR-2A workbook was chosen for this run, so there is "
                         "nothing to link — only the master workbook was written.")
        return render_template("finish.html", report=report, error=error,
                               master_path=str(master), store=controller.store,
                               remaining=len(r.flagged()),
                               state=controller.run_state())

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

    app = create_app(controller)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(port=port, debug=False, use_reloader=False)
