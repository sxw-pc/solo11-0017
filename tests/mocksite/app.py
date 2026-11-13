"""Primary mock site covering every link class used by the tests."""

from __future__ import annotations

import time
from collections import defaultdict

from flask import Flask, Response, abort, request

from . import pages

def create_mock_app(ext_base: str = "http://127.0.0.1:9",
                    unstable_failures: int = 2) -> Flask:
    app = Flask(__name__)
    counters: dict[str, int] = defaultdict(int)
    app.config["COUNTERS"] = counters
    app.config["UNSTABLE_FAILURES"] = unstable_failures
    app.config["REGRESSION_BROKEN"] = False
    app.config["EXT_FARM_LINKS"] = 12

    def html(body: str, status: int = 200, title: str = "Mock"):
        return Response(body, status=status, content_type="text/html; charset=utf-8")

    @app.get("/")
    def home():
        return html(pages.HOME.format(ext=ext_base.rstrip("/")))

    @app.get("/about")
    def about():
        return html(pages.ABOUT)

    @app.get("/section/index")
    def section_index():
        return html(pages.SECTION)

    @app.get("/section/a")
    @app.get("/section/b")
    def section_leaf():
        return html(pages.SECTION_END)

    @app.get("/depth/1")
    @app.get("/depth/2")
    @app.get("/depth/3")
    @app.get("/depth/4")
    def depth():
        current = int(request.path.rsplit("/", 1)[1])
        if current >= 4:
            next_url = "/"
        else:
            next_url = f"/depth/{current + 1}"
        body = pages.DEPTH.format(title=f"Depth {current}", next_url=next_url)
        return html(body)

    @app.get("/anchor-page")
    def anchor_page():
        return html(pages.ANCHOR_PAGE)

    @app.get("/dead-end")
    def dead_end():
        return html(pages.DEAD_END)

    @app.get("/orphan")
    def orphan():
        return html(pages.ORPHAN)

    @app.get("/missing")
    def missing():
        abort(404)

    @app.get("/server-error")
    def server_error():
        return html(pages.SERVER_ERROR, status=500)

    @app.get("/hop-1")
    def hop1():
        return Response("", 302, {"Location": "/hop-2"})

    @app.get("/hop-2")
    def hop2():
        return Response("", 301, {"Location": "/hop-3"})

    @app.get("/hop-3")
    def hop3():
        return Response("", 307, {"Location": "/landing"})

    @app.get("/loop-a")
    def loop_a():
        return Response("", 302, {"Location": "/loop-b"})

    @app.get("/loop-b")
    def loop_b():
        return Response("", 302, {"Location": "/loop-a"})

    @app.get("/landing")
    def landing():
        return html(pages.LANDING)

    @app.get("/slow")
    def slow():
        time.sleep(0.35)
        return html(pages.SLOW)

    @app.get("/unstable")
    def unstable():
        counters["unstable"] += 1
        if counters["unstable"] <= app.config["UNSTABLE_FAILURES"]:
            return html(pages.SERVER_ERROR, status=500)
        return html(pages.RECOVERED)

    @app.get("/regression")
    def regression():
        if app.config["REGRESSION_BROKEN"]:
            abort(404)
        return html(pages.ABOUT)

    @app.get("/non-html")
    def non_html():
        return Response(pages.NON_HTML, content_type="application/pdf")

    @app.get("/ext-farm")
    def ext_farm():
        # Batch of links to one persistently failing external host. Not
        # linked from any other page, so existing crawls never see it.
        external = ext_base.rstrip("/")
        total = app.config["EXT_FARM_LINKS"]
        links = "\n".join(
            f'<a href="{external}/fail/{index}">failing link {index}</a>'
            for index in range(total)
        )
        return html(pages.EXT_FARM.format(links=links))

    @app.get("/private/secret")
    def private():
        counters["private"] += 1
        return html(pages.PRIVATE)

    @app.get("/search")
    def search():
        return html(pages.SEARCH)

    @app.get("/assets/style.css")
    def style():
        return Response("body{color:#111}", content_type="text/css")

    @app.get("/assets/app.js")
    def script():
        return Response("// fixture", content_type="application/javascript")

    @app.get("/assets/ok.png")
    def ok_image():
        return Response(b"\x89PNG\r\n\x1a\n", content_type="image/png")

    @app.get("/assets/missing.png")
    def missing_image():
        abort(404)

    @app.get("/robots.txt")
    def robots():
        body = "User-agent: *\nDisallow: /private/\nAllow: /private/public\n"
        return Response(body, content_type="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        root = request.url_root.rstrip("/")
        paths = ["/", "/about", "/anchor-page", "/orphan"]
        items = "".join(f"<url><loc>{root}{path}</loc></url>" for path in paths)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{items}</urlset>"
        )
        return Response(xml, content_type="application/xml")

    @app.errorhandler(404)
    def not_found(_error):
        return html(pages.NOT_FOUND, status=404)

    return app
