"""Display functions for rendering Metal Archives entities in the terminal.

Each entity type (band, album, artist, song, label) has three display levels:
- **summary**: one-line format for search result lists
- **header**: info panel with key/value grid (and optional image)
- **details**: header + all sections expanded

Dispatch dicts (``SUMMARY``, ``HEADER``, ``DETAILS``) map ``_type`` strings to
the corresponding display function.
"""

import sys
from collections.abc import Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .images import get_image_escape

console = Console()

IMAGE_ROWS = 8
IMAGE_COLS = 22
MIN_PANEL_WIDTH = 40


def _show_image_beside(image_data: bytes | None, panel: Panel) -> None:
    """Display image and panel side by side, or panel only if images unsupported."""
    escape = (
        get_image_escape(image_data, height=IMAGE_ROWS, width=IMAGE_COLS)
        if image_data
        else None
    )

    wide_enough = escape and console.width >= IMAGE_COLS + 2 + MIN_PANEL_WIDTH

    if not wide_enough:
        if escape:
            sys.stdout.write(escape + "\n")
            sys.stdout.flush()
        console.print(panel)
        return

    # Render panel to fit beside image
    panel_width = console.width - IMAGE_COLS - 2
    temp = Console(width=panel_width, force_terminal=True, highlight=False)
    with temp.capture() as cap:
        temp.print(panel)
    panel_lines = cap.get().rstrip("\n").split("\n")

    # Print image (cursor ends up below it)
    sys.stdout.write(escape + "\n")

    # Move cursor back up to the top of the image
    sys.stdout.write(f"\033[{IMAGE_ROWS}A")

    # Print panel lines beside image
    total_lines = max(IMAGE_ROWS, len(panel_lines))
    for i in range(total_lines):
        sys.stdout.write(f"\r\033[{IMAGE_COLS + 2}C")
        if i < len(panel_lines):
            sys.stdout.write(panel_lines[i])
        sys.stdout.write("\n")

    sys.stdout.flush()


STATUS_COLORS = {
    "Active": "green",
    "On hold": "yellow",
    "Split-up": "red",
    "Changed name": "cyan",
    "Unknown": "dim",
}


def _info_grid(rows: list[tuple[str, str]]) -> Table:
    """Build a borderless key/value grid for info sections."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", justify="right")
    grid.add_column()
    for label, value in rows:
        if value:
            grid.add_row(label, value)
    return grid


def print_text_panel(text: str | None, title: str) -> None:
    if text:
        console.print()
        console.print(Panel(text, title=title, border_style="dim"))
    else:
        console.print(f"\n[dim]No {title.lower()} available.[/dim]")


# ─── Band ────────────────────────────────────────────────────────────────────


def display_band_summary(d: dict) -> None:
    country = d.get("country") or "Unknown"
    genre = d.get("genre") or "Unknown"
    console.print(f"[bold]{d['name']}[/bold]  [dim]{country}[/dim]  {genre}")


def display_band_header(d: dict) -> None:
    status = d.get("status", "")
    status_color = STATUS_COLORS.get(status, "")
    status_str = f"[{status_color}]{status}[/{status_color}]" if status else ""

    country = d.get("country_of_origin") or d.get("country", "")
    location = d.get("location", "")
    origin = f"{country} ({location})" if country and location else country

    info = _info_grid(
        [
            ("Status", status_str),
            ("Active", d.get("years_active", "")),
            ("Origin", origin),
            ("Genre", d.get("genre", "")),
            ("Label", d.get("current_label", "")),
            ("Themes", d.get("themes", "")),
        ]
    )

    console.print()
    panel = Panel(info, title=f"[bold]{d['name']}[/bold]", border_style="blue")
    _show_image_beside(d.get("_logo_data"), panel)
    if d.get("url"):
        console.print(f"[dim]{d['url']}[/dim]")
    img_url = d.get("logo_url") or d.get("photo_url")
    if img_url:
        console.print(f"[dim]Image: {img_url}[/dim]")


def display_band_details(d: dict) -> None:
    display_band_header(d)
    _print_band_discography(d.get("discography", []))
    _print_grouped_members(d.get("members", {}), "Members")
    if d.get("description"):
        print_text_panel(d["description"], "Description")
    _print_band_similar_artists(d.get("similar_artists", []))


def _print_grouped_members(
    members: dict[str, list], title: str, capitalize_groups: bool = True
) -> None:
    """Render a grouped member/lineup table with #, Name, Role columns."""
    if not members:
        return

    table = Table(title=title, border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Name", style="bold")
    table.add_column("Role")

    idx = 1
    first_group = True
    for group_name, member_list in members.items():
        label = group_name.capitalize() if capitalize_groups else group_name.strip()
        if label:
            if not first_group:
                table.add_section()
            first_group = False
            table.add_row("", f"[bold dim]{label}[/bold dim]", "")
        for member in member_list:
            table.add_row(
                str(idx),
                member["name"],
                member.get("role", ""),
            )
            idx += 1

    console.print()
    console.print(table)


def _print_band_discography(discography: list) -> None:
    if not discography:
        return

    table = Table(title="Discography", border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Year", style="dim", width=6)
    table.add_column("Title", style="bold")
    table.add_column("Type")
    table.add_column("Reviews", justify="right")

    for i, album in enumerate(discography, 1):
        table.add_row(
            str(i),
            album.get("release_date", "?"),
            album["name"],
            album.get("album_type", ""),
            album.get("review_summary", ""),
        )

    console.print()
    console.print(table)


def _print_band_similar_artists(similar: list) -> None:
    if not similar:
        console.print("\n[dim]No similar artists available.[/dim]")
        return

    table = Table(title="Similar Artists", border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Band", style="bold")
    table.add_column("Country")
    table.add_column("Genre")
    table.add_column("Score", justify="right")

    for i, artist in enumerate(similar, 1):
        score = artist.get("score")
        table.add_row(
            str(i),
            artist["name"],
            artist.get("country", ""),
            artist.get("genre", ""),
            str(score) if score is not None else "",
        )

    console.print()
    console.print(table)


# ─── Album ───────────────────────────────────────────────────────────────────


def display_album_summary(d: dict) -> None:
    console.print(f"[bold]{d['name']}[/bold]  [dim]by[/dim] {d.get('band', 'Unknown')}")


def display_album_header(d: dict) -> None:
    info = _info_grid(
        [
            ("Type", d.get("album_type", "")),
            ("Release Date", d.get("release_date", "")),
            ("Label", d.get("label", "")),
            ("Catalog ID", d.get("catalog_id", "")),
            ("Format", d.get("format", "")),
            ("Reviews", d.get("review_summary", "")),
        ]
    )

    title = f"[bold]{d['name']}[/bold] [dim]by[/dim] {d.get('band', 'Unknown')}"
    console.print()
    panel = Panel(info, title=title, border_style="blue")
    _show_image_beside(d.get("_cover_data"), panel)
    if d.get("url"):
        console.print(f"[dim]{d['url']}[/dim]")
    if d.get("cover_url"):
        console.print(f"[dim]Image: {d['cover_url']}[/dim]")


def display_album_details(d: dict) -> None:
    display_album_header(d)
    _print_tracklist(d.get("tracklist", []))
    _print_album_lineup(d.get("lineup", {}))
    _print_album_reviews(d.get("reviews", []))
    if d.get("additional_notes"):
        print_text_panel(d["additional_notes"], "Additional Notes")


def _parse_duration(dur: str) -> int | None:
    """Parse 'MM:SS' to total seconds, or None if unparseable."""
    parts = dur.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return None


def _format_duration(seconds: int) -> str:
    """Format total seconds as 'H:MM:SS' or 'MM:SS'."""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _print_tracklist(tracklist: list) -> None:
    if not tracklist:
        return

    table = Table(title="Tracklist", border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Title", style="bold")
    table.add_column("Duration", justify="right")
    table.add_column("Lyrics", style="dim", justify="center")

    total_seconds = 0
    all_have_duration = True

    for i, track in enumerate(tracklist, 1):
        dur = track.get("duration", "")
        lyrics = "Yes" if track.get("has_lyrics") else ""
        table.add_row(str(i), track["name"], dur, lyrics)

        parsed = _parse_duration(dur) if dur else None
        if parsed is not None:
            total_seconds += parsed
        else:
            all_have_duration = False

    console.print()
    console.print(table)

    if all_have_duration and total_seconds > 0:
        console.print(f"  [dim]Total: {_format_duration(total_seconds)}[/dim]")


def _print_album_lineup(lineup: dict) -> None:
    _print_grouped_members(lineup, "Lineup", capitalize_groups=False)


def _print_album_reviews(reviews: list) -> None:
    if not reviews:
        return

    table = Table(title="Reviews", border_style="dim", show_edge=False)
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Title")
    table.add_column("Reviewer", style="dim")
    table.add_column("Date", style="dim")

    for review in reviews:
        table.add_row(
            review.get("score", ""),
            review.get("title", ""),
            review.get("reviewer", ""),
            review.get("date", ""),
        )

    console.print()
    console.print(table)


# ─── Artist ──────────────────────────────────────────────────────────────────


def display_artist_summary(d: dict) -> None:
    real_name = d.get("real_name") or "N/A"
    country = d.get("country", "")
    console.print(f"[bold]{d['name']}[/bold]  [dim]({real_name})[/dim]  {country}")


def display_artist_header(d: dict) -> None:
    info = _info_grid(
        [
            ("Real Name", d.get("real_name", "")),
            ("Gender", d.get("gender", "")),
            ("Country", d.get("country", "")),
            ("Place of Birth", d.get("place_of_birth", "")),
            ("Birth Date", d.get("birth_date", "")),
        ]
    )

    console.print()
    panel = Panel(info, title=f"[bold]{d['name']}[/bold]", border_style="blue")
    _show_image_beside(d.get("_photo_data"), panel)
    if d.get("url"):
        console.print(f"[dim]{d['url']}[/dim]")
    if d.get("photo_url"):
        console.print(f"[dim]Image: {d['photo_url']}[/dim]")


def display_artist_details(d: dict) -> None:
    display_artist_header(d)
    if d.get("biography"):
        print_text_panel(d["biography"], "Biography")
    _print_artist_bands(d.get("bands_overview", []))


def _print_artist_bands(bands_overview: list) -> None:
    if not bands_overview:
        return

    type_headers = {
        "active": "Active",
        "past": "Past",
        "live": "Live",
        "guest": "Guest/Session",
        "misc": "Misc",
    }

    table = Table(title="Bands", border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Band", style="bold")
    table.add_column("Role")
    table.add_column("Period", style="dim")

    current_type = None
    for i, band_info in enumerate(bands_overview, 1):
        band = band_info["band"]
        band_type = band_info.get("type", "")

        if band_type != current_type:
            if current_type is not None:
                table.add_section()
            label = type_headers.get(band_type, band_type)
            table.add_row("", f"[bold dim]{label}[/bold dim]", "", "")
            current_type = band_type

        table.add_row(
            str(i),
            band["name"],
            band_info.get("role") or "",
            band_info.get("period") or "",
        )

    console.print()
    console.print(table)


# ─── Song ────────────────────────────────────────────────────────────────────


def display_song_summary(d: dict) -> None:
    band = d.get("band", "Unknown")
    album = d.get("album", "Unknown")
    console.print(
        f"[bold]{d['name']}[/bold]  [dim]by[/dim] {band}  [dim]from[/dim] {album}"
    )


def display_song_header(d: dict) -> None:
    info = _info_grid(
        [
            ("Band", d.get("band", "")),
            ("Album", d.get("album", "")),
            ("Track", d.get("track_number", "")),
            ("Duration", d.get("duration", "")),
            ("Has Lyrics", "Yes" if d.get("has_lyrics") else "No"),
        ]
    )

    console.print()
    console.print(Panel(info, title=f"[bold]{d['name']}[/bold]", border_style="blue"))
    if d.get("url"):
        console.print(f"[dim]{d['url']}[/dim]")


def display_song_details(d: dict) -> None:
    display_song_header(d)
    if d.get("lyrics"):
        print_text_panel(d["lyrics"], "Lyrics")


# ─── Label ───────────────────────────────────────────────────────────────────


def display_label_summary(d: dict) -> None:
    country = d.get("country", "")
    console.print(f"[bold]{d['name']}[/bold]  [dim]{country}[/dim]")


def display_label_header(d: dict) -> None:
    info = _info_grid(
        [
            ("Address", d.get("address", "")),
            ("Country", d.get("country", "")),
            ("Phone", d.get("phone", "")),
            ("Status", d.get("status", "")),
            ("Founded", d.get("founding_date", "")),
            ("Styles", ", ".join(d["styles"]) if d.get("styles") else ""),
            ("Website", d.get("website", "")),
            ("Email", d.get("email", "")),
            (
                "Online Shopping",
                "Yes" if d.get("online_shopping") else "",
            ),
        ]
    )

    console.print()
    console.print(Panel(info, title=f"[bold]{d['name']}[/bold]", border_style="blue"))

    if d.get("sub_labels"):
        names = ", ".join(sub["name"] for sub in d["sub_labels"])
        console.print(f"[bold]Sub-labels:[/bold] {names}")

    if d.get("url"):
        console.print(f"[dim]{d['url']}[/dim]")
    if d.get("logo_url"):
        console.print(f"[dim]Image: {d['logo_url']}[/dim]")


def display_label_details(d: dict) -> None:
    display_label_header(d)
    _print_roster_table(d.get("current_roster", []), "Current Roster", count=10)
    _print_roster_table(d.get("past_roster", []), "Past Roster", count=5)
    _print_label_releases(d.get("releases", []))
    if d.get("notes"):
        print_text_panel(d["notes"], "Notes")


def _print_roster_table(roster: list, title: str, count: int | None = None) -> None:
    if not roster:
        return

    items = roster[:count] if count else roster

    table = Table(
        title=f"{title} ({len(roster)} bands)", border_style="dim", show_edge=False
    )
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Band", style="bold")
    table.add_column("Country")
    table.add_column("Genre")

    for i, band in enumerate(items, 1):
        table.add_row(
            str(i),
            band["name"],
            band.get("country") or "Unknown",
            band.get("genre") or "Unknown",
        )

    console.print()
    console.print(table)


def _print_label_releases(releases: list, count: int | None = None) -> None:
    if not releases:
        return

    items = releases[:count] if count else releases

    table = Table(
        title=f"Releases ({len(releases)} albums)",
        border_style="dim",
        show_edge=False,
    )
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Year", style="dim", width=6)
    table.add_column("Band")
    table.add_column("Album", style="bold")
    table.add_column("Type")

    for i, release in enumerate(items, 1):
        table.add_row(
            str(i),
            release.get("release_date", "?"),
            release.get("band", "Unknown"),
            release["name"],
            release.get("album_type", ""),
        )

    console.print()
    console.print(table)


# ─── Selection list ──────────────────────────────────────────────────────────


def select_from_list(results: list[dict]) -> dict | None:
    """Display search results and prompt the user to pick one.

    Returns the selected dict, or None if cancelled. Returns directly if only
    one match.
    """
    if len(results) == 1:
        return results[0]

    console.print()
    console.print("[bold]Multiple items found. Please choose one:[/bold]")
    console.print()
    mixed = len({item["_type"] for item in results}) > 1
    for i, item in enumerate(results, 1):
        tag = f"[dim]{item['_type']:>6}[/dim] " if mixed else ""
        console.print(f"  [bold cyan]\\[{i}][/bold cyan] {tag}", end="")
        SUMMARY[item["_type"]](item)

    num = len(results)
    console.print(f"\n[dim]1-{num} to select | [bold]0[/bold] to go back | Ctrl+C to quit[/dim]")
    while True:
        try:
            raw = console.input("[bold]>[/bold] ").strip()
            if not raw:
                continue
            choice = int(raw)
            if choice == 0:
                return None
            if 1 <= choice <= num:
                return results[choice - 1]
            console.print(
                f"[red]Invalid choice. Please enter a number between 1 and {num}.[/red]"
            )
        except (KeyboardInterrupt, EOFError):
            return None
        except ValueError:
            console.print("[red]Invalid input. Please enter a number.[/red]")


# ─── Dispatch ────────────────────────────────────────────────────────────────

SUMMARY = {
    "band": display_band_summary,
    "album": display_album_summary,
    "artist": display_artist_summary,
    "song": display_song_summary,
    "label": display_label_summary,
}

HEADER = {
    "band": display_band_header,
    "album": display_album_header,
    "artist": display_artist_header,
    "song": display_song_header,
    "label": display_label_header,
}

DETAILS = {
    "band": display_band_details,
    "album": display_album_details,
    "artist": display_artist_details,
    "song": display_song_details,
    "label": display_label_details,
}

# Sections: (key, label, display_fn taking entity dict)
# "lazy" sections are fetchable on demand (not fetched by default)
ENTITY_SECTIONS: dict[str, list[tuple[str, str, Callable[[dict], None]]]] = {
    "band": [
        (
            "discography",
            "Discography",
            lambda d: _print_band_discography(d.get("discography", [])),
        ),
        (
            "members",
            "Members",
            lambda d: _print_grouped_members(d.get("members", {}), "Members"),
        ),
        (
            "description",
            "Description",
            lambda d: print_text_panel(d.get("description"), "Description"),
        ),
        (
            "similar_artists",
            "Similar Artists",
            lambda d: _print_band_similar_artists(d.get("similar_artists", [])),
        ),
    ],
    "album": [
        ("tracklist", "Tracklist", lambda d: _print_tracklist(d.get("tracklist", []))),
        ("lineup", "Lineup", lambda d: _print_album_lineup(d.get("lineup", {}))),
        ("reviews", "Reviews", lambda d: _print_album_reviews(d.get("reviews", []))),
        (
            "additional_notes",
            "Additional Notes",
            lambda d: print_text_panel(d.get("additional_notes"), "Additional Notes"),
        ),
    ],
    "artist": [
        (
            "biography",
            "Biography",
            lambda d: print_text_panel(d.get("biography"), "Biography"),
        ),
        (
            "bands_overview",
            "Bands",
            lambda d: _print_artist_bands(d.get("bands_overview", [])),
        ),
    ],
    "song": [
        ("lyrics", "Lyrics", lambda d: print_text_panel(d.get("lyrics"), "Lyrics")),
    ],
    "label": [
        (
            "current_roster",
            "Current Roster",
            lambda d: _print_roster_table(
                d.get("current_roster", []), "Current Roster", count=25
            ),
        ),
        (
            "past_roster",
            "Past Roster",
            lambda d: _print_roster_table(
                d.get("past_roster", []), "Past Roster", count=25
            ),
        ),
        (
            "releases",
            "Releases",
            lambda d: _print_label_releases(d.get("releases", []), count=25),
        ),
        ("notes", "Notes", lambda d: print_text_panel(d.get("notes"), "Notes")),
    ],
}

# Sections that can be lazily fetched (not included in the initial request)
LAZY_SECTIONS = {
    "band": {"description", "similar_artists"},
}


def display_header(entity: dict) -> None:
    HEADER[entity["_type"]](entity)


def display_details(entity: dict) -> None:
    DETAILS[entity["_type"]](entity)


def display_section_page(items: list[dict], start: int = 0, count: int = 25) -> None:
    """Display a page of navigable items (used for pagination)."""
    if not items:
        return

    end = min(start + count, len(items))
    page_items = items[start:end]

    # Detect item type from first item
    sample = page_items[0] if page_items else {}
    item_type = sample.get("_type", "")

    # Tracklist items have no _type but have song_id/duration
    is_tracklist = not item_type and "song_id" in sample

    table = Table(border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)

    if is_tracklist:
        table.add_column("Title", style="bold")
        table.add_column("Duration", justify="right")
        table.add_column("Lyrics", style="dim", justify="center")
        for i, item in enumerate(page_items, start + 1):
            lyrics = "Yes" if item.get("has_lyrics") else ""
            table.add_row(str(i), item["name"], item.get("duration", ""), lyrics)
    elif item_type == "band":
        table.add_column("Band", style="bold")
        table.add_column("Country")
        table.add_column("Genre")
        for i, item in enumerate(page_items, start + 1):
            table.add_row(
                str(i),
                item["name"],
                item.get("country") or "",
                item.get("genre") or "",
            )
    elif item_type == "album":
        table.add_column("Year", style="dim", width=6)
        table.add_column("Album", style="bold")
        table.add_column("Type")
        table.add_column("Reviews", justify="right")
        for i, item in enumerate(page_items, start + 1):
            table.add_row(
                str(i),
                item.get("release_date", "?"),
                item["name"],
                item.get("album_type", ""),
                item.get("review_summary", ""),
            )
    elif item_type == "artist":
        table.add_column("Name", style="bold")
        table.add_column("Role")
        for i, item in enumerate(page_items, start + 1):
            table.add_row(str(i), item["name"], item.get("role", ""))
    else:
        table.add_column("Name", style="bold")
        for i, item in enumerate(page_items, start + 1):
            table.add_row(str(i), item.get("name", str(item)))

    console.print()
    console.print(table)


def display_section(entity: dict, section_key: str) -> None:
    sections = ENTITY_SECTIONS.get(entity["_type"], [])
    for key, _label, display_fn in sections:
        if key == section_key:
            display_fn(entity)
            return
