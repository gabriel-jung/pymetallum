"""Discord bot for browsing Metal Archives interactively.

Uses discord-metadata to render entity embeds with select-menu navigation.
Run with the ``metallum-discord`` entry point.
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord_metadata import (
    BaseNavigator,
    DisplayEngine,
    EntityDef,
    HeaderField,
    HeaderLink,
    MetadataBot,
    SectionDef,
    SummaryField,
    SyncAPI,
    TableColumn,
)

from ..core.api import BandAPI, LabelAPI
from ..core.client import MetalArchivesClient
from .common import (
    ENTITY_TYPES,
    LAZY_FETCHERS,
    band_origin,
    build_filters,
    make_apis,
    mode_label,
    styles_transform,
)

# ─── Metal Archives branding ────────────────────────────────────────────────

MA_COLOR = 0xB7410E
MA_FOOTER = "Metal Archives"
MA_FOOTER_ICON = "https://www.metal-archives.com/css/default/images/logo-symbol.png"

# ─── Display transforms ─────────────────────────────────────────────────────


def _album_title(d: dict) -> str:
    return f"{d.get('name', '')} by {d.get('band', 'Unknown')}"


# ─── Entity definitions (Discord-adapted) ───────────────────────────────────

_MEMBER_COLUMNS = [
    TableColumn("Name", "name"),
    TableColumn("Role", "role"),
]

band_def = EntityDef(
    type_name="band",
    summary=[
        SummaryField(key="name", bold=True),
        SummaryField(key="country", fallback="Unknown"),
        SummaryField(key="genre", fallback="Unknown"),
    ],
    header_fields=[
        HeaderField("Status", key="status"),
        HeaderField("Active", key="years_active"),
        HeaderField("Origin", transform=band_origin),
        HeaderField("Genre", key="genre"),
        HeaderField("Label", key="current_label"),
        HeaderField("Themes", key="themes"),
    ],
    thumbnail_url_key="logo_url",
    sections=[
        SectionDef(
            "discography",
            navigable=True,
            columns=[
                TableColumn("Year", "release_date"),
                TableColumn("Title", "name"),
                TableColumn("Type", "album_type"),
            ],
        ),
        SectionDef("members", navigable=True, columns=_MEMBER_COLUMNS),
        SectionDef("description", lazy=True),
        SectionDef(
            "similar_artists",
            navigable=True,
            lazy=True,
            columns=[
                TableColumn("Band", "name"),
                TableColumn("Country", "country"),
                TableColumn("Genre", "genre"),
                TableColumn("Score", "score"),
            ],
        ),
    ],
    header_links=[
        HeaderLink("Label: {current_label}", "label", ref_key="label_url"),
    ],
    color=MA_COLOR,
    footer=MA_FOOTER,
    footer_icon_url=MA_FOOTER_ICON,
    url_key="url",
)

album_def = EntityDef(
    type_name="album",
    summary=[
        SummaryField(key="name", bold=True),
        SummaryField(prefix="by ", key="band", fallback="Unknown"),
        SummaryField(key="release_date", transform=lambda v: f"({v})" if v else ""),
    ],
    header_fields=[
        HeaderField("Type", key="album_type"),
        HeaderField("Release Date", key="release_date"),
        HeaderField("Label", key="label"),
        HeaderField("Catalog ID", key="catalog_id"),
        HeaderField("Format", key="format"),
        HeaderField("Reviews", key="review_summary"),
    ],
    header_title=_album_title,
    image_url_key="cover_url",
    sections=[
        SectionDef(
            "tracklist",
            navigable=True,
            duration_key="duration",
            columns=[
                TableColumn("Title", "name"),
                TableColumn("Duration", "duration"),
            ],
        ),
        SectionDef("lineup", navigable=True, columns=_MEMBER_COLUMNS),
        SectionDef(
            "reviews",
            numbered=False,
            columns=[
                TableColumn("Score", "score"),
                TableColumn("Title", "title"),
                TableColumn("Reviewer", "reviewer"),
            ],
        ),
        SectionDef("additional_notes"),
    ],
    header_links=[
        HeaderLink(
            "Band: {band}",
            "band",
            ref_fn=lambda d: BandAPI.url(d["band_id"][0]) if len(d.get("band_id", [])) == 1 else None,
        ),
        HeaderLink(
            "Label: {label}",
            "label",
            ref_fn=lambda d: LabelAPI.url(d["label_id"][0]) if len(d.get("label_id", [])) == 1 else None,
        ),
    ],
    color=MA_COLOR,
    footer=MA_FOOTER,
    footer_icon_url=MA_FOOTER_ICON,
    url_key="url",
)

artist_def = EntityDef(
    type_name="artist",
    summary=[
        SummaryField(key="name", bold=True),
        SummaryField(
            key="real_name",
            transform=lambda v: f"({v})" if v else "",
            fallback="N/A",
        ),
        SummaryField(key="country"),
    ],
    header_fields=[
        HeaderField("Real Name", key="real_name"),
        HeaderField("Gender", key="gender"),
        HeaderField("Country", key="country"),
        HeaderField("Place of Birth", key="place_of_birth"),
        HeaderField("Birth Date", key="birth_date"),
    ],
    thumbnail_url_key="photo_url",
    sections=[
        SectionDef("biography"),
        SectionDef(
            "bands_overview",
            label="Bands",
            navigable=True,
            group_key="type",
            nav_items=lambda d: [entry["band"] for entry in d.get("bands_overview", [])],
            columns=[
                TableColumn("Band", "band.name"),
                TableColumn("Role", "role"),
                TableColumn("Period", "period"),
            ],
        ),
    ],
    color=MA_COLOR,
    footer=MA_FOOTER,
    footer_icon_url=MA_FOOTER_ICON,
    url_key="url",
)

song_def = EntityDef(
    type_name="song",
    summary=[
        SummaryField(key="name", bold=True),
        SummaryField(prefix="by ", key="band", fallback="Unknown"),
        SummaryField(prefix="from ", key="album", fallback="Unknown"),
    ],
    header_fields=[
        HeaderField("Band", key="band"),
        HeaderField("Album", key="album"),
        HeaderField("Track", key="track_number"),
        HeaderField("Duration", key="duration"),
    ],
    sections=[
        SectionDef("lyrics", lazy=True),
    ],
    color=MA_COLOR,
    footer=MA_FOOTER,
    footer_icon_url=MA_FOOTER_ICON,
    url_key="url",
)

_ROSTER_COLUMNS = [
    TableColumn("Band", "name"),
    TableColumn("Country", "country"),
    TableColumn("Genre", "genre"),
]

label_def = EntityDef(
    type_name="label",
    summary=[
        SummaryField(key="name", bold=True),
        SummaryField(key="country"),
    ],
    header_fields=[
        HeaderField("Address", key="address"),
        HeaderField("Country", key="country"),
        HeaderField("Phone", key="phone"),
        HeaderField("Status", key="status"),
        HeaderField("Founded", key="founding_date"),
        HeaderField("Styles", key="styles", transform=styles_transform),
        HeaderField("Website", key="website"),
    ],
    thumbnail_url_key="logo_url",
    sections=[
        SectionDef("current_roster", navigable=True, columns=_ROSTER_COLUMNS),
        SectionDef("past_roster", navigable=True, columns=_ROSTER_COLUMNS),
        SectionDef(
            "releases",
            navigable=True,
            columns=[
                TableColumn("Year", "release_date"),
                TableColumn("Band", "band"),
                TableColumn("Album", "name"),
                TableColumn("Type", "album_type"),
            ],
        ),
        SectionDef("notes"),
    ],
    color=MA_COLOR,
    footer=MA_FOOTER,
    footer_icon_url=MA_FOOTER_ICON,
    url_key="url",
)

# ─── Engine & bot setup ─────────────────────────────────────────────────────

engine = DisplayEngine()
engine.register(band_def, album_def, artist_def, song_def, label_def)


def _build_bot() -> MetadataBot:
    client = MetalArchivesClient()
    apis = {k: SyncAPI(v) for k, v in make_apis(client).items()}
    navigator = BaseNavigator(
        engine,
        apis=apis,
        lazy_fetchers=LAZY_FETCHERS,
        ephemeral=False,
        placeholder="Browse sections & navigate\u2026",
        search_kwargs={"exact_match": False},
    )
    return MetadataBot(navigator, on_close=client.close)


bot: MetadataBot  # Initialized in main(); referenced by slash command handlers below.

# ─── Advanced search helper ─────────────────────────────────────────────────

_STATUS_CHOICES = [
    app_commands.Choice(name="Active", value="active"),
    app_commands.Choice(name="Split-up", value="split-up"),
    app_commands.Choice(name="On Hold", value="on hold"),
    app_commands.Choice(name="Changed Name", value="changed name"),
    app_commands.Choice(name="Unknown", value="unknown"),
]


async def _advanced_search_and_navigate(
    interaction: discord.Interaction,
    entity_type: str,
    filters: dict[str, str],
) -> None:
    """Run advanced search in a thread and navigate the results."""
    await interaction.response.defer()

    raw_api = bot.navigator.apis[entity_type]._sync
    results, _total = await asyncio.to_thread(
        raw_api.advanced_search, start=0, count=25, **filters
    )

    filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items())
    await bot.navigator.navigate_results(interaction, results, title=filter_desc)


def _has_filters(*values) -> bool:
    return any(v is not None for v in values)


# ─── Slash commands (grouped under /metallum) ───────────────────────────────

metallum = app_commands.Group(name="metallum", description="Browse Metal Archives")


@metallum.command(name="band", description="Search for a band")
@app_commands.describe(
    query="Band name",
    genre="Filter by genre (e.g. 'black metal')",
    country="Filter by country (name or ISO code)",
    year="Filter by year or range (e.g. '1995' or '1990-1995')",
    status="Filter by band status",
    themes="Filter by lyrical themes",
    location="Filter by location (e.g. 'Bay Area')",
    label="Filter by label name",
)
@app_commands.choices(status=_STATUS_CHOICES)
async def cmd_band(
    interaction: discord.Interaction,
    query: str,
    genre: str | None = None,
    country: str | None = None,
    year: str | None = None,
    status: app_commands.Choice[str] | None = None,
    themes: str | None = None,
    location: str | None = None,
    label: str | None = None,
):
    if _has_filters(genre, country, year, status, themes, location, label):
        status_val = status.value if status else None
        filters = build_filters(
            "band", query=query, genre=genre, country=country, year=year,
            status=status_val, themes=themes, location=location, label=label,
        )
        await _advanced_search_and_navigate(interaction, "band", filters)
    else:
        await bot.navigator.search_and_navigate(interaction, query, ["band"])


@metallum.command(name="album", description="Search for an album")
@app_commands.describe(
    query="Album title",
    genre="Filter by genre",
    country="Filter by country (name or ISO code)",
    year="Filter by year or range (e.g. '1995' or '1990-1995')",
    location="Filter by location",
    label="Filter by label name",
)
async def cmd_album(
    interaction: discord.Interaction,
    query: str,
    genre: str | None = None,
    country: str | None = None,
    year: str | None = None,
    location: str | None = None,
    label: str | None = None,
):
    if _has_filters(genre, country, year, location, label):
        filters = build_filters(
            "album", query=query, genre=genre, country=country,
            year=year, location=location, label=label,
        )
        await _advanced_search_and_navigate(interaction, "album", filters)
    else:
        await bot.navigator.search_and_navigate(interaction, query, ["album"])


@metallum.command(name="artist", description="Search for an artist")
@app_commands.describe(query="Artist name")
async def cmd_artist(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ["artist"])


@metallum.command(name="song", description="Search for a song")
@app_commands.describe(
    query="Song title",
    genre="Filter by genre",
    lyrics="Search in lyrics",
)
async def cmd_song(
    interaction: discord.Interaction,
    query: str,
    genre: str | None = None,
    lyrics: str | None = None,
):
    if _has_filters(genre, lyrics):
        filters = build_filters("song", query=query, genre=genre, lyrics=lyrics)
        await _advanced_search_and_navigate(interaction, "song", filters)
    else:
        await bot.navigator.search_and_navigate(interaction, query, ["song"])


@metallum.command(name="label", description="Search for a label")
@app_commands.describe(query="Label name")
async def cmd_label(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ["label"])


@metallum.command(name="search", description="Search all categories")
@app_commands.describe(query="Search query")
async def cmd_search(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ENTITY_TYPES)


@metallum.command(name="random", description="Show a random band")
async def cmd_random(interaction: discord.Interaction):
    await interaction.response.defer()
    raw_api = bot.navigator.apis["band"]._sync
    entity = await asyncio.to_thread(raw_api.get_random)
    if not entity:
        await interaction.followup.send("Could not fetch a random band.")
        return
    await bot.navigator.navigate(interaction, entity)


_RECENT_TYPE_CHOICES = [
    app_commands.Choice(name="Bands", value="band"),
    app_commands.Choice(name="Labels", value="label"),
]

_RECENT_MODE_CHOICES = [
    app_commands.Choice(name="New & Modified", value="both"),
    app_commands.Choice(name="New only", value="created"),
    app_commands.Choice(name="Modified only", value="modified"),
]


@metallum.command(name="recent", description="Recently added/modified bands or labels")
@app_commands.describe(
    type="Bands or labels (default: bands)",
    mode="New, modified, or both (default: both)",
    month="Month in YYYY-MM format (default: current month)",
)
@app_commands.choices(type=_RECENT_TYPE_CHOICES, mode=_RECENT_MODE_CHOICES)
async def cmd_recent(
    interaction: discord.Interaction,
    type: app_commands.Choice[str] | None = None,
    mode: app_commands.Choice[str] | None = None,
    month: str | None = None,
):
    await interaction.response.defer()
    entity_type = type.value if type else "band"
    mode_val = mode.value if mode else "both"
    raw_api = bot.navigator.apis[entity_type]._sync

    modes = (
        ["created"] if mode_val == "created"
        else ["modified"] if mode_val == "modified"
        else ["created", "modified"]
    )

    totals = {}
    for m in modes:
        _, totals[m] = await asyncio.to_thread(raw_api.fetch_recent_page, m, month)

    sources = [
        (
            mode_label(m, entity_type, totals[m]),
            lambda s, c, m=m: raw_api.fetch_recent_page(m, month, s, c),
        )
        for m in modes
    ]

    await bot.navigator.browse_sources(interaction, sources)


@metallum.command(name="upcoming", description="Upcoming album releases")
async def cmd_upcoming(interaction: discord.Interaction):
    await interaction.response.defer()
    raw_api = bot.navigator.apis["album"]._sync

    await bot.navigator.browse(
        interaction,
        lambda s, c: raw_api.fetch_upcoming_page(s, c),
        title="Upcoming releases",
    )


# ─── Entry point ─────────────────────────────────────────────────────────────


def main():
    """Run the Metal Archives Discord bot."""
    global bot
    bot = _build_bot()
    bot.tree.add_command(metallum)
    bot.run_with_args("DISCORD_TOKEN")


if __name__ == "__main__":
    main()
