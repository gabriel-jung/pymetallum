"""Shared HTML/text utility functions used by parsers and the API layer."""

import re
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup, Tag


def extract_id_from_url(url: str) -> str | None:
    """Extract the trailing numeric ID from a Metal Archives URL.

    For example, ``https://www.metal-archives.com/bands/Summoning/29`` → ``"29"``.
    """
    if not url:
        return None
    clean = url.split("#")[0].split("?")[0].strip("/")
    match = re.search(r"/(\d+)$", clean)
    return match.group(1) if match else None


def parse_link(html: str) -> Tag | None:
    """Parse an HTML string and return the first ``<a>`` tag, or None."""
    try:
        return BeautifulSoup(html, "html.parser").find("a")
    except Exception:
        return None


def normalize_text(text: str) -> str:
    """Collapse all whitespace in a string to single spaces."""
    return " ".join(text.split()) if text else ""


def safe_get_text(element: Tag, default: str = "") -> str:
    """Get normalized text content from a BeautifulSoup element, or a default."""
    if not element:
        return default
    return normalize_text(element.get_text())


def safe_get_attr(element: Tag, attr: str, default: str = "") -> str:
    """Get an attribute from a BeautifulSoup element, or a default if missing."""
    return element.get(attr, default) if element else default


def parse_dt_dd_pairs(soup: BeautifulSoup, selector: str) -> dict[str, str]:
    """Parse ``<dt>``/``<dd>`` pairs within a container into a flat dict.

    Keys are lowercased, colon-stripped, and space-to-underscore converted.
    For example, ``<dt>Country of origin:</dt><dd>Austria</dd>`` becomes
    ``{"country_of_origin": "Austria"}``.
    """
    pairs: dict[str, str] = {}
    for dt in soup.select(f"{selector} dt"):
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        key = dt.text.rstrip(":").strip().lower().replace(" ", "_")
        pairs[key] = normalize_text(dd.get_text())
    return pairs


def extract_text_block(element: Tag) -> str | None:
    """Extract clean plain text from an HTML block containing links and ``<br>`` tags.

    Links are unwrapped to plain text, ``<br>`` tags become newlines, each line
    is whitespace-normalized, and excessive blank lines are collapsed. Returns
    None if the result is empty or trivially short.
    """
    import copy as _copy

    block = _copy.copy(element)
    for a in block.find_all("a"):
        a.unwrap()
    for br in block.find_all("br"):
        br.replace_with("\n")

    lines = [normalize_text(line) for line in block.get_text().split("\n")]
    text = "\n".join(line for line in lines if line)

    return text if text and len(text) > 10 else None


def parse_rating(text: str) -> int | None:
    """Parse a percentage rating like ``'66%'`` into an int, or None."""
    digits = text.strip().replace("%", "")
    return int(digits) if digits.isdigit() else None


def extract_cell(cell) -> tuple[str, str]:
    """Extract text and first link href from an AJAX table cell.

    Cells are either plain strings or dicts with ``text`` and optional ``links``.
    Returns ``(text, url)`` where url may be empty.
    """
    if isinstance(cell, str):
        return cell, ""
    text = cell.get("text", "")
    links = cell.get("links", [])
    url = links[0].get("href", "") if links else ""
    return text, url


def create_link_dict(link_element: Tag) -> dict[str, Any] | None:
    """Build a dict with ``name``, ``url``, and ``id`` from an ``<a>`` tag."""
    if not link_element:
        return None
    url = safe_get_attr(link_element, "href")
    return {
        "name": safe_get_text(link_element),
        "url": url,
        "id": extract_id_from_url(url),
    }


def extract_date(item: dict, year: int | None = None) -> date | None:
    """Extract a date from an item's ``added_on`` or ``modified_on`` fields.

    Handles two Metal Archives date formats:
    - ISO prefix: ``"2026-03-15 ..."``
    - Abbreviated: ``"Mar 15th, ..."`` (requires ``year``)
    """
    for key in ("added_on", "modified_on"):
        val = item.get(key)
        if not val:
            continue
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
        m = re.match(r"([A-Z][a-z]{2})\s+(\d+)\w*,", val)
        if m and year:
            try:
                month_num = datetime.strptime(m.group(1), "%b").month
                day = int(m.group(2))
                return date(year, month_num, day)
            except (ValueError, TypeError):
                pass
    return None
