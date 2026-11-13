"""HTTP fetching with timeouts, bounded retries, and redirect auditing."""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .breaker import CircuitBreaker
from .urlnorm import host_key

REDIRECT_CODES = {301, 302, 303, 307, 308}

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTPError responses so every hop is visible."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

@dataclass
class FetchResult:
    """The outcome of one fetch including its complete redirect trail."""

    requested_url: str
    final_url: str
    status_code: int | None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0
    redirect_chain: list[dict] = field(default_factory=list)
    error: str | None = None
    method: str = "GET"

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value.split(";", 1)[0].strip().lower()
        return ""

    @property
    def is_ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300

    @property
    def is_html(self) -> bool:
        return self.content_type in {"text/html", "application/xhtml+xml"}

    @property
    def text(self) -> str:
        charset = "utf-8"
        for key, value in self.headers.items():
            if key.lower() != "content-type":
                continue
            for part in value.split(";")[1:]:
                if part.strip().lower().startswith("charset="):
                    charset = part.split("=", 1)[1].strip().strip('"') or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

class Fetcher:
    """Small policy layer around :mod:`urllib.request`."""

    def __init__(
        self,
        timeout_s: float = 5.0,
        retries: int = 2,
        user_agent: str = "linkpatrol/0.1",
        max_redirects: int = 5,
        max_body_bytes: int = 2 * 1024 * 1024,
        backoff_s: float = 0.02,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.user_agent = user_agent
        self.max_redirects = max(1, max_redirects)
        self.max_body_bytes = max_body_bytes
        self.backoff_s = backoff_s
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self.request_count = 0

    def fetch(
        self,
        url: str,
        method: str = "GET",
        max_body_bytes: int | None = None,
    ) -> FetchResult:
        """Fetch a URL and follow at most ``max_redirects`` redirects.

        Network failures and 5xx responses are retried with exponential
        backoff. 4xx responses are never retried. Redirect loops terminate
        with ``error == "redirect_loop"``.

        The settled outcome (after retries and redirect following) is
        reported to the circuit breaker exactly once, against the host that
        produced the final response. The breaker only observes here; it
        never alters fetch behaviour.
        """
        limit = self.max_body_bytes if max_body_bytes is None else max_body_bytes
        result = self._fetch_followed(url, method, limit)
        self.breaker.record_result(
            host_key(result.final_url or url), result.status_code, result.error
        )
        return result

    def _fetch_followed(self, url: str, method: str, limit: int) -> FetchResult:
        started = time.perf_counter()
        current = url
        seen = {current}
        chain: list[dict] = []
        while True:
            result = self._request_with_retries(current, method, limit)
            if result.error:
                result.requested_url = url
                result.redirect_chain = chain
                result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return result
            location = _header(result.headers, "location")
            if result.status_code not in REDIRECT_CODES or not location:
                result.requested_url = url
                result.final_url = current
                result.redirect_chain = chain
                result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return result
            next_url = urljoin(current, location)
            chain.append({"url": current, "status": result.status_code, "location": next_url})
            if next_url in seen:
                result.requested_url = url
                result.final_url = current
                result.redirect_chain = chain
                result.error = "redirect_loop"
                result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return result
            if len(chain) > self.max_redirects:
                result.requested_url = url
                result.final_url = current
                result.redirect_chain = chain
                result.error = "too_many_redirects"
                result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return result
            seen.add(next_url)
            current = next_url

    def _request_with_retries(self, url: str, method: str, limit: int) -> FetchResult:
        last: FetchResult | None = None
        for attempt in range(self.retries + 1):
            last = self._request_once(url, method, limit)
            retryable = last.error is not None or (
                last.status_code is not None and last.status_code >= 500
            )
            if not retryable or attempt >= self.retries:
                return last
            delay = self.backoff_s * (2**attempt)
            if delay:
                time.sleep(delay)
        return last  # pragma: no cover - loop always returns

    def _request_once(self, url: str, method: str, limit: int) -> FetchResult:
        started = time.perf_counter()
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect())
        self.request_count += 1
        status: int | None = None
        headers: dict[str, str] = {}
        body = b""
        error: str | None = None
        try:
            with opener.open(request, timeout=self.timeout_s) as response:
                status = response.status
                headers = dict(response.headers.items())
                if method != "HEAD":
                    body = response.read(limit)
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
            if method != "HEAD":
                try:
                    body = exc.read(limit)
                except Exception:
                    body = b""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            error = f"{type(exc).__name__}: {reason}"
        except http.client.HTTPException as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(
            requested_url=url,
            final_url=url,
            status_code=status,
            headers=headers,
            body=body,
            elapsed_ms=elapsed,
            error=error,
            method=method,
        )

def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""
