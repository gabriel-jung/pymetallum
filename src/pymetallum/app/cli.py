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
    metallum --recent                    # recently added/modified bands
    metallum --new                       # only newly created bands
    metallum --modified                  # only recently modified bands
    metallum --label --new               # newly created labels
    metallum --recent --month 2026-02    # specific month
    metallum --new --from 2026-03-20     # new bands since a specific date
    metallum --upcoming                  # upcoming album releases
    metallum --upcoming --from 2026-04-01 --to 2026-06-01
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from importlib.metadata import version

__version__ = version("pymetallum")

from loguru import logger

from ..core.client import MetalArchivesClient
from .countries import resolve_country
from .display import console, display_details, select_from_list
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
        "--random",
        action="store_true",
        help="Open a random band page",
    )
    parser.add_argument(
        "--recent",
        action="store_true",
        help="Show recently added/modified entries (default: bands, or use --label)",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Show only newly created entries (implies --recent)",
    )
    parser.add_argument(
        "--modified",
        action="store_true",
        help="Show only recently modified entries (implies --recent)",
    )
    parser.add_argument(
        "--upcoming",
        action="store_true",
        help="Show upcoming album releases",
    )
    parser.add_argument(
        "--month",
        type=str,
        help="Month for --recent (YYYY-MM format, default: current month)",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        type=str,
        help="Start date (YYYY-MM-DD) for --recent or --upcoming",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        type=str,
        help="End date (YYYY-MM-DD) for --recent or --upcoming",
    )
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

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


def _months_in_range(from_date: date, to_date: date) -> list[str]:
    """Return list of YYYY-MM strings covering the date range."""
    months = []
    current = from_date.replace(day=1)
    while current <= to_date:
        months.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def _parse_date(date_str: str) -> date | None:
    """Parse a YYYY-MM-DD date string."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_date(item: dict, year: int | None = None) -> date | None:
    """Extract the date from an item's added_on or modified_on field.

    Metal Archives archive dates use the format "Mar 2nd, 05:43" (no year).
    The *year* must be supplied from the month context (e.g. "2025-03" → 2025).
    """

    for key in ("added_on", "modified_on"):
        val = item.get(key)
        if not val:
            continue
        # Try ISO format first (YYYY-MM-DD)
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
        # Metal Archives format: "Mar 2nd, 05:43"
        m = re.match(r"([A-Z][a-z]{2})\s+(\d+)\w*,", val)
        if m and year:
            try:
                month_num = datetime.strptime(m.group(1), "%b").month
                day = int(m.group(2))
                return date(year, month_num, day)
            except (ValueError, TypeError):
                pass
    return None


def _list_fetcher(items: list):
    """Wrap a list as a fetch_page(start, count) -> (page, total) callable."""

    def fetch_page(start, count):
        return items[start : start + count], len(items)

    return fetch_page


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
    filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items())

    if args.json:
        with console.status(f"Searching {entity_type}s ({filter_desc})..."):
            results, _total = api.advanced_search(start=0, count=200, **filters)
        if not results:
            console.print(
                f"[yellow]No {entity_type}s found matching your criteria.[/yellow]"
            )
            return
        print(json.dumps(results, indent=2))
        return

    navigator.browse(
        fetch_page=lambda s, c: api.advanced_search(start=s, count=c, **filters),
        title=f"{entity_type.capitalize()}s ({filter_desc})",
        full=args.full,
    )


def _fetch_recent_filtered(api, mode, months, from_d, to_d):
    """Fetch and date-filter recent entries across one or more months."""
    all_items = []
    for m in months:
        year = int(m.split("-")[0])
        batch = (
            api.fetch_recently_created(m)
            if mode == "created"
            else api.fetch_recently_modified(m)
        )
        if from_d or to_d:
            for item in batch:
                d = _extract_date(item, year=year)
                if not d:
                    continue
                if from_d and d < from_d:
                    continue
                if to_d and d > to_d:
                    continue
                all_items.append(item)
        else:
            all_items.extend(batch)
    return all_items


def _run_recent(navigator, entity_type, args):
    """Show recently created/modified bands or labels."""
    api = navigator.apis[entity_type]

    from_d = _parse_date(args.from_date) if args.from_date else None
    to_d = _parse_date(args.to_date) if args.to_date else None

    if args.from_date and not from_d:
        console.print("[red]Invalid --from date. Use YYYY-MM-DD format.[/red]")
        return
    if args.to_date and not to_d:
        console.print("[red]Invalid --to date. Use YYYY-MM-DD format.[/red]")
        return

    # Determine which modes to show
    if args.new and not args.modified:
        requested_modes = ["created"]
    elif args.modified and not args.new:
        requested_modes = ["modified"]
    else:
        requested_modes = ["created", "modified"]

    has_date_filter = from_d or to_d

    if has_date_filter and args.month:
        console.print(
            "[red]Cannot use --month with --from/--to. Use one or the other.[/red]"
        )
        return

    if has_date_filter:
        # Date-filtered mode: fetch full months, filter client-side
        if not from_d:
            from_d = date.today().replace(day=1)
        if not to_d:
            to_d = date.today()
        months = _months_in_range(from_d, to_d)
        date_desc = f"{from_d} to {to_d}"

        fetched = {}
        with console.status(f"Fetching {entity_type}s ({date_desc})..."):
            for mode in requested_modes:
                fetched[mode] = _fetch_recent_filtered(api, mode, months, from_d, to_d)

        if args.json:
            print(json.dumps(fetched, indent=2))
            return

        mode_entries = [
            (m, _mode_label(m, entity_type, len(fetched[m])), fetched[m])
            for m in requested_modes
        ]

        # Skip menu if only one mode
        if len(mode_entries) == 1:
            _, label, items = mode_entries[0]
            if not items:
                console.print("[yellow]No items found.[/yellow]")
                return
            navigator.browse(
                fetch_page=_list_fetcher(items),
                title=label,
                full=args.full,
            )
            return

        _recent_menu(navigator, mode_entries, args)
    else:
        # Month mode: server-side pagination
        month = args.month

        totals = {}
        with console.status(
            f"Fetching recent {entity_type}s ({month or 'this month'})..."
        ):
            for mode in requested_modes:
                _, totals[mode] = api.fetch_recent_page(mode, month)

        if args.json:
            fetched = {}
            with console.status("Fetching all results..."):
                for mode in requested_modes:
                    fetched[mode] = (
                        api.fetch_recently_created(month)
                        if mode == "created"
                        else api.fetch_recently_modified(month)
                    )
            print(json.dumps(fetched, indent=2))
            return

        mode_entries = [
            (
                m,
                _mode_label(m, entity_type, totals[m]),
                lambda s, c, m=m: api.fetch_recent_page(m, month, s, c),
            )
            for m in requested_modes
        ]

        # Skip menu if only one mode
        if len(mode_entries) == 1:
            _, label, fetch_fn = mode_entries[0]
            navigator.browse(
                fetch_page=fetch_fn,
                title=label,
                full=args.full,
            )
            return

        _recent_menu(navigator, mode_entries, args)


def _mode_label(mode: str, entity_type: str, count: int) -> str:
    prefix = "New" if mode == "created" else "Modified"
    return f"{prefix} {entity_type}s ({count})"


def _recent_menu(navigator, mode_entries, args):
    """Menu to choose between created/modified, then browse.

    mode_entries is a list of tuples. Each tuple contains:
      - (mode, label, fetch_page_fn)  — with a pre-built fetch_page callable
      - or (mode, label, items)       — with pre-fetched items (date-filtered)
    """
    while True:
        console.print()
        for i, entry in enumerate(mode_entries, 1):
            console.print(f"  [bold cyan]\\[{i}][/bold cyan] {entry[1]}")
        console.print()
        console.print("  [dim][bold]0[/bold] to go back | Ctrl+C to quit[/dim]")

        try:
            raw = console.input("\n[bold]Choose:[/bold] ").strip()
        except (KeyboardInterrupt, EOFError):
            return

        if not raw:
            continue

        try:
            choice = int(raw)
        except ValueError:
            continue

        if choice == 0:
            return

        if 1 <= choice <= len(mode_entries):
            entry = mode_entries[choice - 1]
            label = entry[1]
            data = entry[2]

            # data is either a fetch_page callable or a pre-fetched list
            if callable(data):
                fetch_page = data
            else:
                items = data
                if not items:
                    console.print("[yellow]No items found.[/yellow]")
                    continue
                fetch_page = _list_fetcher(items)

            navigator.browse(
                fetch_page=fetch_page,
                title=label,
                full=args.full,
            )


def _run_upcoming(navigator, args):
    """Show upcoming album releases."""
    album_api = navigator.apis["album"]
    from_date = args.from_date
    to_date = args.to_date

    with console.status("Fetching upcoming releases..."):
        first_page, total = album_api.fetch_upcoming_page(
            from_date=from_date, to_date=to_date
        )

    if not first_page:
        console.print("[yellow]No upcoming releases found.[/yellow]")
        return

    if args.json:
        with console.status("Fetching all results..."):
            releases = album_api.fetch_upcoming(from_date=from_date, to_date=to_date)
        print(json.dumps(releases, indent=2))
        return

    navigator.browse(
        fetch_page=lambda s, c: album_api.fetch_upcoming_page(
            s, c, from_date=from_date, to_date=to_date
        ),
        title=f"Upcoming releases ({total})",
        full=args.full,
    )


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
            if args.random:
                with console.status("Fetching random band..."):
                    band = navigator.apis["band"].get_random(full=args.full)
                if not band:
                    console.print("[red]Could not fetch random band.[/red]")
                    return
                if args.json:
                    print(json.dumps(_strip_bytes(band), indent=2))
                    return
                if args.full:
                    display_details(band)
                    return
                navigator.navigate(band)
                return

            elif args.recent or args.new or args.modified:
                recent_type = entity_type or "band"
                if recent_type not in ("band", "label"):
                    console.print(
                        "[red]--recent is only available for bands and labels. "
                        "Use --band --recent or --label --recent.[/red]"
                    )
                    return
                _run_recent(navigator, recent_type, args)
                return

            elif args.upcoming:
                _run_upcoming(navigator, args)
                return

            elif has_filters:
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
