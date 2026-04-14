"""CLI entry point for metallum — an interactive Metal Archives browser.

Run ``metallum --help`` for usage examples.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from importlib.metadata import version

__version__ = version("pymetallum")

from rich_metadata import (
    BaseNavigator,
    DisplayEngine,
    EntityDef,
    HeaderField,
    HeaderLink,
    QuitSignal,
    SectionDef,
    SummaryField,
    TableColumn,
    configure_logging,
    list_fetcher,
    months_in_range,
    parse_date_args,
    resolve_entity_type,
)

from ..core.api import STATUS_MAP, BandAPI, LabelAPI
from ..core.client import MetalArchivesClient
from ..core.countries import resolve_country
from .common import (
    ENTITY_TYPES,
    LAZY_FETCHERS,
    NAME_PARAM,
    VALID_FILTERS,
    band_origin,
    build_filters,
    make_apis,
    mode_label,
    styles_transform,
)

# ─── Display transforms (CLI-specific Rich markup) ──────────────────────────

STATUS_COLORS = {
    "Active": "green",
    "On hold": "yellow",
    "Split-up": "red",
    "Changed name": "cyan",
    "Unknown": "dim",
}


def _status_transform(status: str) -> str:
    """Color-code band status (e.g. Active → green, Split-up → red)."""
    color = STATUS_COLORS.get(status, "")
    return f"[{color}]{status}[/{color}]" if status else ""


def _album_title(d: dict) -> str:
    """Format album header title as 'Name by Band'."""
    return f"[bold]{d.get('name', '')}[/bold] [dim]by[/dim] {d.get('band', 'Unknown')}"


def _label_footer_sub_labels(d: dict) -> str | None:
    """Format sub-labels as a comma-separated line, or None if empty."""
    subs = d.get("sub_labels")
    if not subs:
        return None
    names = ", ".join(sub["name"] for sub in subs)
    return f"[bold]Sub-labels:[/bold] {names}"


_MEMBER_COLUMNS = [
    TableColumn("Name", "name", style="bold"),
    TableColumn("Role", "role"),
]

# ─── Entity definitions ──────────────────────────────────────────────────────

band_def = EntityDef(
    type_name="band",
    summary=[
        SummaryField(key="name", style="bold"),
        SummaryField(key="country", style="dim", fallback="Unknown"),
        SummaryField(key="genre", fallback="Unknown"),
    ],
    header_fields=[
        HeaderField("Status", key="status", transform=_status_transform),
        HeaderField("Active", key="years_active"),
        HeaderField("Origin", transform=band_origin),
        HeaderField("Genre", key="genre"),
        HeaderField("Label", key="current_label"),
        HeaderField("Themes", key="themes"),
    ],
    header_image_key="_logo_data",
    sections=[
        SectionDef(
            "discography", navigable=True,
            columns=[
                TableColumn("Year", "release_date", style="dim", width=6),
                TableColumn("Title", "name", style="bold"),
                TableColumn("Type", "album_type"),
                TableColumn("Reviews", "review_summary", justify="right"),
            ],
        ),
        SectionDef("members", navigable=True, columns=_MEMBER_COLUMNS),
        SectionDef("description", lazy=True),
        SectionDef(
            "similar_artists", navigable=True, lazy=True,
            columns=[
                TableColumn("Band", "name", style="bold"),
                TableColumn("Country", "country"),
                TableColumn("Genre", "genre"),
                TableColumn("Score", "score", justify="right"),
            ],
        ),
    ],
    header_links=[
        HeaderLink("Label: {current_label}", "label", ref_key="label_url"),
    ],
    footer=["url", lambda d: d.get("logo_url") or d.get("photo_url")],
)

album_def = EntityDef(
    type_name="album",
    summary=[
        SummaryField(key="name", style="bold"),
        SummaryField(prefix="by ", key="band", fallback="Unknown"),
        SummaryField(key="release_date", style="dim", transform=lambda v: f"({v})" if v else ""),
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
    header_image_key="_cover_data",
    sections=[
        SectionDef(
            "tracklist", navigable=True, duration_key="duration",
            columns=[
                TableColumn("Title", "name", style="bold"),
                TableColumn("Duration", "duration", justify="right"),
                TableColumn(
                    "Lyrics", "has_lyrics", style="dim", justify="center",
                    transform=lambda v: "Yes" if v else "",
                ),
            ],
        ),
        SectionDef("lineup", navigable=True, columns=_MEMBER_COLUMNS),
        SectionDef(
            "reviews", numbered=False,
            columns=[
                TableColumn("Score", "score", justify="right", style="bold"),
                TableColumn("Title", "title"),
                TableColumn("Reviewer", "reviewer", style="dim"),
                TableColumn("Date", "date", style="dim"),
            ],
        ),
        SectionDef("additional_notes"),
    ],
    header_links=[
        HeaderLink(
            "Band: {band}", "band",
            ref_fn=lambda d: BandAPI.url(d["band_id"][0])
            if len(d.get("band_id", [])) == 1
            else None,
        ),
        HeaderLink(
            "Label: {label}", "label",
            ref_fn=lambda d: LabelAPI.url(d["label_id"][0])
            if len(d.get("label_id", [])) == 1
            else None,
        ),
    ],
    footer=["url", "cover_url"],
)

artist_def = EntityDef(
    type_name="artist",
    summary=[
        SummaryField(key="name", style="bold"),
        SummaryField(
            key="real_name", style="dim",
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
    header_image_key="_photo_data",
    sections=[
        SectionDef("biography"),
        SectionDef(
            "bands_overview", label="Bands", navigable=True, group_key="type",
            nav_items=lambda d: [entry["band"] for entry in d.get("bands_overview", [])],
            columns=[
                TableColumn("Band", "band.name", style="bold"),
                TableColumn("Role", "role"),
                TableColumn("Period", "period", style="dim"),
            ],
        ),
    ],
    footer=["url", "photo_url"],
)

song_def = EntityDef(
    type_name="song",
    summary=[
        SummaryField(key="name", style="bold"),
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
    footer=["url"],
    auto_full=True,
)

_ROSTER_COLUMNS = [
    TableColumn("Band", "name", style="bold"),
    TableColumn("Country", "country"),
    TableColumn("Genre", "genre"),
]

label_def = EntityDef(
    type_name="label",
    summary=[
        SummaryField(key="name", style="bold"),
        SummaryField(key="country", style="dim"),
    ],
    header_fields=[
        HeaderField("Address", key="address"),
        HeaderField("Country", key="country"),
        HeaderField("Phone", key="phone"),
        HeaderField("Status", key="status"),
        HeaderField("Founded", key="founding_date"),
        HeaderField("Styles", key="styles", transform=styles_transform),
        HeaderField("Website", key="website"),
        HeaderField("Email", key="email"),
        HeaderField(
            "Online Shopping",
            transform=lambda d: "Yes" if d.get("online_shopping") else "",
        ),
    ],
    sections=[
        SectionDef("current_roster", navigable=True, columns=_ROSTER_COLUMNS),
        SectionDef("past_roster", navigable=True, columns=_ROSTER_COLUMNS),
        SectionDef(
            "releases", navigable=True,
            columns=[
                TableColumn("Year", "release_date", style="dim", width=6),
                TableColumn("Band", "band"),
                TableColumn("Album", "name", style="bold"),
                TableColumn("Type", "album_type"),
            ],
        ),
        SectionDef("notes"),
    ],
    footer=[_label_footer_sub_labels, "url", "logo_url"],
)

# ─── Engine & navigator setup ────────────────────────────────────────────────

engine = DisplayEngine()
engine.register(band_def, album_def, artist_def, song_def, label_def)
console = engine.console

_ALL_FILTER_FLAGS = sorted(set().union(*VALID_FILTERS.values()))


# ─── Parser ───────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all flags and entity types."""
    parser = argparse.ArgumentParser(
        description="An interactive CLI for Metal Archives.",
        epilog=(
            "examples:\n"
            "  metallum Summoning              search all categories\n"
            "  metallum --band Summoning       search bands only\n"
            '  metallum --album "Minas Morgul" search albums only\n'
            '  metallum --genre "death doom"   search bands by genre\n'
            "  metallum --album --genre doom --year 1990-1995\n"
            '  metallum --song --lyrics "ring of power"\n'
            "  metallum --band --country NO --status active\n"
            "  metallum --band Summoning -s    show all matches (fuzzy)\n"
            "  metallum --band Summoning --full     all sections at once\n"
            "  metallum --band Summoning --json     output as JSON\n"
            "  metallum --recent                    recently added/modified bands\n"
            "  metallum --new                       only newly created bands\n"
            "  metallum --modified                  only recently modified bands\n"
            "  metallum --label --new               newly created labels\n"
            "  metallum --recent --month 2026-02    specific month\n"
            "  metallum --new --from 2026-03-20     new bands since a date\n"
            "  metallum --upcoming                  upcoming album releases\n"
            "  metallum --upcoming --from 2026-04-01 --to 2026-06-01"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("query", nargs="?", type=str, help="Search all categories")

    entity_group = parser.add_mutually_exclusive_group()
    for entity in ENTITY_TYPES:
        entity_group.add_argument(
            f"--{entity}", nargs="?", const=True, default=None,
            metavar="NAME", help=f"Search {entity}s (optionally by name)",
        )

    parser.add_argument("--genre", type=str, help="Filter by genre")
    parser.add_argument("--country", type=str, help="Filter by country (ISO code)")
    parser.add_argument("--themes", type=str, help="Filter bands by lyrical themes")
    parser.add_argument(
        "--year", type=str, help="Filter by year or range (e.g. 1995 or 1990-1995)",
    )
    parser.add_argument(
        "--status", type=str,
        help="Filter bands by status (active, split-up, on hold, changed name, unknown)",
    )
    parser.add_argument("--location", type=str, help="Filter by location")
    parser.add_argument("--label-name", type=str, help="Filter by label name")
    parser.add_argument("--lyrics", type=str, help="Search songs by lyrics content")
    parser.add_argument("--random", action="store_true", help="Open a random band page")
    parser.add_argument(
        "--recent", action="store_true",
        help="Show recently added/modified entries (default: bands, or --label)",
    )
    parser.add_argument(
        "--new", action="store_true",
        help="Show only newly created entries (implies --recent)",
    )
    parser.add_argument(
        "--modified", action="store_true",
        help="Show only recently modified entries (implies --recent)",
    )
    parser.add_argument("--upcoming", action="store_true", help="Show upcoming releases")
    parser.add_argument(
        "--month", type=str,
        help="Month for --recent (YYYY-MM format, default: current month)",
    )
    parser.add_argument(
        "--from", dest="from_date", type=str,
        help="Start date (YYYY-MM-DD) for --recent or --upcoming",
    )
    parser.add_argument(
        "--to", dest="to_date", type=str,
        help="End date (YYYY-MM-DD) for --recent or --upcoming",
    )
    parser.add_argument(
        "-s", "--search", action="store_true",
        help="Show all matches instead of only exact ones",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Show all sections at once (no interactive menu)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logs")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    return parser


# ─── Filter helpers ───────────────────────────────────────────────────────────


def _build_advanced_filters(args, entity_type: str) -> dict | None:
    """Validate CLI flags and build Metal Archives API filter parameters.

    Returns None and prints errors on invalid input.
    """
    if entity_type not in VALID_FILTERS:
        console.print(
            f"[red]Advanced search not available for {entity_type}s. "
            f"Use --band, --album, or --song.[/red]"
        )
        return None

    valid = VALID_FILTERS[entity_type]
    errors = [
        f"--{flag.replace('_', '-')} not supported for {entity_type} search"
        for flag in _ALL_FILTER_FLAGS
        if getattr(args, flag, None) and flag not in valid
    ]
    if errors:
        for err in errors:
            console.print(f"[red]{err}[/red]")
        return None

    # CLI-specific validation before delegating to shared builder
    country = getattr(args, "country", None)
    if country and not resolve_country(country):
        console.print(
            f"[red]Unknown country '{country}'. Use an ISO code "
            f"(e.g. FR, NO, US) or a full country name.\n"
            f"For regions/cities, use --location instead.[/red]"
        )
        return None

    status = getattr(args, "status", None)
    if status and not STATUS_MAP.get(status.lower()):
        console.print(
            f"[red]Unknown status '{status}'. "
            f"Valid: {', '.join(dict.fromkeys(STATUS_MAP))}[/red]"
        )
        return None

    return build_filters(
        entity_type,
        genre=getattr(args, "genre", None),
        country=country,
        year=getattr(args, "year", None),
        status=status,
        themes=getattr(args, "themes", None),
        location=getattr(args, "location", None),
        label=getattr(args, "label_name", None),
        lyrics=getattr(args, "lyrics", None),
    )


# ─── Search/browse commands ───────────────────────────────────────────────────


def _run_search(navigator, query, entity_type, args):
    """Search by name, select a result, then display or navigate."""
    search_types = [entity_type] if entity_type else ENTITY_TYPES
    navigator.search_and_navigate(
        query, search_types,
        exact_first=not args.search,
        json_output=args.json,
        full=args.full,
    )


def _run_advanced_search(navigator, entity_type, filters, args):
    """Run a filtered search and browse results with pagination."""
    api = navigator.apis[entity_type]
    filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items())

    if args.json:
        with console.status(f"Searching {entity_type}s ({filter_desc})..."):
            results, _ = api.advanced_search(start=0, count=200, **filters)
        if not results:
            console.print(f"[yellow]No {entity_type}s found matching your criteria.[/yellow]")
            return
        print(json.dumps(results, indent=2))
        return

    navigator.browse(
        fetch_page=lambda s, c: api.advanced_search(start=s, count=c, **filters),
        title=f"{entity_type.capitalize()}s ({filter_desc})",
        full=args.full,
    )


def _run_listing(navigator, entity_type, args):
    """Handle --recent/--new/--modified/--upcoming listing commands."""
    api = navigator.apis[entity_type]

    if args.upcoming:
        if args.json:
            with console.status("Fetching upcoming releases..."):
                releases = api.fetch_upcoming(from_date=args.from_date, to_date=args.to_date)
            if not releases:
                console.print("[yellow]No upcoming releases found.[/yellow]")
                return
            print(json.dumps(releases, indent=2))
            return
        navigator.browse(
            fetch_page=lambda s, c: api.fetch_upcoming_page(
            s, c, from_date=args.from_date, to_date=args.to_date,
        ),
            title="Upcoming releases",
            full=args.full,
        )
        return

    # --recent / --new / --modified
    parsed = parse_date_args(args, console)
    if parsed is None:
        return
    from_date, to_date = parsed

    modes = (
        ["created"] if args.new and not args.modified
        else ["modified"] if args.modified and not args.new
        else ["created", "modified"]
    )

    if (from_date or to_date) and args.month:
        console.print("[red]Cannot use --month with --from/--to.[/red]")
        return

    if from_date or to_date:
        from_date = from_date or date.today().replace(day=1)
        to_date = to_date or date.today()
        months = months_in_range(from_date, to_date)

        fetched = {}
        with console.status(f"Fetching {entity_type}s ({from_date} to {to_date})..."):
            for mode in modes:
                fetched[mode] = api.fetch_recent_filtered(mode, months, from_date, to_date)

        if args.json:
            print(json.dumps(fetched, indent=2))
            return

        browsers = [
            (mode_label(mode, entity_type, len(fetched[mode])), list_fetcher(fetched[mode]))
            for mode in modes
        ]
    else:
        month = args.month

        if args.json:
            fetched = {}
            with console.status("Fetching all results..."):
                for mode in modes:
                    fetched[mode] = (
                        api.fetch_recently_created(month) if mode == "created"
                        else api.fetch_recently_modified(month)
                    )
            print(json.dumps(fetched, indent=2))
            return

        totals = {}
        with console.status(f"Fetching recent {entity_type}s ({month or 'this month'})..."):
            for mode in modes:
                _, totals[mode] = api.fetch_recent_page(mode, month)

        browsers = [
            (mode_label(mode, entity_type, totals[mode]),
             lambda s, c, mode=mode: api.fetch_recent_page(mode, month, s, c))
            for mode in modes
        ]

    navigator.browse_sources(browsers, full=args.full)


# ─── Main ─────────────────────────────────────────────────────────────────────


def _make_navigator(client: MetalArchivesClient) -> BaseNavigator:
    """Create a navigator wired to all Metal Archives APIs."""
    return BaseNavigator(engine, apis=make_apis(client), lazy_fetchers=LAZY_FETCHERS)


def main():
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    configure_logging(args.verbose)
    entity_type, name_query = resolve_entity_type(args, ENTITY_TYPES)

    has_filters = any(getattr(args, f, None) for f in _ALL_FILTER_FLAGS)

    with MetalArchivesClient() as client:
        navigator = _make_navigator(client)

        try:
            if args.random:
                with console.status("Fetching random band..."):
                    band = navigator.apis["band"].get_random(
                        full=args.full or args.json,
                    )
                if not band:
                    console.print("[red]Could not fetch random band.[/red]")
                    return
                navigator.display_or_navigate(
                    band, json_output=args.json, full=args.full,
                )
                return

            elif args.upcoming:
                _run_listing(navigator, "album", args)
                return

            elif args.recent or args.new or args.modified:
                listing_type = entity_type or "band"
                if listing_type not in ("band", "label"):
                    console.print(
                        "[red]--recent is only available for bands and labels. "
                        "Use --band --recent or --label --recent.[/red]"
                    )
                    return
                _run_listing(navigator, listing_type, args)
                return

            elif has_filters:
                search_type = entity_type or "band"

                filters = _build_advanced_filters(args, search_type)
                if filters is None:
                    return

                if name_query and search_type in NAME_PARAM:
                    filters[NAME_PARAM[search_type]] = name_query

                if not filters:
                    parser.error("No valid filters provided.")

                _run_advanced_search(navigator, search_type, filters, args)

            elif entity_type and name_query:
                _run_search(navigator, name_query, entity_type, args)
            elif args.query:
                _run_search(navigator, args.query, None, args)
            else:
                parser.error(
                    "Provide a search query or use filters.\n"
                    "  metallum Summoning\n"
                    "  metallum --band Summoning\n"
                    '  metallum --genre "black metal"\n'
                    "  metallum --album --genre doom --year 1990-1995"
                )
        except (QuitSignal, KeyboardInterrupt):
            pass


