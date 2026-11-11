"""robots.txt parsing, matching, and caching tests."""

from linkpatrol.robots import RobotsCache, parse_robots

def test_prefix_and_wildcard_patterns():
    policy = parse_robots("User-agent: *\nDisallow: /private/\n", "linkpatrol/0.1")
    assert not policy.allowed("/private/secret") and policy.allowed("/public")
    wildcard = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", "linkpatrol")
    assert not wildcard.allowed("/manual.pdf") and wildcard.allowed("/manual.pdf.html")

def test_longest_match_and_allow_tie_break():
    text = """User-agent: *
Disallow: /private/
Allow: /private/public/
Disallow: /tie
Allow: /tie
"""
    policy = parse_robots(text, "linkpatrol")
    assert not policy.allowed("/private/secret")
    assert policy.allowed("/private/public/page") and policy.allowed("/tie")

def test_user_agent_specificity():
    text = """User-agent: *
Disallow: /all

User-agent: linkpatrol
Disallow: /one

User-agent: linkpatrol-demo
Disallow: /two
"""
    specific = parse_robots(text, "linkpatrol-demo/0.1")
    assert specific.allowed("/all") and specific.allowed("/one")
    assert not specific.allowed("/two")
    assert not parse_robots(text, "linkpatrol/0.1").allowed("/one")

class FakeFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url, method="GET", max_body_bytes=None):
        self.calls.append(url)

        class Result:
            status_code = 200
            error = None
            body = b"User-agent: *\nDisallow: /secret\n"

        return Result()

def test_robots_is_cached_per_host_and_can_be_disabled():
    fetcher = FakeFetcher()
    cache = RobotsCache(fetcher, respect=True, user_agent="linkpatrol")
    assert not cache.policy_for("http://127.0.0.1:5001/a").allowed("/secret")
    assert not cache.policy_for("http://127.0.0.1:5001/b").allowed("/secret")
    assert fetcher.calls == ["http://127.0.0.1:5001/robots.txt"]
    off = RobotsCache(FakeFetcher(), respect=False, user_agent="linkpatrol")
    assert off.policy_for("http://127.0.0.1:5001/x").allowed("/anything")
