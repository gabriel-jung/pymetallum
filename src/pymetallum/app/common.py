"""Shared definitions for CLI and Discord frontends.

Entity definitions are UI-specific (Rich markup vs Discord embeds), but
transforms, lazy fetchers, filter helpers, and constants are shared here.
"""

from __future__ import annotations

from ..core.api import STATUS_MAP, AlbumAPI, ArtistAPI, BandAPI, LabelAPI, SongAPI
from ..core.countries import resolve_country

# ─── Entity types ────────────────────────────────────────────────────────────

ENTITY_TYPES = ["band", "album", "artist", "song", "label"]

# ─── Display transforms (plain text; UI layers may wrap with markup) ────────


def band_origin(d: dict) -> str:
    """Format origin as 'Country (Location)' or just 'Country'."""
    country = d.get("country_of_origin") or d.get("country", "")
    location = d.get("location", "")
    return f"{country} ({location})" if country and location else country


def styles_transform(styles) -> str:
    """Join a list of style strings with commas."""
    return ", ".join(styles) if styles else ""


def mode_label(mode: str, entity_type: str, count: int) -> str:
    """Format a menu label like 'New bands (42)' or 'Modified labels (7)'."""
    return f"{'New' if mode == 'created' else 'Modified'} {entity_type}s ({count})"


# ─── Lazy fetchers ───────────────────────────────────────────────────────────

LAZY_FETCHERS = {
    ("band", "description"): lambda api, entity: api.fetch_description(entity["id"]),
    ("band", "similar_artists"): lambda api, entity: api.fetch_similar_artists(entity["id"]),
    ("song", "lyrics"): lambda api, entity: (
        api.fetch_lyrics(entity["song_id"]) if entity.get("song_id") else None
    ),
}

# ─── API factory ─────────────────────────────────────────────────────────────


def make_apis(client):
    """Create the {type: API} mapping used by both CLI and Discord navigators."""
    return {
        "band": BandAPI(client),
        "album": AlbumAPI(client),
        "artist": ArtistAPI(client),
        "song": SongAPI(client),
        "label": LabelAPI(client),
    }


# ─── Advanced search filter helpers ─────────────────────────────────────────

# Which entity types support advanced search and their valid filter keys
VALID_FILTERS: dict[str, set[str]] = {
    "band": {"genre", "country", "themes", "year", "status", "location", "label_name"},
    "album": {"genre", "country", "year", "location", "label_name"},
    "song": {"genre", "lyrics"},
}

# API parameter name for the entity name query
NAME_PARAM = {"band": "bandName", "album": "releaseTitle", "song": "songTitle"}

# API parameter names for year range filters
YEAR_PARAMS = {
    "band": ("yearCreationFrom", "yearCreationTo"),
    "album": ("releaseYearFrom", "releaseYearTo"),
}

# API parameter name when it differs from the flag name
API_PARAM_NAME: dict[tuple[str, str], str] = {
    ("band", "label_name"): "bandLabelName",
    ("album", "label_name"): "releaseLabelName",
}


def parse_year_range(year_str: str) -> tuple[str, str]:
    """Split '1990-1995' into ('1990', '1995'). A single year returns (year, year)."""
    if "-" in year_str:
        parts = year_str.split("-", 1)
        return parts[0].strip(), parts[1].strip()
    year = year_str.strip()
    return year, year


def build_filters(entity_type: str, **kwargs: str | None) -> dict[str, str]:
    """Build API filter dict from keyword arguments.

    Accepts the user-facing filter names (``query``, ``genre``, ``country``,
    ``year``, ``status``, ``themes``, ``location``, ``label``, ``lyrics``)
    and maps them to the Metal Archives API parameter names.

    Returns an empty dict if no filters were provided.
    """
    filters: dict[str, str] = {}

    query = kwargs.get("query")
    if query and entity_type in NAME_PARAM:
        filters[NAME_PARAM[entity_type]] = query

    genre = kwargs.get("genre")
    if genre:
        filters["genre"] = genre

    country = kwargs.get("country")
    if country:
        filters["country"] = resolve_country(country) or country

    year = kwargs.get("year")
    if year and entity_type in YEAR_PARAMS:
        param_from, param_to = YEAR_PARAMS[entity_type]
        f, t = parse_year_range(year)
        filters[param_from] = f
        filters[param_to] = t

    status = kwargs.get("status")
    if status:
        filters["status"] = STATUS_MAP.get(status.lower(), "")

    themes = kwargs.get("themes")
    if themes:
        filters["themes"] = themes

    location = kwargs.get("location")
    if location:
        filters["location"] = location

    label = kwargs.get("label")
    if label:
        filters[API_PARAM_NAME.get((entity_type, "label_name"), "label")] = label

    lyrics = kwargs.get("lyrics")
    if lyrics:
        filters["lyrics"] = lyrics

    return filters
