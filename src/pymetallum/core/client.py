"""HTTP client for Metal Archives with Cloudflare bypass via curl_cffi."""

import contextlib
import json
import math
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from loguru import logger

BASE_URL = "https://www.metal-archives.com"
REQUEST_TIMEOUT = 15

# How long to stop issuing requests after a session rebuild fails to clear a
# Cloudflare challenge. Without this, every queued caller serially pays another
# session-rebuild backoff for a block that is already known to be in effect.
CHALLENGE_COOLDOWN = 120.0


class NotFoundError(Exception):
    """Raised when a Metal Archives page returns HTTP 404."""


class FetchError(Exception):
    """A request failed for a reason other than 404: retry later.

    Raised for network errors and timeouts, 5xx and other HTTP errors that
    the client's retries did not fix, and bodies that should be JSON but are
    not. Unlike :class:`NotFoundError` it never means the page is gone.
    """

    def __init__(self, message: str, url: str | None = None):
        super().__init__(message)
        self.url = url


class BlockedError(FetchError):
    """Metal Archives is refusing requests: back off before retrying.

    Raised for a 403 (Cloudflare) that a session rebuild did not clear and for
    a 429 (rate limited), and, without touching the network, for every request
    during the cooldown that follows either.
    """


class MetalArchivesClient:
    """HTTP client for Metal Archives, using curl_cffi to bypass Cloudflare.

    Uses ``curl_cffi`` with Chrome TLS fingerprint impersonation to avoid
    Cloudflare's bot detection. All requests go through a persistent session
    with automatic rate limiting.

    Rate limits:
        - Normal requests: 1.0s between calls (``rate_limit_seconds``).
        - Bulk/crawl requests: 3.0s between calls (``crawl_delay``), matching
          the Metal Archives ``robots.txt`` crawl-delay directive.

    The interactive default is deliberately below the advertised crawl-delay,
    since those requests are driven one at a time by a human rather than by a
    crawler, but high enough not to look like a scraper. Anything that fans out
    over many pages passes ``crawl=True`` and gets the full 3s.

    All arguments are keyword-only.

    Args:
        timeout: Seconds to wait for each attempt, at least 1 ms and finite.
            With retries, one call can take about ``max_attempts`` times as
            long. Raise it if a listing times out. The request methods also
            take a per-call ``timeout`` that overrides it.
        rate_limit_seconds: Minimum delay between normal requests.
        crawl_delay: Minimum delay between ``crawl=True`` requests.
        max_attempts: How many times one call may send its request, counting
            the first (at least 1). Network errors and 5xx are retried until
            it runs out. A 403 spends one attempt on a session rebuild, so with
            1 a 403 blocks straight away.
        retry_delay: Seconds to wait before a retry. A retry also respects the
            rate limit, so the wait is never shorter than that delay.
        rebuild_backoff: ``(min, max)`` range of the random sleep before a
            session rebuild.
        challenge_cooldown: Seconds to refuse requests after a block (a 403
            the rebuild did not clear, or a 429). A longer ``Retry-After`` is
            capped to it.
        impersonate: Browser whose TLS fingerprint ``curl_cffi`` imitates.
    """

    def __init__(
        self,
        *,
        timeout: float = REQUEST_TIMEOUT,
        rate_limit_seconds: float = 1.0,
        crawl_delay: float = 3.0,
        max_attempts: int = 2,
        retry_delay: float = 3.0,
        rebuild_backoff: tuple[float, float] = (10.0, 60.0),
        challenge_cooldown: float = CHALLENGE_COOLDOWN,
        impersonate: str = "chrome",
    ):
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError(f"max_attempts must be a whole number of at least 1, got {max_attempts!r}")
        low, high = rebuild_backoff
        for name, value in (
            ("rate_limit_seconds", rate_limit_seconds),
            ("crawl_delay", crawl_delay),
            ("retry_delay", retry_delay),
            ("rebuild_backoff", low),
            ("rebuild_backoff", high),
            ("challenge_cooldown", challenge_cooldown),
        ):
            _check_delay(name, value)
        if low > high:
            raise ValueError(f"rebuild_backoff must be (min, max) with min <= max, got {rebuild_backoff!r}")
        _check_timeout(timeout)
        self.base_url = BASE_URL
        self.timeout = timeout
        self.rate_limit_seconds = rate_limit_seconds
        self.crawl_delay = crawl_delay
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.rebuild_backoff = rebuild_backoff
        self.challenge_cooldown = challenge_cooldown
        self.impersonate = impersonate
        self._last_request_time = None
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._session = curl_requests.Session(impersonate=impersonate)
        logger.info("HTTP client initialized.")

    def get(
        self, url: str, params: dict | None = None, crawl: bool = False, timeout: float | None = None
    ) -> str:
        """Make a GET request and return the response body as text.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters (appended as ``?key=value``).
            crawl: If True, use the longer crawl delay between requests.
            timeout: Seconds to wait for each attempt (default: ``self.timeout``).

        Returns:
            Response body as a string.

        Raises:
            NotFoundError: If the page returns HTTP 404. Callers that treat a
                missing page as an empty result should use the ``_get*``
                helpers on ``BaseAPI``, which swallow it.
            FetchError: If the request failed otherwise (``BlockedError`` when
                Metal Archives is refusing requests).
        """
        return self._request(url, params, crawl, timeout).text

    def get_with_url(
        self, url: str, params: dict | None = None, crawl: bool = False, timeout: float | None = None
    ) -> tuple[str, str]:
        """Make a GET request and return both the response body and final URL.

        Useful for endpoints that redirect (e.g. /band/random).

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use the longer crawl delay between requests.
            timeout: Seconds to wait for each attempt (default: ``self.timeout``).

        Returns:
            Tuple of (response_text, final_url).

        Raises:
            NotFoundError: If the page returns HTTP 404.
            FetchError: If the request failed otherwise.
        """
        result = self._request(url, params, crawl, timeout)
        return result.text, str(result.url)

    def get_bytes(self, url: str, crawl: bool = False, timeout: float | None = None) -> bytes:
        """Make a GET request and return the response body as raw bytes.

        Used for binary content like band photos and album covers, which are
        displayed in the terminal via iTerm2/Kitty image protocols.

        Args:
            url: Full URL to fetch.
            crawl: If True, use the longer crawl delay between requests.
            timeout: Seconds to wait for each attempt (default: ``self.timeout``).

        Returns:
            Raw bytes of the response body.

        Raises:
            NotFoundError: If the image returns HTTP 404.
            FetchError: If the request failed otherwise.
        """
        return self._request(url, crawl=crawl, timeout=timeout).content

    def _request(
        self, url: str, params: dict | None = None, crawl: bool = False, timeout: float | None = None
    ):
        """Internal: execute a GET request and return the response object.

        Serialized with a lock so concurrent callers (e.g. the Discord bot's
        thread pool) cannot bypass the rate limiter or share the session
        mid-rebuild.

        On HTTP 403 (Cloudflare challenge), the session is rebuilt once after a
        randomized ``rebuild_backoff`` sleep and the request is retried. A
        second 403 opens a ``challenge_cooldown`` window during which further
        requests fail fast instead of each queueing behind another backoff.
        Transient 5xx responses and network errors are retried after
        ``retry_delay``, up to ``max_attempts`` requests in total. Every
        attempt, retries included, goes through the rate limiter.

        Raises:
            NotFoundError: If the page returns HTTP 404.
            BlockedError: On a 403 the rebuild did not clear or a 429, and
                during the cooldown after either (no request is made then).
            FetchError: On any other failure that the retry did not fix.
        """
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        timeout = self.timeout if timeout is None else _check_timeout(timeout)
        with self._lock:
            remaining = self._blocked_until - time.time()
            if remaining > 0:
                raise BlockedError(
                    f"Metal Archives is refusing requests, {remaining:.0f}s of cooldown left", url
                )

            rebuilt = False
            for attempt in range(self.max_attempts):
                retry = attempt < self.max_attempts - 1
                # Every attempt counts as a request, retries included, so the
                # next call's delay is measured from the last one sent.
                self._enforce_rate_limit(crawl=crawl)
                try:
                    response = self._session.get(url, timeout=timeout)
                except Exception as e:
                    if retry:
                        logger.warning(f"Request error for {url}: {e}, retrying in {self.retry_delay:g}s")
                        time.sleep(self.retry_delay)
                        continue
                    raise FetchError(f"Request failed: {e}", url) from e

                status = response.status_code
                if status == 404:
                    raise NotFoundError(f"404 Not Found: {url}")
                if status == 403:
                    if retry and not rebuilt:
                        self._rebuild_session(timeout)
                        rebuilt = True
                        continue
                    cooldown = self.challenge_cooldown
                    self._blocked_until = time.time() + cooldown
                    logger.error(f"403 for {url}, pausing requests for {cooldown:.0f}s")
                    raise BlockedError("Metal Archives refused the request (403)", url)
                if status == 429:
                    # Rate limited: back off like a block, so callers that
                    # retry FetchError right away do not turn this into a 403.
                    wait = _retry_after(response, self.challenge_cooldown) or self.challenge_cooldown
                    self._blocked_until = time.time() + wait
                    logger.error(f"429 for {url}, pausing requests for {wait:.0f}s")
                    raise BlockedError("Metal Archives is rate limiting requests (429)", url)
                if status >= 500 and retry:
                    logger.warning(f"{status} for {url}, retrying in {self.retry_delay:g}s")
                    time.sleep(self.retry_delay)
                    continue
                if status >= 400:
                    raise FetchError(f"HTTP {status}", url)
                return response
            raise AssertionError("unreachable: the loop returns or raises")

    def _rebuild_session(self, timeout: float) -> None:
        """Drop the current session and build a fresh one after a challenge.

        Sleeps a random ``rebuild_backoff`` first so the new TLS handshake does
        not arrive right after the blocked one, then warms the session up
        against the site root, waiting at most ``timeout``. Callers must hold
        ``self._lock``.
        """
        backoff = random.uniform(*self.rebuild_backoff)
        logger.warning(f"403 received, rebuilding session, sleeping {backoff:.1f}s")
        with contextlib.suppress(Exception):
            self._session.close()
        time.sleep(backoff)
        self._session = curl_requests.Session(impersonate=self.impersonate)
        try:
            self._session.get(self.base_url, timeout=timeout)
        except Exception as warmup_err:
            logger.warning(f"Warmup after 403 failed: {warmup_err}")
        self._last_request_time = time.time()

    def get_json(
        self, url: str, params: dict | None = None, crawl: bool = False, timeout: float | None = None
    ) -> dict | None:
        """Make a GET request and parse the response as JSON.

        Metal Archives sometimes wraps JSON responses in HTML (inside a
        ``<pre>`` or ``<body>`` tag). The ``_extract_json`` helper handles
        unwrapping these automatically.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use the longer crawl delay between requests.
            timeout: Seconds to wait for each attempt (default: ``self.timeout``).

        Returns:
            Parsed JSON as a dict, or None for an empty body.

        Raises:
            NotFoundError: If the endpoint returns HTTP 404.
            FetchError: If the request failed, or the body is not JSON (an
                error page served with 200 reads as a failure, not as data).
        """
        text = self.get(url, params, crawl=crawl, timeout=timeout)
        if not text:
            return None
        try:
            return _extract_json(text)
        except Exception as e:
            raise FetchError(f"Response is not JSON: {e}", url) from e

    def download_image(self, url: str, output_dir: str = "./images/") -> str | None:
        """Download an image to a local file.

        The output path mirrors the remote path structure under ``output_dir``.
        For example, an image at ``.../images/1/2/3/photo.jpg`` is saved
        to ``output_dir/1/2/3/photo.jpg``. Parent directories are created
        automatically.

        Since the path segment comes from a remote URL, the resolved
        destination is checked to be inside ``output_dir``; anything that
        escapes it (via ``..`` or an absolute segment) is refused.

        Args:
            url: Full image URL.
            output_dir: Local directory to save images under.

        Returns:
            The path of the saved file as a string, or None if the download
            failed, ``url`` was empty, or the path escaped ``output_dir``.
        """
        if not url:
            return None

        try:
            clean_path = url.split("?")[0].replace(self.base_url + "/images/", "")
            root = Path(output_dir).resolve()
            output_path = (root / clean_path).resolve()
            if output_path == root or not output_path.is_relative_to(root):
                logger.warning(f"Refusing to write outside {root}: {url}")
                return None
            output_path.parent.mkdir(parents=True, exist_ok=True)

            image_data = self.get_bytes(url)
            if not image_data:
                return None

            output_path.write_bytes(image_data)
            logger.debug(f"Downloaded {Path(clean_path).name} -> {output_path}")
            return str(output_path)

        except Exception as e:
            logger.debug(f"Failed to download {url}: {e}")
            return None

    def _enforce_rate_limit(self, crawl: bool = False):
        """Enforce delay between requests.

        Args:
            crawl: If True, use the longer ``crawl_delay`` (3s by default, per robots.txt).
                   Used for bulk/listing operations.
        """
        delay = self.crawl_delay if crawl else self.rate_limit_seconds
        if self._last_request_time is not None:
            elapsed = time.time() - self._last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def close(self):
        """Close the underlying HTTP session and release resources."""
        self._session.close()
        logger.info("HTTP client closed.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _check_timeout(timeout: float) -> float:
    """Reject timeouts that curl_cffi cannot pass on to libcurl.

    curl_cffi converts the timeout to whole milliseconds. libcurl treats 0 as
    no timeout, so anything under 1 ms could hang on a stalled connection while
    holding the client lock, and infinity cannot be converted at all.
    """
    if not (math.isfinite(timeout) and timeout >= 0.001):
        raise ValueError(f"timeout must be finite and at least 0.001s, got {timeout!r}")
    return timeout


def _check_delay(name: str, value: float) -> None:
    """Reject delays that ``time.sleep`` would refuse or that disable a wait."""
    if not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} must be finite and not negative, got {value!r}")


def _retry_after(response, cap: float) -> float | None:
    """Seconds from a numeric Retry-After header, capped at ``cap``."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return min(max(seconds, 1.0), cap) if seconds > 0 else None


def _extract_json(text: str) -> dict:
    """Extract JSON data from a response that may be plain JSON or HTML-wrapped.

    Metal Archives AJAX endpoints sometimes return raw JSON, and sometimes
    wrap it in HTML tags (typically ``<pre>`` or inside ``<body>``). This
    function tries each extraction strategy in order:

    1. Direct parse: if the text starts with ``{`` or ``[``.
    2. ``<pre>`` tag: extract text content from the first ``<pre>`` element.
    3. ``<body>`` tag: extract text content from the ``<body>`` element.
    4. Fallback: try parsing the raw text as-is.

    Raises:
        json.JSONDecodeError: If none of the strategies produce valid JSON.
    """
    text = text.strip()

    if text.startswith("{") or text.startswith("["):
        return json.loads(text)

    soup = BeautifulSoup(text, "html.parser")

    pre_tag = soup.find("pre")
    if pre_tag:
        return json.loads(pre_tag.get_text())

    body_tag = soup.find("body")
    if body_tag:
        body_text = body_tag.get_text().strip()
        if body_text:
            return json.loads(body_text)

    return json.loads(text)
