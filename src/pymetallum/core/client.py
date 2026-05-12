"""HTTP client for Metal Archives with Cloudflare bypass via curl_cffi."""

import json
import time
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from loguru import logger

BASE_URL = "https://www.metal-archives.com"
REQUEST_TIMEOUT = 15


class NotFoundError(Exception):
    """Raised when a Metal Archives page returns HTTP 404."""


class MetalArchivesClient:
    """HTTP client for Metal Archives, using curl_cffi to bypass Cloudflare.

    Uses ``curl_cffi`` with Chrome TLS fingerprint impersonation to avoid
    Cloudflare's bot detection. All requests go through a persistent session
    with automatic rate limiting.

    Rate limits:
        - Normal requests: 0.3s between calls (``rate_limit_seconds``).
        - Bulk/crawl requests: 3.0s between calls (``crawl_delay``), matching
          the Metal Archives ``robots.txt`` crawl-delay directive.
    """

    def __init__(self):
        self.base_url = BASE_URL
        self.rate_limit_seconds = 0.3
        self.crawl_delay = 3.0
        self._last_request_time = None
        self._session = curl_requests.Session(impersonate="chrome")
        logger.info("HTTP client initialized.")

    def get(
        self, url: str, params: dict | None = None, crawl: bool = False
    ) -> str | None:
        """Make a GET request and return the response body as text.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters (appended as ``?key=value``).
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Response body as a string, or None on any failure (timeout,
            HTTP error, network issue).
        """
        result = self._request(url, params, crawl)
        if result is None:
            return None
        return result.text

    def get_with_url(
        self, url: str, params: dict | None = None, crawl: bool = False
    ) -> tuple[str, str] | None:
        """Make a GET request and return both the response body and final URL.

        Useful for endpoints that redirect (e.g. /band/random).

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Tuple of (response_text, final_url), or None on failure.
        """
        result = self._request(url, params, crawl)
        if result is None:
            return None
        return result.text, str(result.url)

    def get_bytes(self, url: str, crawl: bool = False) -> bytes | None:
        """Make a GET request and return the response body as raw bytes.

        Used for binary content like band photos and album covers, which are
        displayed in the terminal via iTerm2/Kitty image protocols.

        Args:
            url: Full URL to fetch.
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Raw bytes of the response body, or None on failure.
        """
        result = self._request(url, crawl=crawl)
        if result is None:
            return None
        return result.content

    def _request(self, url: str, params: dict | None = None, crawl: bool = False):
        """Internal: execute a GET request and return the response object, or None."""
        self._enforce_rate_limit(crawl=crawl)
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                raise NotFoundError(f"404 Not Found: {url}")
            response.raise_for_status()
            return response
        except NotFoundError:
            raise
        except Exception as e:
            logger.error(f"Request failed for {url}: {e}")
            return None

    def get_json(
        self, url: str, params: dict | None = None, crawl: bool = False
    ) -> dict | None:
        """Make a GET request and parse the response as JSON.

        Metal Archives sometimes wraps JSON responses in HTML (inside a
        ``<pre>`` or ``<body>`` tag). The ``_extract_json`` helper handles
        unwrapping these automatically.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use the longer crawl delay between requests.

        Returns:
            Parsed JSON as a dict, or None if the request failed or the
            response could not be parsed as JSON.
        """
        text = self.get(url, params, crawl=crawl)
        if not text:
            return None
        try:
            return _extract_json(text)
        except Exception as e:
            logger.error(f"Failed to parse JSON from {url}: {e}")
            return None

    def download_image(
        self, url: str, output_dir: str = "./images/"
    ) -> str | None:
        """Download an image to a local file.

        The output path mirrors the remote path structure under ``output_dir``.
        For example, an image at ``.../images/1/2/3/photo.jpg`` is saved
        to ``output_dir/1/2/3/photo.jpg``. Parent directories are created
        automatically.

        Args:
            url: Full image URL.
            output_dir: Local directory to save images under.

        Returns:
            The path of the saved file as a string, or None if the download
            failed or ``url`` was empty.
        """
        if not url:
            return None

        try:
            clean_path = url.split("?")[0].replace(
                self.base_url + "/images/", ""
            )
            output_path = Path(output_dir) / clean_path
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
