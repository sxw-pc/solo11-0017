"""Self-contained demonstration website with healthy and unhealthy links."""

from __future__ import annotations

import time
from collections import defaultdict
from html import escape

from flask import Flask, Response, abort, request

STYLE = """
body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1f2933}
a{color:#0b63ce}nav a{margin-right:1rem}.box{border:1px solid #d6dde5;padding:1rem;border-radius:8px}
footer{margin-top:2rem;color:#66788a;font-size:.9rem}
"""

def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="/assets/style.css">
<script src="/assets/app.js" defer></script>
</head><body><div id="top">
<nav><a href="/">Home</a><a href="/about">About</a><a href="/docs">Docs</a>
<a href="/blog/post-1">Blog</a></nav>
<h1>{title}</h1>{body}
<footer>linkpatrol local demonstration site</footer>
</div></body></html>"""

def create_demo_app() -> Flask:
    app = Flask(__name__)
    counters: dict[str, int] = defaultdict(int)
    app.config["DEMO_COUNTERS"] = counters
    # Scan-phase state used to demonstrate a broken-then-fixed history.
    app.config["SCAN_COUNT"] = 0
    app.config["REGRESSION_MODE"] = "auto"

    @app.get("/")
    def home():
        ua = request.headers.get("User-Agent", "").lower()
        if not ua.startswith("mozilla"):
            # Every crawler/scanner fetches the seed exactly once per run,
            # so consecutive scans can demonstrate a broken-then-fixed
            # history: the probe is broken for the first two runs.
            app.config["SCAN_COUNT"] += 1
        body = """
<div class="box">
<p>This local site intentionally contains several classes of link defects.</p>
<p><a href="/about">About</a> · <a href="/docs">Documentation</a> ·
<a href="/blog/post-1">A blog post</a> · <a href="/dead-end">Dead end</a></p>
<p><a href="/missing">A 404 link</a> · <a href="/server-error">A 500 link</a> ·
<a href="/hop-1">A three-hop redirect</a> · <a href="/loop-a">A redirect loop</a></p>
<p><a href="/flaky">An initially unstable page</a> · <a href="/slow">A slow page</a> ·
<a href="/regression">A regression probe</a> ·
<a href="/private/secret">A robots-disallowed page</a></p>
<p><img src="/assets/missing.png" alt="missing"> <img src="/assets/logo.png" alt="logo"></p>
<form action="/search" method="get"><input name="q"><button>Search</button></form>
<p><a href="mailto:maintainer@127.0.0.1">Email</a> ·
<a href="tel:+10000000000">Phone</a> · <a href="javascript:void(0)">Script</a></p>
</div>"""
        return _page("Demo home", body)

    def regression_state() -> bool:
        mode = app.config["REGRESSION_MODE"]
        if mode == "broken":
            return True
        if mode == "ok":
            return False
        return app.config["SCAN_COUNT"] <= 2

    @app.get("/regression")
    def regression():
        if regression_state():
            abort(404)
        return _page("Regression probe", '<p>This page recovered. <a href="/">Home</a>.</p>')

    @app.get("/__demo/state")
    def demo_state():
        return Response(
            f'{{"scan_count": {app.config["SCAN_COUNT"]}, '
            f'"regression_mode": "{app.config["REGRESSION_MODE"]}", '
            f'"regression_broken": {str(regression_state()).lower()}}}',
            content_type="application/json",
        )

    @app.get("/__demo/regression/<mode>")
    def demo_regression_mode(mode: str):
        if mode not in {"auto", "broken", "ok"}:
            abort(400)
        app.config["REGRESSION_MODE"] = mode
        return demo_state()

    @app.get("/__demo/reset")
    def demo_reset():
        app.config["SCAN_COUNT"] = 0
        app.config["REGRESSION_MODE"] = "auto"
        counters.clear()
        return demo_state()

    @app.get("/about")
    def about():
        body = """
<h2 id="team">About the team</h2>
<p><a href="/docs#install">Install section</a> ·
<a href="/docs#not-a-real-anchor">Missing anchor</a> ·
<a href="#top">Back to top</a></p>"""
        return _page("About", body)

    @app.get("/docs")
    def docs():
        body = """
<h2 id="install">Install</h2><p>Everything runs locally.</p>
<h2 id="usage">Usage</h2><p><a href="/about">Team</a> · <a href="/">Home</a></p>"""
        return _page("Documentation", body)

    @app.get("/blog/post-1")
    def blog_post():
        return _page("Blog post", '<p>A healthy article. <a href="/">Back home</a>.</p>')

    @app.get("/dead-end")
    def dead_end():
        return (
            "<!doctype html><html><head><title>Dead end</title></head><body>"
            "<h1>Dead end</h1><p>This page deliberately has no outgoing links at all.</p>"
            "</body></html>"
        )

    @app.get("/orphan")
    def orphan():
        return _page("Orphan", "<p>Only the sitemap knows this page exists.</p>")

    @app.get("/landing")
    def landing():
        return _page("Landing", '<p>Redirect chain destination. <a href="/">Home</a>.</p>')

    @app.get("/hop-1")
    def hop1():
        return Response("", 302, {"Location": "/hop-2"})

    @app.get("/hop-2")
    def hop2():
        return Response("", 301, {"Location": "/hop-3"})

    @app.get("/hop-3")
    def hop3():
        return Response("", 307, {"Location": "/landing"})

    @app.get("/old")
    def old():
        return Response("", 301, {"Location": "/about"})

    @app.get("/loop-a")
    def loop_a():
        return Response("", 302, {"Location": "/loop-b"})

    @app.get("/loop-b")
    def loop_b():
        return Response("", 302, {"Location": "/loop-a"})

    @app.get("/flaky")
    def flaky():
        counters["flaky"] += 1
        if counters["flaky"] <= 2:
            return _page("Temporary error", "<p>Try again shortly.</p>"), 500
        return _page("Recovered", '<p>The retry succeeded. <a href="/">Home</a>.</p>')

    @app.get("/slow")
    def slow():
        time.sleep(0.12)
        return _page("Slow", '<p>This response is intentionally slow. <a href="/">Home</a>.</p>')

    @app.get("/private/secret")
    def private():
        return _page("Private", '<p>robots.txt disallows this page. <a href="/">Home</a>.</p>')

    @app.get("/search")
    def search():
        query = escape(request.args.get("q", ""))
        return _page("Search", f'<p>Query: {query}. <a href="/">Home</a>.</p>')

    @app.get("/missing")
    def missing():
        abort(404)

    @app.get("/server-error")
    def server_error():
        return _page("Server error", "<p>This is an intentional 500 response.</p>"), 500

    @app.get("/assets/style.css")
    def style_css():
        return Response(STYLE, content_type="text/css")

    @app.get("/assets/app.js")
    def app_js():
        return Response("/* demo asset */\n", content_type="application/javascript")

    @app.get("/assets/logo.png")
    def logo():
        return Response(b"\x89PNG\r\n\x1a\n", content_type="image/png")

    @app.get("/robots.txt")
    def robots_txt():
        return Response(
            "User-agent: *\nDisallow: /private/\nAllow: /\n",
            content_type="text/plain",
        )

    @app.get("/sitemap.xml")
    def sitemap_xml():
        root = request.url_root.rstrip("/")
        urls = ["/", "/about", "/docs", "/orphan"]
        items = "".join(
            f"<url><loc>{root}{path}</loc><lastmod>2026-01-01</lastmod></url>"
            for path in urls
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{items}</urlset>"
        )
        return Response(xml, content_type="application/xml")

    @app.errorhandler(404)
    def not_found(_error):
        return _page("Not found", '<p>Page missing. <a href="/">Home</a>.</p>'), 404

    return app
