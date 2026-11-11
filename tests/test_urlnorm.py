"""URL normalisation and scope tests."""

import pytest

from linkpatrol.urlnorm import (
    get_fragment, host_key, hostname_of, in_path_scope, is_http_url, is_internal,
    normalize_url, origin, same_origin, strip_fragment, with_fragment,
)

def test_normalisation_case_ports_paths_and_queries():
    assert normalize_url("HTTP://Example.COM/Path") == "http://example.com/Path"
    assert normalize_url("http://Example.COM:80/a") == "http://example.com/a"
    assert normalize_url("https://Example.COM:443/a") == "https://example.com/a"
    assert normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"
    assert normalize_url("http://example.com/a/../b/") == "http://example.com/b"
    assert normalize_url("http://example.com/") == "http://example.com/"
    assert normalize_url("http://example.com") == "http://example.com/"

def test_fragments_and_idempotence():
    assert normalize_url("http://example.com/a#part") == "http://example.com/a"
    assert normalize_url("http://example.com/a#part", keep_fragment=True) == (
        "http://example.com/a#part")
    first = normalize_url("http://example.com/a?b=2&a=1&a=0")
    assert first == "http://example.com/a?a=0&a=1&b=2"
    assert normalize_url(first) == first

def test_drop_params_and_relative_resolution():
    url = "http://example.com/a?utm_source=x&utm_medium=y&fbclid=z&keep=1"
    assert normalize_url(url, drop_params=["utm_*", "fbclid"]) == "http://example.com/a?keep=1"
    assert normalize_url("../b/", base="http://example.com/a/c/") == "http://example.com/a/b"

def test_fragment_and_origin_helpers():
    url = "http://example.com/a#section"
    assert strip_fragment(url) == "http://example.com/a"
    assert get_fragment(url) == "section"
    assert with_fragment("http://example.com/a", "x") == "http://example.com/a#x"
    assert origin("HTTP://Example.com:80/x") == "http://example.com"
    assert host_key("http://127.0.0.1:5002/") == "127.0.0.1:5002"

def test_internal_scope_respects_scheme_and_port():
    seed = "http://127.0.0.1:5001/"
    assert is_internal("http://127.0.0.1:5001/about", seed)
    assert not is_internal("http://127.0.0.1:5002/about", seed)
    assert not is_internal("https://127.0.0.1:5001/about", seed)

def test_subdomain_scope_is_opt_in():
    seed = "https://example.com/"
    assert not is_internal("https://blog.example.com/post", seed)
    assert is_internal("https://blog.example.com/post", seed, allow_subdomains=True)
    assert not is_internal("https://example.org/", seed, allow_subdomains=True)

def test_path_scope_and_credentials():
    assert in_path_scope("http://x/a/b", ["^/a/"], [])
    assert not in_path_scope("http://x/b", ["^/a/"], [])
    assert not in_path_scope("http://x/private/x", [], ["^/private/"])
    assert in_path_scope("http://x/public/x", [], ["^/private/"])
    with pytest.raises(ValueError):
        normalize_url("http://user:secret@example.com/")
    with pytest.raises(ValueError):
        normalize_url("http://example.com:bad/")


def test_protocol_and_host_helpers():
    assert not is_http_url("mailto:test@example.com")
    assert not is_http_url("http://[::1")
    assert normalize_url("mailto:test@example.com") == "mailto:test@example.com"
    assert hostname_of("http://Example.COM:8080/x") == "example.com"
    assert same_origin("http://Example.COM:80/a", "http://example.com/b")
    assert not same_origin("http://example.com/a", "https://example.com/a")
    assert not is_internal("mailto:test@example.com", "http://example.com/")
    assert normalize_url("http://example.com/a/./b/") == "http://example.com/a/b"
    assert normalize_url("http://example.com/.") == "http://example.com/"
    assert origin("/relative") == "://"
