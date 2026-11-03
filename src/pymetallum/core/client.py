"""HTTP client for Metal Archives with Cloudflare bypass via curl_cffi."""

import contextlib
import json
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
# 10-60s backoff for a block that is already known to be in effect.
CHALLENGE_COOLDOWN = 120.0


class NotFoundError(Exception):
    """Raised when a Metal Archives page returns HTTP 404."""


class FetchError(Exception):
    """A request failed for a reason other than 404: retry later.

    Raised for network errors and timeouts, 5xx and other HTTP errors (after
    one retry), and bodies that should be JSON but are not. Unlike
    :class:`NotFoundError` it never means the page is gone.
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
    """

    def __init__(self):
        self.base_url = BASE_URL
        self.rate_limit_seconds = 1.0
        self.crawl_delay = 3.0
        self._last_request_time = None
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._session = curl_requests.Session(impersonate="chrome")
        logger.info("HTTP client initialized.")

    def get(self, url: str, params: dict | None = None, crawl: bool = False) -> str:
        """Make a GET request and return the response body as text.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters (appended as ``?key=value``).
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Response body as a string.

        Raises:
            NotFoundError: If the page returns HTTP 404. Callers that treat a
                missing page as an empty result should use the ``_get*``
                helpers on ``BaseAPI``, which swallow it.
            FetchError: If the request failed otherwise (``BlockedError`` when
                Metal Archives is refusing requests).
        """
        return self._request(url, params, crawl).text

    def get_with_url(self, url: str, params: dict | None = None, crawl: bool = False) -> tuple[str, str]:
        """Make a GET request and return both the response body and final URL.

        Useful for endpoints that redirect (e.g. /band/random).

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Tuple of (response_text, final_url).

        Raises:
            NotFoundError: If the page returns HTTP 404.
            FetchError: If the request failed otherwise.
        """
        result = self._request(url, params, crawl)
        return result.text, str(result.url)

    def get_bytes(self, url: str, crawl: bool = False) -> bytes:
        """Make a GET request and return the response body as raw bytes.

        Used for binary content like band photos and album covers, which are
        displayed in the terminal via iTerm2/Kitty image protocols.

        Args:
            url: Full URL to fetch.
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Raw bytes of the response body.

        Raises:
            NotFoundError: If the image returns HTTP 404.
            FetchError: If the request failed otherwise.
        """
        return self._request(url, crawl=crawl).content

    def _request(self, url: str, params: dict | None = None, crawl: bool = False):
        """Internal: execute a GET request and return the response object.

        Serialized with a lock so concurrent callers (e.g. the Discord bot's
        thread pool) cannot bypass the rate limiter or share the session
        mid-rebuild.

        On HTTP 403 (Cloudflare challenge), the session is rebuilt once with a
        randomized 10-60s backoff and the request is retried. A second 403 opens
        a ``CHALLENGE_COOLDOWN`` window during which further requests fail fast
        instead of each queueing behind another backoff. Transient 5xx responses
        and network errors are retried once after a short delay.

        Raises:
            NotFoundError: If the page returns HTTP 404.
            BlockedError: On a 403 the rebuild did not clear or a 429, and
                during the cooldown after either (no request is made then).
            FetchError: On any other failure that the retry did not fix.
        """
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        with self._lock:
            remaining = self._blocked_until - time.time()
            if remaining > 0:
                raise BlockedError(
                    f"Metal Archives is refusing requests, {remaining:.0f}s of cooldown left", url
                )

            self._enforce_rate_limit(crawl=crawl)
            for attempt in range(2):
                try:
                    response = self._session.get(url, timeout=REQUEST_TIMEOUT)
                except Exception as e:
                    if attempt == 0:
                        logger.warning(f"Request error for {url}: {e}, retrying in 3s")
                        time.sleep(3)
                        continue
                    raise FetchError(f"Request failed: {e}", url) from e

                status = response.status_code
                if status == 404:
                    raise NotFoundError(f"404 Not Found: {url}")
                if status == 403:
                    if attempt == 0:
                        self._rebuild_session()
                        continue
                    self._blocked_until = time.time() + CHALLENGE_COOLDOWN
                    logger.error(
                        f"403 for {url} after session rebuild, pausing requests for {CHALLENGE_COOLDOWN:.0f}s"
                    )
                    raise BlockedError("Metal Archives refused the request (403)", url)
                if status == 429:
                    # Rate limited: back off like a block, so callers that
                    # retry FetchError right away do not turn this into a 403.
                    wait = _retry_after(response) or CHALLENGE_COOLDOWN
                    self._blocked_until = time.time() + wait
                    logger.error(f"429 for {url}, pausing requests for {wait:.0f}s")
                    raise BlockedError("Metal Archives is rate limiting requests (429)", url)
                if status >= 500 and attempt == 0:
                    logger.warning(f"{status} for {url}, retrying in 3s")
                    time.sleep(3)
                    continue
                if status >= 400:
                    raise FetchError(f"HTTP {status}", url)
                return response
            raise AssertionError("unreachable: the loop returns or raises")

    def _rebuild_session(self) -> None:
        """Drop the current session and build a fresh one after a challenge.

        Sleeps a randomized 10-60s first so the new TLS handshake does not
        arrive immediately after the blocked one, then warms the session up
        against the site root. Callers must hold ``self._lock``.
        """
        backoff = random.uniform(10, 60)
        logger.warning(f"403 received, rebuilding session, sleeping {backoff:.1f}s")
        with contextlib.suppress(Exception):
            self._session.close()
        time.sleep(backoff)
        self._session = curl_requests.Session(impersonate="chrome")
        try:
            self._session.get(self.base_url, timeout=REQUEST_TIMEOUT)
        except Exception as warmup_err:
            logger.warning(f"Warmup after 403 failed: {warmup_err}")
        self._last_request_time = time.time()

    def get_json(self, url: str, params: dict | None = None, crawl: bool = False) -> dict | None:
        """Make a GET request and parse the response as JSON.

        Metal Archives sometimes wraps JSON responses in HTML (inside a
        ``<pre>`` or ``<body>`` tag). The ``_extract_json`` helper handles
        unwrapping these automatically.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Parsed JSON as a dict, or None for an empty body.

        Raises:
            NotFoundError: If the endpoint returns HTTP 404.
            FetchError: If the request failed, or the body is not JSON (an
                error page served with 200 reads as a failure, not as data).
        """
        text = self.get(url, params, crawl=crawl)
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
            crawl: If True, use the longer crawl_delay (3s, per robots.txt).
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


def _retry_after(response) -> float | None:
    """Seconds from a numeric Retry-After header, capped at the challenge cooldown."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return min(max(seconds, 1.0), CHALLENGE_COOLDOWN) if seconds > 0 else None


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
