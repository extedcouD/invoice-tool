"""Flask app: review queue + live dashboard + per-document trace viewer.

One app instance is bound to one run (a RunStore). It reads detections.json
into memory, serves the review workflow, and writes corrections back (then
re-exports the workbook). The dashboard polls status.json so it also reflects a
scan that is still in progress.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from ..core.models import RunResult
from ..io.excel import write_master
from ..io.runstore import RunStore


def create_app(store: RunStore) -> Flask:
    app = Flask(__name__)
    state: dict[str, RunResult] = {"result": store.load()}
    lock = threading.Lock()

    def result() -> RunResult:
        return state["result"]

    def doc_by_id(doc_id: str):
        return next((d for d in result().documents if d.id == doc_id), None)

    # ---- pages -----------------------------------------------------------
    @app.route("/")
    def dashboard():
        r = result()
        return render_template("dashboard.html", r=r, summary=r.summary(),
                               metrics=r.stage_metrics, store=store)

    @app.route("/review")
    def review():
        r = result()
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
        if store.status_path.exists():
            return jsonify(json.loads(store.status_path.read_text()))
        return jsonify({"state": "unknown"})

    @app.route("/api/docs")
    def api_docs():
        return jsonify([json.loads(d.model_dump_json()) for d in result().documents])

    @app.route("/pdf/<doc_id>")
    def pdf(doc_id):
        d = doc_by_id(doc_id)
        if not d:
            abort(404)
        candidates = [d.review_pdf_path, d.path]
        for cand in candidates:
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
            store.save(result())
            write_master(result(), store.master_path())
        return redirect(url_for("review"))

    @app.route("/export", methods=["POST"])
    def export():
        with lock:
            path = write_master(result(), store.master_path())
        return jsonify({"ok": True, "path": str(path)})

    return app


def serve(store: RunStore, port: int = 5000, open_browser: bool = True) -> None:
    app = create_app(store)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(port=port, debug=False, use_reloader=False)
