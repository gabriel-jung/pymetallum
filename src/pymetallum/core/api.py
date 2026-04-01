"""API layer for Metal Archives.

Each entity type (band, album, artist, label, song) has a dedicated API class
that handles searching, fetching detail pages, and parsing AJAX endpoints.
All data is returned as plain dicts with a ``_type`` discriminator key.
"""

from collections.abc import Callable
from datetime import date
from typing import Any, ClassVar

from bs4 import BeautifulSoup
from loguru import logger

from .client import BASE_URL, MetalArchivesClient, NotFoundError
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


class BaseAPI:
    """Base class providing shared fetch/search/parse helpers for all entity APIs.

    All entity-specific API classes (BandAPI, AlbumAPI, etc.) inherit from this
    and rely on its HTTP helpers, generic search logic, and utility methods like
    image downloading and lyrics fetching.

    Subclasses that support advanced search should define:
    - ``_ADVANCED_ENDPOINT``: the search URL path
    - ``_ADVANCED_DEFAULTS``: default parameters dict
    - ``_ADVANCED_ROW_PARSER``: name of the method to parse result rows

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

    def __init__(self, client: MetalArchivesClient):
        self._client = client
        self._base_url = client.base_url

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

        Args:
            mode: ``"created"`` or ``"modified"``.
            month: Month in ``YYYY-MM`` format. Defaults to current month.
            start: Offset into results (for pagination).
            count: Page size (server caps at 200).

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
        params = {"sEcho": 0, "iDisplayStart": start, "iDisplayLength": count}
        data = self._client.get_json(url, params, crawl=True)
        if not data:
            return [], 0
        rows = data.get("aaData", [])
        total = data.get("iTotalRecords", 0)
        row_parser = getattr(self, self._ARCHIVE_ROW_PARSER)
        results = [r for row in rows if (r := row_parser(row, date_key))]
        return results, total

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
        all_items: list[dict[str, Any]] = []
        start = 0
        while True:
            results, total = self.fetch_recent_page(mode, month, start, batch_size)
            if start == 0:
                logger.info(f"Fetching {total} {mode} items for {month}")
            if not results:
                break
            all_items.extend(results)
            if len(all_items) >= total:
                break
            start += batch_size
        logger.success(f"Completed: {len(all_items)} {mode} items for {month}")
        return all_items

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
        """
        import inspect

        html = self._client.get(url)
        if not html:
            return None
        sig = inspect.signature(parser_class.__init__)
        valid = set(sig.parameters) - {"self"}
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
        data = self._client.get_json(f"{self._base_url}{endpoint}", {"query": query})
        if not data:
            return []

        results = [
            parsed for item in data.get("aaData", []) if (parsed := row_parser(item))
        ]

        if exact_match:
            return [r for r in results if r["name"].lower() == query.lower()]
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
        data = self._client.get_json(url, params)
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
        if not self._ADVANCED_ENDPOINT or self._ADVANCED_DEFAULTS is None:
            raise NotImplementedError(
                f"{type(self).__name__} does not support advanced search."
            )
        row_parser = getattr(self, self._ADVANCED_ROW_PARSER)
        return self._generic_advanced_search(
            self._ADVANCED_ENDPOINT,
            row_parser,
            self._ADVANCED_DEFAULTS,
            start=start,
            count=count,
            **filters,
        )

    def fetch_lyrics(self, song_id: str) -> str | None:
        """Fetch song lyrics via the AJAX lyrics endpoint.

        Metal Archives serves lyrics as an HTML fragment from
        ``/release/ajax-view-lyrics/id/{song_id}``. The response is stripped
        of HTML tags and validated — short strings, loading placeholders, and
        error messages are discarded.

        Args:
            song_id: The numeric song ID (from a tracklist entry).

        Returns:
            Plain-text lyrics, or None if unavailable or too short.
        """
        url = f"{self._base_url}/release/ajax-view-lyrics/id/{song_id}"
        html = self._client.get(url)
        if not html:
            return None
        soup = BeautifulSoup(html.strip(), "html.parser")
        text = soup.get_text().strip()
        if (
            text
            and len(text) > 5
            and not text.startswith("(loading")
            and not text.startswith("Error")
        ):
            return text
        return None


class BandAPI(BaseAPI):
    """Search and fetch bands, discographies, descriptions, and similar artists.

    Band search results include name, genre, and country. The ``get()`` method
    fetches the full band page and always includes the discography. Additional
    data (description, similar artists) can be fetched lazily or eagerly via
    the ``full`` flag.
    """

    @staticmethod
    def url(band_id: str) -> str:
        """Build a band page URL from an ID."""
        return f"{BASE_URL}/bands//{band_id}"

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
    _ADVANCED_ROW_PARSER = "_parse_search_row"
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

    def get_random(self, full: bool = False, **kwargs) -> dict | None:
        """Fetch a random band from Metal Archives.

        Uses the ``/band/random`` endpoint which redirects to a random band
        page. Parses it the same way as ``get()``.

        Args:
            full: If True, eagerly fetch description and similar artists.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Band dict, or None if the request failed.
        """
        url = f"{self._base_url}/band/random"
        result = self._client.get_with_url(url)
        if not result:
            return None
        html, final_url = result
        soup = BeautifulSoup(html, "html.parser")
        band = BandPageParser(soup, final_url, **kwargs).parse()
        return self._enrich_band(band, full)

    def get(self, band_url: str, full: bool = False, **kwargs) -> dict | None:
        """Fetch and parse a band's detail page.

        Always fetches the full discography (one extra AJAX request). The band
        photo is also downloaded in-memory for terminal display.

        When ``full`` is True, two additional requests are made to fetch the
        band's text description and list of similar artists. In the interactive
        CLI these are fetched lazily on demand instead.

        Args:
            band_url: Full URL of the band page.
            full: If True, eagerly fetch description and similar artists.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Band dict with members, discography, and optionally description
            and similar artists. None if the page could not be fetched.
        """
        band = self._fetch_and_parse(band_url, BandPageParser, **kwargs)
        return self._enrich_band(band, full)

    def _enrich_band(self, band: dict | None, full: bool = False) -> dict | None:
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

        if band.get("logo_url"):
            try:
                band["_logo_data"] = self._client.get_bytes(band["logo_url"])
            except NotFoundError:
                band["_logo_data"] = None

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
        html = self._client.get(url)
        if not html:
            return None

        soup = BeautifulSoup(html.strip(), "html.parser")
        text = extract_text_block(soup)
        if text:
            text = text.removesuffix("Read more").strip()
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
        try:
            html = self._client.get(url)
        except NotFoundError:
            return []
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
        html = self._client.get(url)
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
        all_bands = []
        start = 0
        url = f"{self._base_url}/browse/ajax-letter/l/{letter}/json/1"
        total_records = None

        while True:
            params = {"sEcho": 0, "iDisplayStart": start, "iDisplayLength": batch_size}

            data = self._client.get_json(url, params, crawl=True)
            if not data:
                break

            bands = data.get("aaData", [])
            total_records = data.get("iTotalRecords", 0)

            if start == 0 and verbose:
                logger.info(f"Fetching {total_records} bands for letter: {letter}")

            if not bands:
                break

            for row in bands:
                band = self._parse_band_list_row(row)
                if band:
                    all_bands.append(band)

            if verbose and len(all_bands) < total_records:
                logger.debug(f"  Progress: {len(all_bands)}/{total_records}")

            if len(all_bands) >= total_records:
                break

            start += batch_size

        if verbose:
            logger.success(f"Completed {letter}: {len(all_bands)} bands")

        return all_bands

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

    def _parse_advanced_row(self, row: list[str]) -> dict | None:
        """Parse a row from the advanced album search (5 columns)."""
        if len(row) < 3:
            return None
        band_link = parse_link(row[0])
        album_link = parse_link(row[1])
        if not band_link or not album_link:
            return None
        return {
            "_type": "album",
            "name": normalize_text(album_link.text),
            "band": normalize_text(band_link.text),
            "url": album_link.get("href", ""),
            "id": extract_id_from_url(album_link.get("href", "")),
            "album_type": normalize_text(row[2]) if len(row) > 2 else "",
            "genre": normalize_text(row[3]) if len(row) > 3 else "",
            "release_date": normalize_text(row[4]) if len(row) > 4 else "",
        }

    def get(self, album_url: str, **kwargs) -> dict | None:
        """Fetch and parse an album's detail page.

        Parses the full album page including tracklist and lineup. The album
        cover art is downloaded in-memory for terminal display.

        Args:
            album_url: Full URL of the album page.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Album dict with tracklist, lineup, and cover data, or None on
            fetch failure.
        """
        album = self._fetch_and_parse(
            album_url, AlbumPageParser, with_tracklist=True, with_lineup=True, **kwargs
        )
        if album and album.get("cover_url"):
            try:
                album["_cover_data"] = self._client.get_bytes(album["cover_url"])
            except NotFoundError:
                album["_cover_data"] = None
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

        Args:
            start: Offset into results (for pagination).
            count: Page size (server caps at 100).
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

        params = {
            "sEcho": 0,
            "iDisplayStart": start,
            "iDisplayLength": count,
            **extra_params,
        }
        data = self._client.get_json(url, params, crawl=True)
        if not data:
            return [], 0
        rows = data.get("aaData", [])
        total = data.get("iTotalRecords", 0)
        results = [r for row in rows if (r := self._parse_upcoming_row(row))]
        return results, total

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
        all_releases = []
        start = 0
        while True:
            results, total = self.fetch_upcoming_page(
                start, batch_size, from_date, to_date, include_versions
            )
            if start == 0:
                logger.info(f"Fetching {total} upcoming releases")
            if not results:
                break
            all_releases.extend(results)
            if len(all_releases) >= total:
                break
            start += batch_size
        logger.success(f"Completed: {len(all_releases)} upcoming releases")
        return all_releases

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
        result = {
            "_type": "artist",
            "name": normalize_text(name_link.text),
            "url": name_link.get("href", ""),
            "real_name": (row[1].strip() or None) if len(row) > 1 else None,
            "country": (
                normalize_text(BeautifulSoup(row[2], "html.parser").get_text())
                if len(row) > 2
                else None
            ),
        }
        if len(row) > 3 and row[3].strip():
            result["bands"] = normalize_text(
                BeautifulSoup(row[3], "html.parser").get_text()
            )
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

    def get(self, artist_url: str, **kwargs) -> dict | None:
        """Fetch and parse an artist's detail page.

        Parses the full artist page including biography, trivia, and a
        complete overview of all band memberships (active and past). The
        artist photo is downloaded in-memory for terminal display.

        Args:
            artist_url: Full URL of the artist page.
            **kwargs: Forwarded to the parser constructor.

        Returns:
            Artist dict with biography and bands overview, or None on fetch
            failure.
        """
        artist = self._fetch_and_parse(artist_url, ArtistPageParser, **kwargs)
        if artist and artist.get("photo_url"):
            try:
                artist["_photo_data"] = self._client.get_bytes(artist["photo_url"])
            except NotFoundError:
                artist["_photo_data"] = None
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
        try:
            data = self._client.get_json(url)
        except NotFoundError:
            return []
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
        all_labels = []
        start = 0
        url = f"{self._base_url}/label/ajax-list/l/{letter}/json/1"
        total_records = None

        while True:
            params = {"sEcho": 0, "iDisplayStart": start, "iDisplayLength": batch_size}
            data = self._client.get_json(url, params, crawl=True)
            if not data:
                break

            rows = data.get("aaData", [])
            total_records = data.get("iTotalRecords", 0)

            if start == 0 and verbose:
                logger.info(f"Fetching {total_records} labels for letter: {letter}")

            if not rows:
                break

            for row in rows:
                label = self._parse_label_list_row(row)
                if label:
                    all_labels.append(label)

            if verbose and len(all_labels) < total_records:
                logger.debug(f"  Progress: {len(all_labels)}/{total_records}")

            if len(all_labels) >= total_records:
                break

            start += batch_size

        if verbose:
            logger.success(f"Completed {letter}: {len(all_labels)} labels")

        return all_labels

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

    Songs are unique in that they don't have their own detail pages — they
    live inside album pages. The ``search()`` method returns song entries
    pointing to their parent album URL, and ``get()`` fetches that album
    page and extracts the specific song from the tracklist.

    A search result cache (``_last_search_results``) maps album URLs to
    song names, allowing ``get()`` to auto-detect which song to extract
    without the caller needing to pass the song name explicitly.
    """

    def __init__(self, client: MetalArchivesClient):
        super().__init__(client)
        self._last_search_results = {}

    def _parse_search_row(self, row: list[str]) -> dict | None:
        if len(row) < 4:
            return None

        band_link = parse_link(row[0])
        album_link = parse_link(row[1])
        if not band_link or not album_link:
            return None

        return {
            "_type": "song",
            "name": normalize_text(row[3]),
            "url": album_link.get("href", ""),
            "id": None,
            "band": normalize_text(band_link.text),
            "album": normalize_text(album_link.text),
            "album_type": normalize_text(row[2]) if len(row) > 2 else None,
        }

    _ADVANCED_ENDPOINT = "/search/ajax-advanced/searching/songs"
    _ADVANCED_ROW_PARSER = "_parse_search_row"
    _ADVANCED_DEFAULTS: ClassVar[dict[str, str]] = {
        "songTitle": "",
        "bandName": "",
        "releaseTitle": "",
        "lyrics": "",
        "genre": "",
    }

    def search(self, query: str, exact_match: bool = True) -> list[dict]:
        """Search songs by name via ``/search/ajax-song-search/``.

        Results are cached internally so that a subsequent ``get()`` call can
        auto-detect the target song name from the album URL. This avoids
        requiring the caller to pass ``song_name`` explicitly.

        Args:
            query: Song name to search for.
            exact_match: If True, only return songs whose name matches exactly.

        Returns:
            List of song dicts with keys: ``_type``, ``name``, ``url`` (album
            URL), ``id`` (None until ``get()`` resolves it), ``band``,
            ``album``, ``album_type``.
        """
        results = self._generic_search(
            "/search/ajax-song-search/", query, self._parse_search_row, exact_match
        )

        for song in results:
            self._last_search_results[song["url"]] = song["name"]

        return results

    def get(self, song_url: str, **kwargs) -> dict | None:
        """Fetch a song's details by parsing its parent album page.

        Since songs don't have dedicated pages, this fetches the album page
        and uses ``SongPageParser`` to locate the specific song in the
        tracklist. The target song is identified either from the ``song_name``
        kwarg or from the internal cache populated by ``search()``.

        Once the song is found, its numeric ID and a fragment URL
        (``album_url#song_id``) are set. Lyrics are fetched by default
        unless ``with_lyrics=False`` is passed.

        Args:
            song_url: Album URL, optionally with a ``#song_id`` fragment.
            **kwargs: Optional ``song_name`` (str) to specify which song to
                extract, and ``with_lyrics`` (bool, default True) to control
                lyrics fetching.

        Returns:
            Song dict with album context, tracklist position, and optionally
            lyrics. None if the page could not be fetched or the song was
            not found in the tracklist.
        """
        album_url = song_url.split("#")[0] if "#" in song_url else song_url
        html = self._client.get(album_url)
        if not html:
            return None

        target_song_name = kwargs.get("song_name")
        if not target_song_name and song_url in self._last_search_results:
            target_song_name = self._last_search_results[song_url]
            logger.debug(f"Auto-detected target song name: {target_song_name}")

        parser = SongPageParser(
            BeautifulSoup(html, "html.parser"),
            song_url,
            target_song_name=target_song_name,
        )

        song = parser.parse()
        if not song:
            return None

        # Update song ID and URL from the matched tracklist entry
        if parser.song_id:
            song["id"] = parser.song_id
            song["url"] = f"{album_url}#{parser.song_id}"

        if song.get("has_lyrics") and kwargs.get("with_lyrics", True):
            lyrics = self.fetch_lyrics(song["id"])
            if lyrics:
                song["lyrics"] = lyrics

        return song
