"""Second local site used for cross-origin and HEAD fallback checks."""

from __future__ import annotations

from collections import defaultdict

from flask import Flask, Response, abort, request

from . import pages

def create_ext_app() -> Flask:
    app = Flask(__name__)
    counters: dict[str, int] = defaultdict(int)
    app.config["COUNTERS"] = counters

    @app.get("/ok")
    def ok():
        return Response(pages.EXT_OK, content_type="text/html; charset=utf-8")

    @app.get("/missing")
    def missing():
        abort(404)

    @app.route("/head-only", methods=["GET", "HEAD"])
    def head_only():
        if request.method == "HEAD":
            return Response("", status=405)
        return Response(pages.EXT_HEAD_ONLY, content_type="text/html; charset=utf-8")

    @app.route("/fail/<int:index>", methods=["GET", "HEAD"])
    def fail(index):
        # Persistently failing endpoint used by circuit-breaker tests.
        counters["fail"] += 1
        return Response(pages.EXT_FAIL, status=500, content_type="text/html")

    @app.errorhandler(404)
    def not_found(_error):
        return Response(pages.EXT_NOT_FOUND, status=404, content_type="text/html")

    return app
