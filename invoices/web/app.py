"""Flask app: setup + live progress + review queue + per-document trace viewer.

The app is a *view* over a :class:`RunController` (`web/runner.py`), which owns
the state of the current scan(+link) job. It can be served before any run
exists — the home page lets you choose the invoice folder + GSTR file, kicks off
the job, streams live progress, and then hands you straight into the integrated
review queue. `review`/`export` write corrections back and re-export the
workbook. Nothing here re-scans; it renders whatever the controller holds.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)

from ..io.excel import write_master
from ..io.runstore import RunStore
from .runner import RunController


def create_app(controller: RunController) -> Flask:
    app = Flask(__name__)
    lock = threading.Lock()

    def result():
        return controller.result

    def doc_by_id(doc_id: str):
        r = result()
        return next((d for d in r.documents if d.id == doc_id), None) if r else None

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
                               metrics=r.stage_metrics, store=controller.store)

    @app.route("/review")
    def review():
        r = result()
        if not r:
            return redirect(url_for("home"))
        # sort worst-first: hard flags & low confidence at the top
        docs = sorted(r.flagged(), key=lambda d: (d.confidence, -len(d.flags)))
        return render_template("review_list.html", docs=docs, total=len(r.documents))

    @app.route("/doc/<doc_id>")
    def doc_detail(doc_id):
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
        return render_template("doc_detail.html", d=d)

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
        res = controller.start(invoice, gstr, source_mode=source_mode,
                               upload_folder_name=upload_folder)
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
        abort(404)

    _EDITABLE_FIELDS = ("invoice_id", "vendor_name_pdf", "vendor_gstin",
                        "invoice_date", "bill_to_name", "bill_to_gstin")
    _MONEY_FIELDS = ("taxable_value", "total_value")

    @app.route("/doc/<doc_id>/confirm", methods=["POST"])
    def confirm(doc_id):
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
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
            from ..observability.events import record
            record(d, "review", "confirmed", "human-confirmed via web UI")
            controller.store.save(result())
            write_master(result(), controller.store.master_path())
        return redirect(url_for("review"))

    @app.route("/approve_all", methods=["POST"])
    def approve_all():
        with lock:
            from ..observability.events import record
            docs = result().flagged()  # snapshot before we mutate `reviewed`
            for d in docs:
                d.reviewed = True
                record(d, "review", "confirmed", "bulk-approved via web UI")
            if docs:
                controller.store.save(result())
                write_master(result(), controller.store.master_path())
        return redirect(url_for("review"))

    @app.route("/finish", methods=["POST"])
    def finish():
        """Finalize review: write the master, then (deferred) link to GSTR-2A.

        Runs *after* review so corrections reach the linked workbook + flat
        invoice folder, and renders a page naming every file it wrote.
        """
        r = result()
        if r is None or controller.store is None:
            return redirect(url_for("home"))
        report, error = None, None
        with lock:
            master = write_master(r, controller.store.master_path())
            if controller.gstr_path:
                try:
                    report = controller.link_now()
                except Exception as exc:  # surface on-page, don't 500
                    error = f"Linking failed: {exc}"
            else:
                error = ("No GSTR-2A workbook was chosen for this run, so there is "
                         "nothing to link — only the master workbook was written.")
        return render_template("finish.html", report=report, error=error,
                               master_path=str(master), store=controller.store,
                               remaining=len(r.flagged()))

    @app.route("/export", methods=["POST"])
    def export():
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
    controller.phase = "done"

    app = create_app(controller)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(port=port, debug=False, use_reloader=False)
