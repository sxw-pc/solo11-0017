"""Link-dense mock site: many hub pages all linking to the same targets.

Used to prove that the link graph deduplicates ``(target_url, kind)`` edges,
that the checker probes each unique target once, and that baseline diffs keep
every source page when a heavily linked target breaks.
"""

from __future__ import annotations

from flask import Flask, Response, abort

HUBS = 8
SHARED = 12

def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{body}</body></html>"
    )

def create_dense_app(ext_base: str = "http://127.0.0.1:9",
                     hubs: int = HUBS, shared: int = SHARED) -> Flask:
    app = Flask(__name__)
    app.config["HUBS"] = hubs
    app.config["SHARED"] = shared
    # Paths in this set flip from 200 to 404 to simulate regressions.
    app.config["BROKEN_SHARED"] = set()
    ext = ext_base.rstrip("/")

    @app.get("/")
    def home():
        links = "".join(f'<a href="/hub/{i}">Hub {i}</a>' for i in range(hubs))
        return _page("Dense home", f"<nav>{links}</nav>")

    @app.get("/hub/<int:n>")
    def hub(n: int):
        if n >= hubs:
            abort(404)
        body = "".join(
            f'<a href="/shared/{i}">Shared {i}</a>' for i in range(shared)
        )
        body += '<a href="/missing-shared">Broken shared target</a>'
        body += f'<a href="{ext}/ok">External ok</a>'
        body += '<img src="/assets/pixel.png" alt="pixel">'
        return _page(f"Hub {n}", body)

    @app.get("/shared/<int:n>")
    def shared_page(n: int):
        if n >= shared or f"/shared/{n}" in app.config["BROKEN_SHARED"]:
            abort(404)
        return _page(f"Shared {n}", '<p><a href="/">Home</a></p>')

    @app.get("/assets/pixel.png")
    def pixel():
        return Response(b"\x89PNG\r\n\x1a\n", content_type="image/png")

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nAllow: /\n", content_type="text/plain")

    @app.errorhandler(404)
    def not_found(_error):
        return _page("Missing", "<p>gone</p>"), 404

    return app
