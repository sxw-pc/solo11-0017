"""HTML fixtures for the local mock websites."""

HOME = """<!doctype html>
<html><head><title>Mock home</title>
<link rel="stylesheet" href="/assets/style.css">
<script src="/assets/app.js"></script>
</head><body>
<h1>Mock home</h1>
<nav>
<a href="/about">About</a>
<a href="/section/index">Section</a>
<a href="/depth/1">Depth one</a>
</nav>
<p>
<a href="/missing">Missing page</a>
<a href="/server-error">Server error</a>
<a href="/hop-1">Redirect chain</a>
<a href="/loop-a">Redirect loop</a>
<a href="/anchor-page#present">Present anchor</a>
<a href="/anchor-page#absent">Absent anchor</a>
<a href="/dead-end">Dead end</a>
<a href="/non-html">Non HTML</a>
<a href="/private/secret">Private</a>
<a href="/regression">Regression probe</a>
<a href="/slow">Slow page</a>
<a href="/unstable">Unstable page</a>
<a href="/search?q=test">Search</a>
<a href="mailto:owner@127.0.0.1">Mail</a>
<a href="tel:+10000000000">Telephone</a>
<a href="javascript:void(0)">Script only</a>
</p>
<p>
<img src="/assets/ok.png" alt="ok">
<img src="/assets/missing.png" alt="missing">
</p>
<form action="/search" method="get"><input name="q"></form>
<p class="external">
<a href="{ext}/ok">External ok</a>
<a href="{ext}/missing">External missing</a>
<a href="{ext}/head-only">External HEAD fallback</a>
</p>
</body></html>"""

ABOUT = """<!doctype html>
<html><head><title>About</title></head><body>
<h1>About</h1><p><a href="/">Home</a></p>
</body></html>"""

SECTION = """<!doctype html>
<html><head><title>Section</title></head><body>
<h1>Section</h1>
<a href="/section/a">A</a>
<a href="/section/b">B</a>
<a href="/about">About</a>
<a href="/">Home</a>
</body></html>"""

SECTION_END = """<!doctype html>
<html><head><title>Section page</title></head><body>
<h1>Section page</h1><a href="/">Home</a>
</body></html>"""

DEPTH = """<!doctype html>
<html><head><title>{title}</title></head><body>
<h1>{title}</h1><a href="{next_url}">Next</a><a href="/">Home</a>
</body></html>"""

ANCHOR_PAGE = """<!doctype html>
<html><head><title>Anchors</title></head><body>
<div id="present">Present</div>
<a name="legacy">Legacy</a>
<p><a href="/">Home</a></p>
</body></html>"""

DEAD_END = """<!doctype html>
<html><head><title>Dead end</title></head><body>
<h1>Dead end</h1><p>No links here.</p>
</body></html>"""

ORPHAN = """<!doctype html>
<html><head><title>Orphan</title></head><body>
<h1>Orphan</h1><p>Only the sitemap mentions this page.</p>
</body></html>"""

RECOVERED = """<!doctype html>
<html><head><title>Recovered</title></head><body>
<h1>Recovered</h1><a href="/">Home</a>
</body></html>"""

SLOW = """<!doctype html>
<html><head><title>Slow</title></head><body>
<h1>Slow</h1><a href="/">Home</a>
</body></html>"""

LANDING = """<!doctype html>
<html><head><title>Landing</title></head><body>
<h1>Landing</h1><a href="/">Home</a>
</body></html>"""

SEARCH = """<!doctype html>
<html><head><title>Search</title></head><body>
<h1>Search</h1><a href="/">Home</a>
</body></html>"""

NOT_FOUND = """<!doctype html>
<html><head><title>Missing</title></head><body>
<h1>Missing</h1><a href="/">Home</a>
</body></html>"""

SERVER_ERROR = """<!doctype html>
<html><head><title>Error</title></head><body>
<h1>Server error</h1><a href="/">Home</a>
</body></html>"""

PRIVATE = """<!doctype html>
<html><head><title>Private</title></head><body>
<h1>Private</h1><a href="/">Home</a>
</body></html>"""

NON_HTML = "This is a PDF-like fixture, not an HTML document.\n<a href='/missing'>not parsed</a>"

EXT_OK = """<!doctype html>
<html><head><title>External OK</title></head><body>
<h1>External OK</h1><p><a href="/missing">Local external missing</a></p>
</body></html>"""

EXT_HEAD_ONLY = """<!doctype html>
<html><head><title>External HEAD fallback</title></head><body>
<h1>HEAD fallback</h1><p>GET works; HEAD is rejected.</p>
</body></html>"""

EXT_NOT_FOUND = """<!doctype html>
<html><head><title>External missing</title></head><body>
<h1>External missing</h1>
</body></html>"""
