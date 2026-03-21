"""CLI entry point for metallum — an interactive Metal Archives browser.

Usage::

    metallum Summoning              # search all categories
    metallum --band Summoning       # search bands only
    metallum --album "Minas Morgul" # search albums only
    metallum --genre "death doom"   # search bands by genre (default)
    metallum --album --genre "doom" --year 1990-1995
    metallum --song --lyrics "ring of power"
    metallum --band --country NO --genre thrash --status active
    metallum --band Summoning -s    # show all matches (fuzzy)
    metallum --band Summoning --full     # show all sections at once
    metallum --band Summoning --json     # output as JSON
"""

import argparse
import json
import sys

from loguru import logger

from ..core.client import MetalArchivesClient
from .countries import resolve_country
from .display import SUMMARY, console, display_details, select_from_list
from .navigator import Navigator, _QuitSignal

ENTITY_TYPES = ["band", "album", "artist", "song", "label"]
ADVANCED_TYPES = ["band", "album", "song"]

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

FILTER_FLAGS = [
    "genre",
    "country",
    "themes",
    "year",
    "status",
    "location",
    "label_name",
    "lyrics",
]


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
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
            "  metallum --band Summoning --json     output as JSON"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("query", nargs="?", type=str, help="Search all categories")

    # Entity type selectors — mutually exclusive with each other
    entity_group = parser.add_mutually_exclusive_group()
    for entity in ENTITY_TYPES:
        entity_group.add_argument(
            f"--{entity}",
            nargs="?",
            const=True,
            default=None,
            metavar="NAME",
            help=f"Search {entity}s (optionally by name)",
        )

    # Filter flags — combinable with each other and with entity types
    parser.add_argument("--genre", type=str, help="Filter by genre")
    parser.add_argument("--country", type=str, help="Filter by country (ISO code)")
    parser.add_argument("--themes", type=str, help="Filter bands by lyrical themes")
    parser.add_argument(
        "--year", type=str, help="Filter by year or range (e.g. 1995 or 1990-1995)"
    )
    parser.add_argument(
        "--status",
        type=str,
        help="Filter bands by status (active, split-up, on hold, changed name, unknown)",
    )
    parser.add_argument("--location", type=str, help="Filter by location")
    parser.add_argument("--label-name", type=str, help="Filter by label name")
    parser.add_argument("--lyrics", type=str, help="Search songs by lyrics content")

    parser.add_argument(
        "-s",
        "--search",
        action="store_true",
        help="Show all matches instead of only exact ones",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show all sections at once (no interactive menu)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logs")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

    return parser


def _parse_year_range(year_str: str) -> tuple[str, str]:
    """Parse a year or year range string into (from, to) tuple."""
    if "-" in year_str:
        parts = year_str.split("-", 1)
        return parts[0].strip(), parts[1].strip()
    year = year_str.strip()
    return year, year


# Maps (entity_type, cli_flag) → API parameter name.
# Only entries present here are valid for that entity type.
_FILTER_PARAM_MAP: dict[tuple[str, str], str] = {
    ("band", "genre"): "genre",
    ("band", "country"): "country",
    ("band", "themes"): "themes",
    ("band", "year"): "year",
    ("band", "status"): "status",
    ("band", "location"): "location",
    ("band", "label_name"): "bandLabelName",
    ("album", "genre"): "genre",
    ("album", "country"): "country",
    ("album", "year"): "year",
    ("album", "location"): "location",
    ("album", "label_name"): "releaseLabelName",
    ("song", "genre"): "genre",
    ("song", "lyrics"): "lyrics",
}

# Year filter maps to different API params per entity type.
_YEAR_PARAMS = {
    "band": ("yearCreationFrom", "yearCreationTo"),
    "album": ("releaseYearFrom", "releaseYearTo"),
}


def _build_advanced_filters(args, entity_type: str) -> dict | None:
    """Map CLI filter flags to API parameters for the given entity type.

    Returns the filters dict, or None if any filter value is invalid
    (e.g. unknown country or status).
    """
    filters = {}

    for flag in FILTER_FLAGS:
        value = getattr(args, flag, None)
        if not value:
            continue

        if (entity_type, flag) not in _FILTER_PARAM_MAP:
            continue  # validation handles the error message

        if flag == "country":
            code = resolve_country(value)
            if not code:
                console.print(
                    f"[red]Unknown country '{value}'. Use an ISO code "
                    f"(e.g. FR, NO, US) or a full country name.\n"
                    f"For regions/cities, use --location instead.[/red]"
                )
                return None
            filters["country"] = code
        elif flag == "year":
            y_from, y_to = _parse_year_range(value)
            param_from, param_to = _YEAR_PARAMS[entity_type]
            filters[param_from] = y_from
            filters[param_to] = y_to
        elif flag == "status":
            code = STATUS_MAP.get(value.lower())
            if not code:
                console.print(
                    f"[red]Unknown status '{value}'. "
                    f"Valid: {', '.join(dict.fromkeys(STATUS_MAP))}[/red]"
                )
                return None
            filters["status"] = code
        else:
            filters[_FILTER_PARAM_MAP[(entity_type, flag)]] = value

    return filters


def _validate_filters(args, entity_type: str) -> list[str]:
    """Check that the given filters are valid for the entity type."""
    if entity_type not in ADVANCED_TYPES:
        return [
            f"Advanced search not available for {entity_type}s. "
            f"Use --band, --album, or --song."
        ]

    errors = []
    for flag in FILTER_FLAGS:
        if getattr(args, flag, None) and (entity_type, flag) not in _FILTER_PARAM_MAP:
            errors.append(
                f"--{flag.replace('_', '-')} not supported for {entity_type} search"
            )
    return errors


def _has_filters(args) -> bool:
    """Check if any filter flags are set."""
    return any(getattr(args, f, None) for f in FILTER_FLAGS)


def _strip_bytes(obj):
    """Recursively strip bytes values from dicts for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _strip_bytes(v) for k, v in obj.items() if not isinstance(v, bytes)}
    if isinstance(obj, list):
        return [_strip_bytes(item) for item in obj]
    return obj


def _run_search(navigator, query, entity_type, args):
    """Run a search, select, and display cycle."""
    search_types = [entity_type] if entity_type else ENTITY_TYPES

    all_results = []
    with console.status(f"Searching for [bold]{query}[/bold]..."):
        for t in search_types:
            results = navigator.apis[t].search(query, exact_match=False)
            all_results.extend(results)

    if args.search:
        search_results = all_results
    else:
        search_results = [r for r in all_results if r["name"].lower() == query.lower()]
        if not search_results:
            search_results = all_results

    if not search_results:
        console.print("[yellow]No items found matching your criteria.[/yellow]")
        return

    selected_item = select_from_list(search_results)
    if not selected_item:
        return

    selected_type = selected_item["_type"]
    selected_api = navigator.apis[selected_type]

    get_kwargs = (
        {"full": True} if selected_type == "band" and (args.full or args.json) else {}
    )
    with console.status("Fetching details..."):
        entity = selected_api.get(selected_item["url"], **get_kwargs)

    if not entity:
        console.print("[red]Could not retrieve detailed information.[/red]")
        return

    if args.json:
        print(json.dumps(_strip_bytes(entity), indent=2))
        return

    if args.full:
        display_details(entity)
        return

    navigator.navigate(entity)


def _run_advanced_search(navigator, entity_type, filters, args):
    """Run an advanced search with server-side pagination."""
    api = navigator.apis[entity_type]
    display_summary = SUMMARY[entity_type]
    page_size = 200
    start = 0

    filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items())
    with console.status(f"Searching {entity_type}s ({filter_desc})..."):
        results, total = api.advanced_search(start=0, count=page_size, **filters)

    if not results:
        console.print(
            f"[yellow]No {entity_type}s found matching your criteria.[/yellow]"
        )
        return

    if args.json:
        print(json.dumps(results, indent=2))
        return

    while True:
        end = min(start + len(results), total)
        console.print()
        for i, item in enumerate(results, start + 1):
            console.print(f"  [bold cyan]\\[{i}][/bold cyan] ", end="")
            display_summary(item)

        console.print(
            f"\n[dim]Showing {start + 1}-{end} of {total} {entity_type}s[/dim]"
        )
        hints = []
        if start > 0:
            hints.append("[bold]p[/bold]rev")
        if end < total:
            hints.append("[bold]n[/bold]ext")
        hints.append("number to select")
        hints.append("0 to cancel")
        console.print(f"[dim]{' | '.join(hints)}[/dim]")

        try:
            raw = console.input("[bold]>[/bold] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return

        if raw == "n" and end < total:
            start += page_size
            with console.status("Loading next page..."):
                results, total = api.advanced_search(
                    start=start, count=page_size, **filters
                )
            if not results:
                console.print("[dim]No more results.[/dim]")
                return
            continue

        if raw == "p" and start > 0:
            start = max(0, start - page_size)
            with console.status("Loading previous page..."):
                results, total = api.advanced_search(
                    start=start, count=page_size, **filters
                )
            continue

        try:
            choice = int(raw)
        except ValueError:
            continue

        if choice == 0:
            return

        idx = choice - 1 - start
        if 0 <= idx < len(results):
            selected_item = results[idx]
        else:
            console.print("[red]Invalid choice.[/red]")
            continue

        # Determine which API to use for fetching details
        selected_type = selected_item.get("_type", entity_type)
        selected_api = navigator.apis[selected_type]

        get_kwargs = {"full": True} if args.full else {}
        with console.status("Fetching details..."):
            entity = selected_api.get(selected_item["url"], **get_kwargs)

        if not entity:
            console.print("[red]Could not retrieve detailed information.[/red]")
            continue

        if args.full:
            display_details(entity)
            return

        navigator.navigate(entity)
        return


def main():
    """Parse arguments, configure logging, and run the interactive session."""
    parser = _build_parser()
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="DEBUG" if args.verbose else "WARNING",
    )

    # Find which entity type was requested (--band, --album, etc.)
    entity_type = None
    name_query = None
    for t in ENTITY_TYPES:
        value = getattr(args, t, None)
        if value is not None:
            entity_type = t
            if value is not True:
                name_query = value
            break

    has_filters = _has_filters(args)

    with MetalArchivesClient() as client:
        navigator = Navigator(client)

        try:
            if has_filters:
                # Advanced search mode
                search_type = entity_type or "band"

                errors = _validate_filters(args, search_type)
                if errors:
                    for e in errors:
                        console.print(f"[red]{e}[/red]")
                    return

                filters = _build_advanced_filters(args, search_type)
                if filters is None:
                    return

                # If a name was also given, add it to filters
                if name_query:
                    if search_type == "band":
                        filters["bandName"] = name_query
                    elif search_type == "album":
                        filters["releaseTitle"] = name_query
                    elif search_type == "song":
                        filters["songTitle"] = name_query

                if not filters:
                    parser.error("No valid filters provided.")

                _run_advanced_search(navigator, search_type, filters, args)

            elif entity_type and name_query:
                # Simple name search with entity type
                _run_search(navigator, name_query, entity_type, args)
            elif args.query:
                # Positional query — search all categories
                _run_search(navigator, args.query, None, args)
            else:
                parser.error(
                    "Provide a search query or use filters.\n"
                    "  metallum Summoning\n"
                    "  metallum --band Summoning\n"
                    '  metallum --genre "black metal"\n'
                    "  metallum --album --genre doom --year 1990-1995"
                )
        except (_QuitSignal, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    main()
