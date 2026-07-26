"""API layer for Metal Archives.

Each entity type (band, album, artist, label, song) has a dedicated API class
that handles searching, fetching detail pages, and parsing AJAX endpoints.
All data is returned as plain dicts with a ``_type`` discriminator key.
"""

import inspect
from collections.abc import Callable
from datetime import date
from functools import cache
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from loguru import logger

from .client import BASE_URL, MetalArchivesClient, NotFoundError
from .countries import CODE_TO_COUNTRY
from .parsers import (
    AlbumPageParser,
    ArtistPageParser,
    BandPageParser,
    LabelPageParser,
    SongPageParser,
)
from .utils import (
    extract_date,
    extract_id_from_url,
    extract_text_block,
    normalize_text,
    parse_link,
    parse_rating,
)

# Bodies the AJAX fragment endpoints return, with HTTP 200, in place of content.
# Without filtering they surface as the lyrics or description themselves.
_LYRICS_PLACEHOLDERS = ("(loading", "(lyrics not available", "error")
_DESCRIPTION_PLACEHOLDERS = ("invalid band id",)

# Maps user-facing status labels to Metal Archives API status codes
STATUS_MAP = {
    "active": "1",
    "on hold": "2",
    "split-up": "3",
    "split up": "3",
    "unknown": "4",
    "changed name": "5",
    "changed": "5",
    "disputed": "6",
}


def _html_text(s: str) -> str:
    """Extract and normalize text from an HTML fragment (strips tags and entities)."""
    return normalize_text(BeautifulSoup(s, "html.parser").get_text())


@cache
def _parser_kwargs(parser_class: Callable) -> frozenset[str]:
    return frozenset(inspect.signature(parser_class.__init__).parameters) - {"self"}


class BaseAPI:
    """Base class providing shared fetch/search/parse helpers for all entity APIs.

    All entity-specific API classes (BandAPI, AlbumAPI, etc.) inherit from this
    and rely on its HTTP helpers, generic search logic, and utility methods like
    image downloading and lyrics fetching.

    Subclasses that support advanced search should define:
    - ``_ADVANCED_ENDPOINT``: the search URL path
    - ``_ADVANCED_DEFAULTS``: default parameters dict
    - ``_ADVANCED_ROW_PARSER``: name of the method to parse result rows,
      called as ``parser(row, columns)``
    - ``_advanced_layout()``: the column layout for a given set of filters

    Args:
        client: A configured ``MetalArchivesClient`` instance that handles
            rate limiting, Cloudflare bypass, and session management.
    """

    _ADVANCED_ENDPOINT: str | None = None
    _ADVANCED_DEFAULTS: dict[str, Any] | None = None
    _ADVANCED_ROW_PARSER: str | None = None

    # Subclasses with recent/archive listings set these:
    _ARCHIVE_ENDPOINT: str | None = None  # e.g. "/archives/ajax-band-list"
    _ARCHIVE_ROW_PARSER: str | None = None  # method name for parsing archive rows
    _ARCHIVE_BLOCK = 200  # rows the archive endpoint returns per request

    def __init__(self, client: MetalArchivesClient):
        self._client = client
        self._base_url = client.base_url

    # ─── HTTP helpers ────────────────────────────────────────────────────────
    #
    # Used for the supporting requests behind an entity: discography, images,
    # roster tables, lyrics, listings and searches. A 404 there means the extra
    # data does not exist, which is not a reason to fail the whole fetch.
    #
    # The entity page itself deliberately does NOT go through these. Its 404 is
    # the only way a caller can tell "this entry was deleted" from "this fetch
    # failed", and consumers rely on that: the scraping pipeline marks an entry
    # deleted on NotFoundError and retries it on any other failure. Swallowing
    # it there would silently turn deletions into permanent retries.
    # Frontends that want the softer behaviour wrap the API instead; see
    # MissingTolerantAPI in app/common.py.

    def _get(
        self, url: str, params: dict | None = None, crawl: bool = False
    ) -> str | None:
        """Fetch a page, returning None if it does not exist."""
        try:
            return self._client.get(url, params, crawl=crawl)
        except NotFoundError:
            logger.debug(f"404 for {url}")
            return None

    def _get_json(
        self, url: str, params: dict | None = None, crawl: bool = False
    ) -> dict | None:
        """Fetch a JSON endpoint, returning None if it does not exist."""
        try:
            return self._client.get_json(url, params, crawl=crawl)
        except NotFoundError:
            logger.debug(f"404 for {url}")
            return None

    def _get_bytes(self, url: str, crawl: bool = False) -> bytes | None:
        """Fetch binary content, returning None if it does not exist."""
        try:
            return self._client.get_bytes(url, crawl=crawl)
        except NotFoundError:
            logger.debug(f"404 for {url}")
            return None

    def fetch_recent_filtered(
        self,
        mode: str,
        months: list[str],
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent items across months, filtering by date range.

        Calls ``fetch_recently_created`` or ``fetch_recently_modified`` for
        each month, then filters results whose date falls within
        [from_date, to_date].

        Args:
            mode: ``"created"`` or ``"modified"``.
            months: List of ``"YYYY-MM"`` strings to fetch.
            from_date: Inclusive start date, or None for no lower bound.
            to_date: Inclusive end date, or None for no upper bound.

        Returns:
            List of item dicts matching the date range.
        """
        all_items: list[dict[str, Any]] = []
        for month in months:
            year = int(month.split("-")[0])
            batch = (
                self.fetch_recently_created(month)
                if mode == "created"
                else self.fetch_recently_modified(month)
            )
            if from_date or to_date:
                for item in batch:
                    item_date = extract_date(item, year=year)
                    if not item_date:
                        continue
                    if from_date and item_date < from_date:
                        continue
                    if to_date and item_date > to_date:
                        continue
                    all_items.append(item)
            else:
                all_items.extend(batch)
        return all_items

    def fetch_recent_page(
        self,
        mode: str = "created",
        month: str | None = None,
        start: int = 0,
        count: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch a single page of recently created/modified items.

        Requires ``_ARCHIVE_ENDPOINT`` and ``_ARCHIVE_ROW_PARSER`` on the subclass.

        The endpoint serves fixed ``_ARCHIVE_BLOCK``-row blocks and ignores the
        requested page size, so the window is assembled by
        ``_fetch_row_window``. Any ``count`` is therefore honoured, but a whole
        block is transferred regardless: asking for fewer rows saves no
        bandwidth.

        Args:
            mode: ``"created"`` or ``"modified"``.
            month: Month in ``YYYY-MM`` format. Defaults to current month.
            start: Offset into results (for pagination).
            count: Page size.

        Returns:
            Tuple of (list of item dicts, total matching records).
        """
        if not self._ARCHIVE_ENDPOINT or not self._ARCHIVE_ROW_PARSER:
            raise NotImplementedError(
                f"{type(self).__name__} does not support archive listings."
            )
        if month is None:
            month = date.today().strftime("%Y-%m")

        url = f"{self._base_url}{self._ARCHIVE_ENDPOINT}/selection/{month}/by/{mode}/json/1"
        date_key = "added_on" if mode == "created" else "modified_on"

        def fetch_block(block_start: int) -> tuple[list, int]:
            params = {
                "sEcho": 0,
                "iDisplayStart": block_start,
                "iDisplayLength": self._ARCHIVE_BLOCK,
            }
            data = self._get_json(url, params, crawl=True)
            if not data:
                return [], 0
            return data.get("aaData", []), data.get("iTotalRecords", 0)

        rows, total = self._fetch_row_window(
            fetch_block, start, count, self._ARCHIVE_BLOCK
        )
        row_parser = getattr(self, self._ARCHIVE_ROW_PARSER)
        return [r for row in rows if (r := row_parser(row, date_key))], total

    def fetch_recently_created(
        self, month: str | None = None, batch_size: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch all items recently created for a given month."""
        return self._fetch_all_archive("created", month, batch_size)

    def fetch_recently_modified(
        self, month: str | None = None, batch_size: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch all items recently modified for a given month."""
        return self._fetch_all_archive("modified", month, batch_size)

    def _fetch_all_archive(
        self, mode: str, month: str | None, batch_size: int,
    ) -> list[dict[str, Any]]:
        """Fetch all items from an archive listing (created or modified)."""
        return self._paginate(
            lambda start: self.fetch_recent_page(mode, month, start, batch_size),
            batch_size=batch_size,
            label=f"{mode} items for {month}",
            verbose=True,
        )

    def _paginate(
        self,
        fetch_page: Callable[[int], tuple[list[dict[str, Any]], int]],
        batch_size: int,
        label: str,
        verbose: bool = True,
    ) -> list[dict[str, Any]]:
        """Accumulate all pages from a ``(start) -> (items, total)`` fetcher.

        Args:
            fetch_page: Callable taking a start offset, returning
                ``(items, total_records)``. Use a closure to bind endpoint,
                filter, and row-parser context.
            batch_size: Page size passed in by the closure; used to advance start.
            label: Human-readable noun phrase for progress logs (e.g.
                ``"bands for letter A"``, ``"upcoming releases"``).
            verbose: If True, log start/progress/completion.
        """
        all_items: list[dict[str, Any]] = []
        start = 0
        while True:
            results, total = fetch_page(start)
            if start == 0 and verbose:
                logger.info(f"Fetching {total} {label}")
            if not results:
                break
            all_items.extend(results)
            if verbose and len(all_items) < total:
                logger.debug(f"  Progress: {len(all_items)}/{total}")
            if len(all_items) >= total:
                break
            start += batch_size
        if verbose:
            logger.success(f"Completed: {len(all_items)} {label}")
        return all_items

    @staticmethod
    def _fetch_row_window(
        fetch_block: Callable[[int], tuple[list, int]],
        start: int,
        count: int,
        block: int,
    ) -> tuple[list, int]:
        """Assemble the raw rows for ``[start, start + count)`` from server blocks.

        The listing endpoints ignore ``iDisplayLength`` and floor
        ``iDisplayStart`` to a multiple of their own fixed block size: asking
        for rows 25-49 returns the whole block starting at 0. Left uncorrected,
        a caller paging in steps of 25 gets the same rows every time.

        So the containing block is requested, the window sliced out of it, and
        a second block pulled when the window straddles a boundary. Slicing
        happens on raw rows, before parsing, so rows the parser rejects do not
        shift the window.

        Args:
            fetch_block: Callable taking a block-aligned offset and returning
                ``(raw_rows, total_records)``.
            start: First row wanted.
            count: How many rows are wanted.
            block: The endpoint's server-side block size.

        Returns:
            Tuple of (raw rows for the window, total matching records).
        """
        block_start = (start // block) * block
        rows, total = fetch_block(block_start)
        if not rows:
            return [], total

        if len(rows) != block and block_start + len(rows) < total:
            logger.warning(
                f"Expected {block} rows per block but got {len(rows)}; "
                f"listing pagination may be misaligned."
            )

        offset = start - block_start
        window = rows[offset : offset + count]
        next_start = block_start + len(rows)
        while len(window) < count and next_start < total:
            # Bind the follow-up total separately: a failed block answers
            # (etc, 0), and assigning that straight to `total` would report the
            # listing as empty even though the window holds rows.
            more, more_total = fetch_block(next_start)
            if not more:
                break
            total = more_total
            window.extend(more[: count - len(window)])
            next_start += len(more)
        return window, total

    def _fetch_and_parse(
        self, url: str, parser_class: Callable, **kwargs
    ) -> dict | None:
        """Fetch a detail page, run it through a parser, and return the result.

        This is the standard pattern for all ``get()`` methods: fetch HTML,
        build a BeautifulSoup tree, pass it to the appropriate parser, and
        return the parsed dict. Extra kwargs are forwarded to the parser
        constructor (e.g. ``with_tracklist=True`` for AlbumPageParser).
        Unknown kwargs are silently ignored so that callers can pass
        generic options like ``full=`` without breaking parsers that
        don't accept them.

        Args:
            url: The detail page URL (e.g. a band or album page).
            parser_class: One of the parser classes from ``parsers.py``.
            **kwargs: Additional arguments passed to the parser constructor.

        Returns:
            Parsed entity dict with ``_type`` key, or None on fetch failure.

        Raises:
            NotFoundError: If the page no longer exists, so callers can tell a
                deleted entry from a failed request.
        """
        html = self._client.get(url)
        if not html:
            return None
        valid = _parser_kwargs(parser_class)
        filtered = {k: v for k, v in kwargs.items() if k in valid}
        parser = parser_class(BeautifulSoup(html, "html.parser"), url, **filtered)
        return parser.parse()

    def _generic_search(
        self, endpoint: str, query: str, row_parser: Callable, exact_match: bool = True
    ) -> list[dict]:
        """Search a Metal Archives AJAX endpoint and return parsed results.

        Metal Archives search endpoints return JSON with an ``aaData`` array
        where each element is a list of HTML-encoded cells (jQuery DataTables
        format). Each cell is parsed by the entity-specific ``row_parser``.

        When ``exact_match`` is True (default), results are filtered to only
        include entries whose name matches the query exactly (case-insensitive).
        This is useful because MA's search is fuzzy and often returns partial
        matches.

        Args:
            endpoint: Relative API path, e.g. ``/search/ajax-band-search/``.
            query: The search string entered by the user.
            row_parser: Entity-specific method that converts a raw row (list
                of HTML strings) into a typed dict, or None to skip.
            exact_match: If True, filter results to exact name matches only.

        Returns:
            List of parsed entity dicts, possibly empty.
        """
        data = self._get_json(f"{self._base_url}{endpoint}", {"query": query})
        if not data:
            return []

        results = [
            parsed for item in data.get("aaData", []) if (parsed := row_parser(item))
        ]

        if exact_match:
            return [r for r in results if r.get("name", "").lower() == query.lower()]
        return results

    def _generic_advanced_search(
        self,
        endpoint: str,
        row_parser: Callable,
        defaults: dict[str, Any],
        start: int = 0,
        count: int = 200,
        **filters: Any,
    ) -> tuple[list[dict], int]:
        """Run an advanced search with arbitrary filters and pagination.

        Args:
            endpoint: The advanced search endpoint path (e.g.
                ``/search/ajax-advanced/searching/bands``).
            row_parser: Method to parse each result row into a dict.
            defaults: Default parameter dict for this entity type.
            start: Offset into results (for pagination).
            count: Number of results per page.
            **filters: Override any default params with these values.

        Returns:
            Tuple of (list of parsed dicts, total number of matching records).
        """
        params = {
            **defaults,
            "sEcho": 1,
            "iDisplayStart": start,
            "iDisplayLength": count,
        }
        params.update(filters)

        url = f"{self._base_url}{endpoint}"
        data = self._get_json(url, params)
        if not data:
            return [], 0

        results = [
            parsed for row in data.get("aaData", []) if (parsed := row_parser(row))
        ]
        return results, data.get("iTotalRecords", len(results))

    def advanced_search(
        self, start: int = 0, count: int = 200, **filters: Any
    ) -> tuple[list[dict], int]:
        """Run an advanced search with filters and pagination.

        Uses the class-level ``_ADVANCED_ENDPOINT``, ``_ADVANCED_DEFAULTS``,
        and ``_ADVANCED_ROW_PARSER`` to dispatch to ``_generic_advanced_search``.

        Args:
            start: Offset into results (for pagination).
            count: Number of results per page.
            **filters: Override any default params with these values.

        Returns:
            Tuple of (list of parsed dicts, total number of matching records).

        Raises:
            NotImplementedError: If the subclass doesn't define the required
                class attributes for advanced search.
        """
        if (
            not self._ADVANCED_ENDPOINT
            or self._ADVANCED_DEFAULTS is None
            or not self._ADVANCED_ROW_PARSER
        ):
            raise NotImplementedError(
                f"{type(self).__name__} does not support advanced search."
            )
        columns = self._advanced_layout(filters)
        row_parser = getattr(self, self._ADVANCED_ROW_PARSER)
        return self._generic_advanced_search(
            self._ADVANCED_ENDPOINT,
            lambda row: row_parser(row, columns),
            self._ADVANCED_DEFAULTS,
            start=start,
            count=count,
            **filters,
        )

    def _advanced_layout(self, filters: dict[str, Any]) -> list[str]:
        """Return the field name of each result column for these filters.

        The advanced endpoints build their result table out of the filters that
        were actually supplied, so the column layout is not fixed: extra columns
        appear for some filters, and one column can change meaning entirely.
        Parsing by hardcoded index therefore mislabels data as soon as the
        filter combination changes. Subclasses that support advanced search
        override this to describe their own table.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not describe an advanced column layout."
        )

    @staticmethod
    def _apply_layout(
        result: dict, row: list[str], columns: list[str], skip: tuple[str, ...]
    ) -> dict:
        """Fill ``result`` from ``row`` according to ``columns``.

        Cells are read as HTML: Metal Archives embeds sort keys as comments
        (``1990 <!-- 1990-00-00 -->``), which leak into the value if the cell
        is treated as plain text.
        """
        for index, field in enumerate(columns):
            if field in skip or index >= len(row):
                continue
            result[field] = _html_text(row[index])
        return result

    def fetch_lyrics(self, song_id: str) -> str | None:
        """Fetch song lyrics via the AJAX lyrics endpoint.

        Metal Archives serves lyrics as an HTML fragment from
        ``/release/ajax-view-lyrics/id/{song_id}``. The response is stripped
        of HTML tags and validated: short strings, loading placeholders, and
        error messages are discarded.

        The endpoint answers 200 with a sentinel body rather than 404 when a
        track has no stored lyrics, so those bodies are filtered by content or
        they would render as the lyrics themselves. ``(instrumental)`` is
        deliberately kept: that one is real information about the track.

        Args:
            song_id: The numeric song ID (from a tracklist entry).

        Returns:
            Plain-text lyrics, or None if unavailable or too short.
        """
        url = f"{self._base_url}/release/ajax-view-lyrics/id/{song_id}"
        html = self._get(url)
        if not html:
            return None
        soup = BeautifulSoup(html.strip(), "html.parser")
        text = soup.get_text().strip()
        if not text or len(text) <= 5:
            return None
        if text.lower().startswith(_LYRICS_PLACEHOLDERS):
            logger.debug(f"No lyrics stored for song {song_id}: {text[:40]!r}")
            return None
        return text


class BandAPI(BaseAPI):
    """Search and fetch bands, discographies, descriptions, and similar artists.

    Band search results include name, genre, and country. The ``get()`` method
    fetches the full band page and always includes the discography. Additional
    data (description, similar artists) can be fetched lazily or eagerly via
    the ``full`` flag.
    """

    @staticmethod
    def url(band_id: str) -> str:
        """Build a band page URL from an ID.

        Uses the ID route rather than the ``/bands//{id}`` slug form. With an
        empty slug Metal Archives answers 200 and a placeholder band page for
        an ID that no longer exists, so a deleted band parses as a real one and
        callers can never tell it went away. The ID route 404s properly.
        """
        return f"{BASE_URL}/band/view/id/{band_id}"

    def _parse_search_row(self, row: list[str]) -> dict | None:
        if len(row) < 3 or not (name_link := parse_link(row[0])):
            return None
        return {
            "_type": "band",
            "name": normalize_text(name_link.text),
            "url": name_link.get("href", ""),
            "id": extract_id_from_url(name_link.get("href", "")),
            "genre": normalize_text(row[1]),
            "country": normalize_text(row[2]),
        }

    def search(self, query: str, exact_match: bool = True) -> list[dict]:
        """Search bands by name via ``/search/ajax-band-search/``.

        Args:
            query: Band name to search for.
            exact_match: If True, only return bands whose name matches exactly.

        Returns:
            List of band dicts with keys: ``_type``, ``name``, ``url``, ``id``,
            ``genre``, ``country``.
        """
        return self._generic_search(
            "/search/ajax-band-search/", query, self._parse_search_row, exact_match
        )

    _ADVANCED_ENDPOINT = "/search/ajax-advanced/searching/bands"
    _ADVANCED_ROW_PARSER = "_parse_advanced_row"
    _ARCHIVE_ENDPOINT = "/archives/ajax-band-list"
    _ARCHIVE_ROW_PARSER = "_parse_archive_row"
    _ADVANCED_DEFAULTS: ClassVar[dict[str, str]] = {
        "bandName": "",
        "genre": "",
        "country": "",
        "yearCreationFrom": "",
        "yearCreationTo": "",
        "bandNotes": "",
        "status": "",
        "themes": "",
        "location": "",
        "bandLabelName": "",
    }

    # Optional band columns, in the order the endpoint appends them.
    _ADVANCED_EXTRAS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("location", "location"),
        ("themes", "themes"),
        ("bandLabelName", "label"),
    )

    def _advanced_layout(self, filters: dict[str, Any]) -> list[str]:
        """Column layout for the advanced band table.

        The third column reports the band's location rather than its country
        once the search is already constrained to a single country, so reading
        it as ``country`` would file a city under a country name.
        """
        columns = ["name", "genre", "location" if filters.get("country") else "country"]
        for param, field in self._ADVANCED_EXTRAS:
            if filters.get(param) and field not in columns:
                columns.append(field)
        return columns

    def _parse_advanced_row(self, row: list[str], columns: list[str]) -> dict | None:
        """Parse an advanced band search row against its column layout."""
        if not row or not (name_link := parse_link(row[0])):
            return None
        url = name_link.get("href", "")
        result = {
            "_type": "band",
            "name": normalize_text(name_link.text),
            "url": url,
            "id": extract_id_from_url(url),
            "genre": "",
            "country": "",
        }
        return self._apply_layout(result, row, columns, skip=("name",))

    def advanced_search(
        self, start: int = 0, count: int = 200, **filters: Any
    ) -> tuple[list[dict], int]:
        """Advanced band search, filling in the country the results share.

        When filtering by country the endpoint drops that column (every hit
        matches it), which would otherwise leave every result displaying an
        unknown country.
        """
        results, total = super().advanced_search(start, count, **filters)
        country = CODE_TO_COUNTRY.get(str(filters.get("country") or ""))
        if country:
            for result in results:
                if not result.get("country"):
                    result["country"] = country
        return results, total

    def get_random(
        self, full: bool = False, with_media: bool = True, **kwargs
    ) -> dict | None:
        """Fetch a random band from Metal Archives.

        Uses the ``/band/random`` endpoint which redirects to a random band
        page. Parses it the same way as ``get()``.

        Args:
            full: If True, eagerly fetch description and similar artists.
            with_media: If False, skip downloading the logo bytes.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Band dict, or None if the request failed.
        """
        url = f"{self._base_url}/band/random"
        result = self._client.get_with_url(url)
        if not result:
            return None
        html, final_url = result
        valid = _parser_kwargs(BandPageParser)
        filtered = {k: v for k, v in kwargs.items() if k in valid}
        soup = BeautifulSoup(html, "html.parser")
        band = BandPageParser(soup, final_url, **filtered).parse()
        return self._enrich_band(band, full, with_media)

    def get(
        self,
        band_url: str,
        full: bool = False,
        with_media: bool = True,
        **kwargs,
    ) -> dict | None:
        """Fetch and parse a band's detail page.

        Always fetches the full discography (one extra AJAX request). The band
        logo is also downloaded in-memory for terminal display, unless
        ``with_media`` is False.

        When ``full`` is True, two additional requests are made to fetch the
        band's text description and list of similar artists. In the interactive
        CLI these are fetched lazily on demand instead.

        Args:
            band_url: Full URL of the band page.
            full: If True, eagerly fetch description and similar artists.
            with_media: If False, skip downloading the logo bytes. ``logo_url``
                is still returned, only ``_logo_data`` is omitted. Bulk callers
                that do not render images should pass False: the fetch costs a
                full rate-limited request per band.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Band dict with members, discography, and optionally description
            and similar artists. None if the page could not be fetched.
        """
        band = self._fetch_and_parse(band_url, BandPageParser, **kwargs)
        return self._enrich_band(band, full, with_media)

    def _enrich_band(
        self, band: dict | None, full: bool = False, with_media: bool = True
    ) -> dict | None:
        """Add discography, logo, and optional extras to a parsed band dict."""
        if not band:
            return None

        if band.get("id"):
            band_id, band_name = band["id"], band["name"]
            band["discography"] = self._fetch_discography(band_id, band_name)
            if full:
                logger.info(f"Fetching extra data for {band_name}...")
                band["description"] = self.fetch_description(band_id)
                band["similar_artists"] = self.fetch_similar_artists(band_id)

        if with_media and band.get("logo_url"):
            band["_logo_data"] = self._get_bytes(band["logo_url"])

        return band

    def fetch_description(self, band_id: str) -> str | None:
        """Fetch the full band description via the read-more AJAX endpoint.

        The description is served as an HTML fragment from
        ``/band/read-more/id/{band_id}``. Links are stripped (unwrapped to
        plain text), ``<br>`` tags are converted to newlines, and excessive
        blank lines are collapsed. A trailing "Read more" label is removed.

        Args:
            band_id: The numeric band ID.

        Returns:
            Cleaned plain-text description, or None if empty or too short
            (under 20 characters, which usually means placeholder content).
        """
        url = f"{self._base_url}/band/read-more/id/{band_id}"
        html = self._get(url)
        if not html:
            return None

        soup = BeautifulSoup(html.strip(), "html.parser")
        text = extract_text_block(soup)
        if text:
            text = text.removesuffix("Read more").strip()
        if text and text.lower().startswith(_DESCRIPTION_PLACEHOLDERS):
            logger.debug(f"No description for band {band_id}: {text[:40]!r}")
            return None
        return text or None

    def _fetch_discography(self, band_id: str, band_name: str | None = None) -> list[dict]:
        """Fetch the full discography via the AJAX tab endpoint.

        Hits ``/band/discography/id/{band_id}/tab/all`` which returns an HTML
        table with one row per release. Each row contains the album name/link,
        type (Full-length, EP, Demo, etc.), year, and optionally a review
        summary (e.g. ``"12 (66%)"``).

        Args:
            band_id: The numeric band ID.
            band_name: Optional band name to include in each album dict for
                display purposes.

        Returns:
            List of album dicts, each with ``_type``, ``name``, ``url``,
            ``id``, ``band``, ``album_type``, ``release_date``, and optionally
            ``review_summary``.
        """
        url = f"{self._base_url}/band/discography/id/{band_id}/tab/all"
        html = self._get(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        albums = []
        for row in soup.select("tbody tr"):
            cells = row.find_all("td")
            if not cells or len(cells) < 3:
                continue
            name_link = cells[0].find("a")
            if not name_link:
                continue

            album = {
                "_type": "album",
                "name": normalize_text(name_link.text),
                "url": name_link.get("href"),
                "id": extract_id_from_url(name_link.get("href")),
                "band": band_name,
                "album_type": normalize_text(cells[1].text),
                "release_date": normalize_text(cells[2].text),
            }

            if len(cells) >= 4:
                review_text = normalize_text(cells[3].text)
                if review_text:
                    album["review_summary"] = review_text

            albums.append(album)

        return albums

    def fetch_similar_artists(self, band_id: str) -> list[dict]:
        """Fetch similar artists via the AJAX recommendations endpoint.

        Hits ``/band/ajax-recommendations/id/{band_id}`` which returns an HTML
        table of user-submitted band recommendations. Each row has band name,
        country, genre, and a similarity score (percentage).

        Rows containing the "Show more" link are filtered out.

        Args:
            band_id: The numeric band ID.

        Returns:
            List of band dicts with ``_type``, ``name``, ``url``, ``id``,
            ``country``, ``genre``, and ``score`` (int percentage or None).
        """
        url = f"{self._base_url}/band/ajax-recommendations/id/{band_id}"
        html = self._get(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        return [
            {
                "_type": "band",
                "name": normalize_text(name_link.text),
                "url": name_link.get("href"),
                "id": extract_id_from_url(name_link.get("href")),
                "country": normalize_text(cells[1].text),
                "genre": normalize_text(cells[2].text),
                "score": parse_rating(cells[3].text),
            }
            for row in soup.find_all("tr")
            if (cells := row.find_all("td"))
            and len(cells) == 4
            and (name_link := cells[0].find("a"))
            and "showMoreSimilar" not in name_link.get("href", "")
        ]

    def fetch_band_list_letter(
        self, letter: str, batch_size: int = 500, verbose: bool = True
    ) -> list[dict[str, Any]]:
        """Fetch all bands starting with a given letter from the alphabetical listing.

        Uses the paginated ``/browse/ajax-letter/`` endpoint, fetching in
        batches until all bands are retrieved. Special letters ``#`` (numbers)
        and ``~`` (non-Latin) are supported.

        This method uses the crawl delay (3s) between requests to avoid
        overloading the server, as it can generate many requests for popular
        letters.

        Args:
            letter: Single letter (A-Z), ``#`` for numbers, or ``~`` for
                non-Latin characters.
            batch_size: Number of bands to request per page (max 500).
            verbose: If True, log progress updates.

        Returns:
            List of band dicts with ``name``, ``url``, ``id``, ``country``,
            ``genre``, and ``status``.
        """
        url = f"{self._base_url}/browse/ajax-letter/l/{letter}/json/1"

        def fetch_page(start: int) -> tuple[list[dict[str, Any]], int]:
            params = {"sEcho": 0, "iDisplayStart": start, "iDisplayLength": batch_size}
            data = self._get_json(url, params, crawl=True)
            if not data:
                return [], 0
            rows = data.get("aaData", [])
            results = [b for row in rows if (b := self._parse_band_list_row(row))]
            return results, data.get("iTotalRecords", 0)

        return self._paginate(
            fetch_page, batch_size, label=f"bands for letter: {letter}", verbose=verbose,
        )

    def fetch_all_bands_list(self) -> list[dict[str, Any]]:
        """Fetch all bands from the alphabetical listing across all 28 letters.

        Iterates over A-Z plus ``#`` and ``~``, calling
        ``fetch_band_list_letter`` for each. This is a long-running operation
        (150k+ bands, hundreds of requests) intended for building a local
        database or cache.

        Returns:
            Complete list of band dicts from the entire Metal Archives catalog.
        """
        letters = ["#", *"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "~"]
        all_bands = []

        logger.info(f"Starting to fetch all bands from {len(letters)} letters...")

        for letter in letters:
            bands = self.fetch_band_list_letter(letter, verbose=True)
            all_bands.extend(bands)
            logger.info(f"  Running total: {len(all_bands)} bands")

        logger.success(f"Completed! Total bands: {len(all_bands)}")
        return all_bands

    def _parse_band_list_row(self, row: list[str]) -> dict[str, Any] | None:
        if len(row) < 4:
            return None

        name_link = parse_link(row[0])
        if not name_link:
            return None

        band_url = name_link.get("href", "")

        return {
            "_type": "band",
            "name": normalize_text(name_link.text),
            "url": band_url,
            "id": extract_id_from_url(band_url),
            "country": normalize_text(row[1]),
            "genre": normalize_text(row[2]),
            "status": _html_text(row[3]),
        }

    def _parse_archive_row(
        self, row: list[str], date_key: str
    ) -> dict[str, Any] | None:
        """Parse a row from the band archive listing (6 columns).

        Columns: [grouping (hidden), band_link, country_link, genre, date, user].
        """
        if len(row) < 5:
            return None

        band_link = parse_link(row[1])
        if not band_link:
            return None

        band_url = band_link.get("href", "")

        return {
            "_type": "band",
            "name": normalize_text(band_link.text),
            "url": band_url,
            "id": extract_id_from_url(band_url),
            "country": _html_text(row[2]),
            "genre": normalize_text(row[3]),
            date_key: normalize_text(row[4]),
        }


class AlbumAPI(BaseAPI):
    """Search and fetch albums with tracklists, lineups, and reviews.

    Album search results include the album name and its parent band. The
    ``get()`` method fetches the full album page with tracklist and lineup
    by default, and downloads the cover art in-memory for terminal display.
    """

    _UPCOMING_BLOCK = 100  # rows the upcoming endpoint returns per request

    @staticmethod
    def url(album_id: str) -> str:
        """Build an album page URL from an ID."""
        return f"{BASE_URL}/albums///{album_id}"

    def _parse_search_row(self, row: list[str]) -> dict | None:
        if len(row) < 2:
            return None
        band_link = parse_link(row[0])
        album_link = parse_link(row[1])
        if band_link and album_link:
            return {
                "_type": "album",
                "name": normalize_text(album_link.text),
                "band": normalize_text(band_link.text),
                "url": album_link.get("href", ""),
                "id": extract_id_from_url(album_link.get("href", "")),
            }
        return None

    def search(self, query: str, exact_match: bool = True) -> list[dict]:
        """Search albums by name via ``/search/ajax-album-search/``.

        Args:
            query: Album name to search for.
            exact_match: If True, only return albums whose name matches exactly.

        Returns:
            List of album dicts with keys: ``_type``, ``name``, ``band``,
            ``url``, ``id``.
        """
        return self._generic_search(
            "/search/ajax-album-search/", query, self._parse_search_row, exact_match
        )

    _ADVANCED_ENDPOINT = "/search/ajax-advanced/searching/albums"
    _ADVANCED_ROW_PARSER = "_parse_advanced_row"
    _ADVANCED_DEFAULTS: ClassVar[dict[str, str]] = {
        "bandName": "",
        "releaseTitle": "",
        "releaseYearFrom": "",
        "releaseYearTo": "",
        "releaseMonthFrom": "",
        "releaseMonthTo": "",
        "country": "",
        "location": "",
        "releaseLabelName": "",
        "releaseCatalogNumber": "",
        "releaseIdentifiers": "",
        "releaseRecordingInfo": "",
        "releaseDescription": "",
        "releaseNotes": "",
        "genre": "",
    }

    # Optional album columns, in the order the endpoint appends them.
    _ADVANCED_EXTRAS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("genre", "genre"),
        ("location", "location"),
        ("releaseLabelName", "label"),
        ("releaseYearFrom", "release_date"),
    )

    def _advanced_layout(self, filters: dict[str, Any]) -> list[str]:
        """Column layout for the advanced album table.

        Only the band, title and type columns are always present; genre,
        location, label and release date each appear only when the matching
        filter was supplied. Reading the date from a fixed index puts it in
        ``genre`` for a year-only search.
        """
        columns = ["band", "name", "album_type"]
        for param, field in self._ADVANCED_EXTRAS:
            if filters.get(param) or (
                field == "release_date" and filters.get("releaseYearTo")
            ):
                columns.append(field)
        return columns

    def _parse_advanced_row(self, row: list[str], columns: list[str]) -> dict | None:
        """Parse an advanced album search row against its column layout."""
        if len(row) < 2:
            return None
        band_link = parse_link(row[0])
        album_link = parse_link(row[1])
        if not band_link or not album_link:
            return None
        url = album_link.get("href", "")
        result = {
            "_type": "album",
            "name": normalize_text(album_link.text),
            "band": normalize_text(band_link.text),
            "url": url,
            "id": extract_id_from_url(url),
            "album_type": "",
            "genre": "",
            "release_date": "",
        }
        return self._apply_layout(result, row, columns, skip=("band", "name"))

    def get(self, album_url: str, with_media: bool = True, **kwargs) -> dict | None:
        """Fetch and parse an album's detail page.

        Parses the full album page including tracklist and lineup. The album
        cover art is downloaded in-memory for terminal display, unless
        ``with_media`` is False.

        Args:
            album_url: Full URL of the album page.
            with_media: If False, skip downloading the cover bytes.
                ``cover_url`` is still returned, only ``_cover_data`` is
                omitted. Bulk callers that do not render images should pass
                False: the fetch costs a full rate-limited request per album.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Album dict with tracklist, lineup, and cover data, or None on
            fetch failure.
        """
        kwargs.setdefault("with_tracklist", True)
        kwargs.setdefault("with_lineup", True)
        album = self._fetch_and_parse(album_url, AlbumPageParser, **kwargs)
        if album and with_media and album.get("cover_url"):
            album["_cover_data"] = self._get_bytes(album["cover_url"])
        return album

    def fetch_upcoming_page(
        self,
        start: int = 0,
        count: int = 100,
        from_date: str | None = None,
        to_date: str | None = None,
        include_versions: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch a single page of upcoming album releases.

        As with the archive listings, the endpoint serves fixed
        ``_UPCOMING_BLOCK``-row blocks and ignores the requested page size, so
        the window is assembled by ``_fetch_row_window``.

        Args:
            start: Offset into results (for pagination).
            count: Page size.
            from_date: Start date in ``YYYY-MM-DD`` format (default: today).
            to_date: End date in ``YYYY-MM-DD`` format (default: open-ended).
            include_versions: Whether to include re-releases/versions.

        Returns:
            Tuple of (list of album dicts, total matching records).
        """
        url = f"{self._base_url}/release/ajax-upcoming/json/1"
        extra_params = {"includeVersions": 1 if include_versions else 0}
        if from_date:
            extra_params["fromDate"] = from_date
        if to_date:
            extra_params["toDate"] = to_date

        def fetch_block(block_start: int) -> tuple[list, int]:
            params = {
                "sEcho": 0,
                "iDisplayStart": block_start,
                "iDisplayLength": self._UPCOMING_BLOCK,
                **extra_params,
            }
            data = self._get_json(url, params, crawl=True)
            if not data:
                return [], 0
            return data.get("aaData", []), data.get("iTotalRecords", 0)

        rows, total = self._fetch_row_window(
            fetch_block, start, count, self._UPCOMING_BLOCK
        )
        return [r for row in rows if (r := self._parse_upcoming_row(row))], total

    def fetch_upcoming(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        include_versions: bool = False,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch all upcoming album releases from Metal Archives.

        Args:
            from_date: Start date in ``YYYY-MM-DD`` format (default: today).
            to_date: End date in ``YYYY-MM-DD`` format (default: open-ended).
            include_versions: Whether to include re-releases/versions.
            batch_size: Page size (server caps at 100).

        Returns:
            List of album dicts.
        """
        return self._paginate(
            lambda start: self.fetch_upcoming_page(
                start, batch_size, from_date, to_date, include_versions,
            ),
            batch_size=batch_size,
            label="upcoming releases",
            verbose=True,
        )

    @staticmethod
    def _parse_upcoming_row(row: list[str]) -> dict[str, Any] | None:
        """Parse a row from the upcoming releases listing (6 columns).

        Columns: [band_link, album_link, type, genre, release_date, added_date].
        """
        if len(row) < 5:
            return None

        band_link = parse_link(row[0])
        album_link = parse_link(row[1])
        if not band_link or not album_link:
            return None

        album_url = album_link.get("href", "")

        return {
            "_type": "album",
            "name": normalize_text(album_link.text),
            "band": normalize_text(band_link.text),
            "band_url": band_link.get("href", ""),
            "band_id": extract_id_from_url(band_link.get("href", "")),
            "url": album_url,
            "id": extract_id_from_url(album_url),
            "album_type": normalize_text(row[2]) if len(row) > 2 else "",
            "genre": normalize_text(row[3]) if len(row) > 3 else "",
            "release_date": normalize_text(row[4]) if len(row) > 4 else "",
            "added_on": normalize_text(row[5]) if len(row) > 5 else "",
        }


class ArtistAPI(BaseAPI):
    """Search and fetch artist profiles with band affiliations.

    Artist search results include the artist's name, real name, country, and
    a summary of their band memberships. The ``get()`` method fetches the
    full artist page with biography and a complete bands overview.
    """

    @staticmethod
    def url(artist_id: str) -> str:
        """Build an artist page URL from an ID."""
        return f"{BASE_URL}/artists//{artist_id}"

    def _parse_search_row(self, row: list[str]) -> dict | None:
        if not row or not (name_link := parse_link(row[0])):
            return None
        artist_url = name_link.get("href", "")
        result = {
            "_type": "artist",
            "name": normalize_text(name_link.text),
            "url": artist_url,
            "id": extract_id_from_url(artist_url),
            "real_name": (row[1].strip() or None) if len(row) > 1 else None,
            "country": _html_text(row[2]) if len(row) > 2 else None,
        }
        if len(row) > 3 and row[3].strip():
            result["bands"] = _html_text(row[3])
        return result

    def search(self, query: str, exact_match: bool = True) -> list[dict]:
        """Search artists by name via ``/search/ajax-artist-search/``.

        Args:
            query: Artist name to search for.
            exact_match: If True, only return artists whose name matches exactly.

        Returns:
            List of artist dicts with keys: ``_type``, ``name``, ``url``,
            ``real_name``, ``country``, and optionally ``bands``.
        """
        return self._generic_search(
            "/search/ajax-artist-search/", query, self._parse_search_row, exact_match
        )

    def get(self, artist_url: str, with_media: bool = True, **kwargs) -> dict | None:
        """Fetch and parse an artist's detail page.

        Parses the full artist page including biography, trivia, and a
        complete overview of all band memberships (active and past). The
        artist photo is downloaded in-memory for terminal display, unless
        ``with_media`` is False.

        Args:
            artist_url: Full URL of the artist page.
            with_media: If False, skip downloading the photo bytes.
                ``photo_url`` is still returned, only ``_photo_data`` is
                omitted. Bulk callers that do not render images should pass
                False: the fetch costs a full rate-limited request per artist.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Artist dict with biography and bands overview, or None on fetch
            failure.
        """
        artist = self._fetch_and_parse(artist_url, ArtistPageParser, **kwargs)
        if artist and with_media and artist.get("photo_url"):
            artist["_photo_data"] = self._get_bytes(artist["photo_url"])
        return artist


class LabelAPI(BaseAPI):
    """Search and fetch labels with rosters and release catalogs.

    Label search results include name, country, and specialisation (genre
    focus). The ``get()`` method fetches the label page along with three
    AJAX tables: current roster, past roster, and releases.
    """

    _ARCHIVE_ENDPOINT = "/archives/ajax-label-list"
    _ARCHIVE_ROW_PARSER = "_parse_label_archive_row"

    @staticmethod
    def url(label_id: str) -> str:
        """Build a label page URL from an ID."""
        return f"{BASE_URL}/labels//{label_id}"

    def _parse_search_row(self, row: list[str]) -> dict | None:
        if not row or not (name_link := parse_link(row[0])):
            return None
        return {
            "_type": "label",
            "name": normalize_text(name_link.text),
            "url": name_link.get("href", ""),
            "id": extract_id_from_url(name_link.get("href", "")),
            "country": normalize_text(row[1]) if len(row) > 1 else None,
            "specialisation": normalize_text(row[2]) if len(row) > 2 else None,
        }

    def search(self, query: str, exact_match: bool = True) -> list[dict]:
        """Search labels by name via ``/search/ajax-label-search/``.

        Args:
            query: Label name to search for.
            exact_match: If True, only return labels whose name matches exactly.

        Returns:
            List of label dicts with keys: ``_type``, ``name``, ``url``,
            ``id``, ``country``, ``specialisation``.
        """
        return self._generic_search(
            "/search/ajax-label-search/", query, self._parse_search_row, exact_match
        )

    def _fetch_ajax_table(self, endpoint: str, label_id: str) -> list[list]:
        """Fetch and parse a paginated AJAX table for a label.

        Used for roster and release data, which are served via separate AJAX
        endpoints (``ajax-bands``, ``ajax-bands-past``, ``ajax-albums``).
        Each cell is parsed from HTML: cells containing links are returned as
        dicts with ``text`` and ``links`` keys, plain text cells as strings.

        Args:
            endpoint: The AJAX endpoint name (e.g. ``ajax-bands``).
            label_id: The numeric label ID.

        Returns:
            List of parsed rows, where each row is a list of cell values.
        """
        url = f"{self._base_url}/label/{endpoint}/nbrPerPage/500/id/{label_id}"
        data = self._get_json(url)
        if not data:
            return []

        parsed_rows = []
        for row_data in data.get("aaData", []):
            parsed_row = []
            for cell in row_data:
                if isinstance(cell, str):
                    cell_soup = BeautifulSoup(cell, "html.parser")
                    links = cell_soup.find_all("a")
                    if links:
                        parsed_row.append(
                            {
                                "text": cell_soup.get_text(strip=True),
                                "links": [
                                    {
                                        "text": a.get_text(strip=True),
                                        "href": a.get("href"),
                                    }
                                    for a in links
                                ],
                            }
                        )
                    else:
                        parsed_row.append(cell_soup.get_text(strip=True))
                else:
                    parsed_row.append(str(cell))
            parsed_rows.append(parsed_row)

        return parsed_rows

    def fetch_label_list_letter(
        self, letter: str, batch_size: int = 200, verbose: bool = True
    ) -> list[dict[str, Any]]:
        """Fetch all labels starting with a given letter from the alphabetical listing.

        Uses the paginated ``/label/ajax-list/`` endpoint.

        Args:
            letter: Single letter (A-Z), ``#`` for numbers, or ``~`` for
                non-Latin characters.
            batch_size: Number of labels to request per page (server caps at 200).
            verbose: If True, log progress updates.

        Returns:
            List of label dicts with ``name``, ``url``, ``id``, ``country``,
            ``status``, and ``specialisation``.
        """
        url = f"{self._base_url}/label/ajax-list/l/{letter}/json/1"

        def fetch_page(start: int) -> tuple[list[dict[str, Any]], int]:
            params = {"sEcho": 0, "iDisplayStart": start, "iDisplayLength": batch_size}
            data = self._get_json(url, params, crawl=True)
            if not data:
                return [], 0
            rows = data.get("aaData", [])
            results = [lbl for row in rows if (lbl := self._parse_label_list_row(row))]
            return results, data.get("iTotalRecords", 0)

        return self._paginate(
            fetch_page, batch_size, label=f"labels for letter: {letter}", verbose=verbose,
        )

    def _parse_label_list_row(self, row: list[str]) -> dict[str, Any] | None:
        """Parse a row from the label alphabetical listing (7 columns).

        Columns: [edit_link, label_link, specialisation, status, country, ?, ?].
        """
        if len(row) < 5:
            return None

        label_link = parse_link(row[1])
        if not label_link:
            return None

        label_url = label_link.get("href", "")

        return {
            "_type": "label",
            "name": normalize_text(label_link.text),
            "url": label_url,
            "id": extract_id_from_url(label_url),
            "country": _html_text(row[4]),
            "status": _html_text(row[3]),
            "specialisation": _html_text(row[2]),
        }

    def _parse_label_archive_row(
        self, row: list[str], date_key: str
    ) -> dict[str, Any] | None:
        """Parse a row from the label archive listing (6 columns).

        Columns: [date_grouping, label_link, status, country, datetime, user].
        """
        if len(row) < 5:
            return None

        label_link = parse_link(row[1])
        if not label_link:
            return None

        label_url = label_link.get("href", "")

        return {
            "_type": "label",
            "name": normalize_text(label_link.text),
            "url": label_url,
            "id": extract_id_from_url(label_url),
            "status": _html_text(row[2]),
            "country": _html_text(row[3]),
            date_key: normalize_text(row[4]),
        }

    def get(self, label_url: str, **kwargs) -> dict | None:
        """Fetch and parse a label's detail page with rosters and releases.

        This is the most request-heavy ``get()`` method: it fetches the label
        page itself plus three AJAX tables (current roster, past roster, and
        releases). All four responses are combined by the parser.

        Args:
            label_url: Full URL of the label page.
            **kwargs: Currently unused, kept for API consistency.

        Returns:
            Label dict with contact info, rosters, and releases, or None on
            fetch failure.

        Raises:
            NotFoundError: If the label page no longer exists.
        """
        html = self._client.get(label_url)
        if not html:
            return None

        label_id = extract_id_from_url(label_url)

        roster_data = {}
        releases_data = []
        if label_id:
            roster_data["current"] = self._fetch_ajax_table("ajax-bands", label_id)
            roster_data["past"] = self._fetch_ajax_table("ajax-bands-past", label_id)
            releases_data = self._fetch_ajax_table("ajax-albums", label_id)

        parser = LabelPageParser(
            BeautifulSoup(html, "html.parser"),
            label_url,
            roster_data=roster_data,
            releases_data=releases_data,
        )
        return parser.parse()


class SongAPI(BaseAPI):
    """Search and fetch individual songs with lyrics.

    Songs are unique in that they don't have their own detail pages; they
    live inside album pages. The ``search()`` method returns song entries
    pointing to their parent album URL, and ``get()`` fetches that album
    page and extracts the specific song from the tracklist.

    Search results carry no song IDs (Metal Archives does not expose them
    there), so each result's ``url`` is the album URL tagged with a
    ``?song=<name>`` parameter naming the track. ``get()`` reads that tag back
    to know which row of the tracklist to extract. Making the reference
    self-describing means results stay resolvable no matter which call
    produced them, and two songs from the same album cannot shadow each other.
    Tracklist entries, which do have IDs, use a ``#song_id`` fragment instead;
    both forms are accepted.
    """

    _SONG_QUERY_KEY = "song"

    @classmethod
    def _tag_url(cls, album_url: str, song_name: str) -> str:
        """Tag an album URL with the song name it should resolve to."""
        if not album_url or not song_name:
            return album_url
        separator = "&" if urlsplit(album_url).query else "?"
        return f"{album_url}{separator}{cls._SONG_QUERY_KEY}={quote(song_name)}"

    @classmethod
    def _split_ref(cls, song_url: str) -> tuple[str, str | None, str | None]:
        """Split a song reference into ``(album_url, song_id, song_name)``.

        Accepts a plain album URL, one tagged with ``?song=``, one carrying a
        ``#song_id`` fragment, or both.
        """
        base, _, fragment = song_url.partition("#")
        parts = urlsplit(base)
        params = parse_qs(parts.query)
        names = params.pop(cls._SONG_QUERY_KEY, [])
        album_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(params, doseq=True), "")
        )
        return album_url, fragment or None, names[0] if names else None

    def _parse_search_row(self, row: list[str]) -> dict | None:
        if len(row) < 4:
            return None

        band_link = parse_link(row[0])
        album_link = parse_link(row[1])
        if not band_link or not album_link:
            return None

        name = normalize_text(row[3])
        return {
            "_type": "song",
            "name": name,
            "url": self._tag_url(album_link.get("href", ""), name),
            "id": None,
            "band": normalize_text(band_link.text),
            "album": normalize_text(album_link.text),
            "album_type": normalize_text(row[2]) if len(row) > 2 else None,
        }

    _ADVANCED_ENDPOINT = "/search/ajax-advanced/searching/songs"
    _ADVANCED_ROW_PARSER = "_parse_advanced_row"
    _ADVANCED_DEFAULTS: ClassVar[dict[str, str]] = {
        "songTitle": "",
        "bandName": "",
        "releaseTitle": "",
        "lyrics": "",
        "genre": "",
    }

    def _advanced_layout(self, filters: dict[str, Any]) -> list[str]:
        """Column layout for the advanced song table.

        Extra filters append columns after the title, but the four the parser
        reads (band, release, type, title) keep their positions, so the layout
        is only used to satisfy the shared calling convention.
        """
        return ["band", "album", "album_type", "name"]

    def _parse_advanced_row(self, row: list[str], columns: list[str]) -> dict | None:
        """Parse an advanced song search row (same leading columns as search)."""
        return self._parse_search_row(row)

    def search(self, query: str, exact_match: bool = True) -> list[dict]:
        """Search songs by name via ``/search/ajax-song-search/``.

        Args:
            query: Song name to search for.
            exact_match: If True, only return songs whose name matches exactly.

        Returns:
            List of song dicts with keys: ``_type``, ``name``, ``url`` (the
            album URL tagged with ``?song=``), ``id`` (None until ``get()``
            resolves it), ``band``, ``album``, ``album_type``.
        """
        return self._generic_search(
            "/search/ajax-song-search/", query, self._parse_search_row, exact_match
        )

    def get(self, song_url: str, **kwargs) -> dict | None:
        """Fetch a song's details by parsing its parent album page.

        Since songs don't have dedicated pages, this fetches the album page
        and uses ``SongPageParser`` to locate the specific song in the
        tracklist. The target song is identified by the ``#song_id`` fragment
        if present, otherwise by name, taken from the ``song_name`` kwarg or
        from the ``?song=`` tag that ``search()`` puts on the URL.

        Once the song is found, its numeric ID and a clean fragment URL
        (``album_url#song_id``) are set. Lyrics are fetched by default
        unless ``with_lyrics=False`` is passed.

        Args:
            song_url: Album URL, optionally tagged with ``?song=<name>``
                and/or a ``#song_id`` fragment.
            **kwargs: Optional ``song_name`` (str) to specify which song to
                extract, and ``with_lyrics`` (bool, default True) to control
                lyrics fetching.

        Returns:
            Song dict with album context, tracklist position, and optionally
            lyrics. None if the page could not be fetched or the song was
            not found in the tracklist.
        """
        album_url, song_id, tagged_name = self._split_ref(song_url)
        html = self._client.get(album_url)
        if not html:
            return None

        target_song_name = kwargs.get("song_name") or tagged_name
        if target_song_name:
            logger.debug(f"Resolving song by name: {target_song_name}")

        parser = SongPageParser(
            BeautifulSoup(html, "html.parser"),
            f"{album_url}#{song_id}" if song_id else album_url,
            target_song_name=target_song_name,
        )

        song = parser.parse()
        if not song:
            return None

        if parser.song_id:
            song["id"] = parser.song_id
            song["url"] = f"{album_url}#{parser.song_id}"

        if song.get("has_lyrics") and song.get("id") and kwargs.get("with_lyrics", True):
            lyrics = self.fetch_lyrics(song["id"])
            if lyrics:
                song["lyrics"] = lyrics

        return song
