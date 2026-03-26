"""Interactive entity browser with back-navigation, pagination, and lazy fetching."""

from ..core.api import AlbumAPI, ArtistAPI, BandAPI, LabelAPI, SongAPI
from ..core.client import MetalArchivesClient

from .display import (
    ENTITY_SECTIONS,
    LAZY_SECTIONS,
    SUMMARY,
    print_text_panel,
    console,
    display_details,
    display_header,
    display_section,
    display_section_page,
)


class _QuitSignal(Exception):
    """Raised to exit the entire interactive session."""


# Maps (entity_type, section_key) to a callable(api, entity) -> fetched_data
LAZY_FETCHERS = {
    ("band", "description"): lambda api, e: api.fetch_description(e["id"]),
    ("band", "similar_artists"): lambda api, e: api.fetch_similar_artists(e["id"]),
}

# Maps (entity_type, section_key) to a callable(entity) -> list of navigable items
# Each item must have "_type" and "url" to be navigable.
NAVIGABLE_SECTIONS = {
    ("band", "discography"): lambda d: d.get("discography", []),
    ("band", "members"): lambda d: [
        m for ml in d.get("members", {}).values() for m in ml
    ],
    ("band", "similar_artists"): lambda d: d.get("similar_artists", []),
    ("album", "lineup"): lambda d: [
        m for ml in d.get("lineup", {}).values() for m in ml
    ],
    ("artist", "bands_overview"): lambda d: [
        bi["band"] for bi in d.get("bands_overview", [])
    ],
    ("album", "tracklist"): lambda d: d.get("tracklist", []),
    ("label", "current_roster"): lambda d: d.get("current_roster", []),
    ("label", "past_roster"): lambda d: d.get("past_roster", []),
    ("label", "releases"): lambda d: d.get("releases", []),
}


class Navigator:
    """Handles interactive browsing between entities."""

    def __init__(self, client: MetalArchivesClient):
        self.apis = {
            "band": BandAPI(client),
            "album": AlbumAPI(client),
            "artist": ArtistAPI(client),
            "song": SongAPI(client),
            "label": LabelAPI(client),
        }
        self._history: list[dict] = []

    def fetch(self, entity_type: str, url: str) -> dict | None:
        api = self.apis[entity_type]
        with console.status("Fetching details..."):
            return api.get(url)

    def navigate(self, entity: dict) -> None:
        """Interactive display with navigation. Supports back navigation."""
        self._history.append(entity)
        try:
            self._interactive_loop(entity)
        finally:
            self._history.pop()

    def browse(self, *, fetch_page, render_page=None, title=None,
               page_size=25, full=False, loop=False):
        """Paginated item browsing with navigation.

        fetch_page(start, count) -> (results, total)
        render_page(results, start): custom page renderer (default: one-liners)
        loop: stay in browsing loop after entity navigation
        """
        start = 0
        results, total = fetch_page(start, page_size)
        if not results:
            if not render_page:
                console.print("[yellow]No items found.[/yellow]")
            return

        def _render_default(page_results, page_start):
            if title:
                console.print(f"\n[bold]{title}[/bold]")
            for i, item in enumerate(page_results, page_start + 1):
                console.print(f"  [bold cyan]\\[{i}][/bold cyan] ", end="")
                SUMMARY.get(item.get("_type", "band"), SUMMARY["band"])(item)

        render = render_page or _render_default

        # Custom render: caller already displayed the section
        if not render_page:
            render(results, start)

        while True:
            end = min(start + len(results), total)
            total_pages = (total + page_size - 1) // page_size
            page = start // page_size

            console.print()
            if total_pages > 1:
                console.print(
                    f"[dim]Page {page + 1}/{total_pages} "
                    f"({start + 1}-{end} of {total})[/dim]"
                )

            hints = [f"[bold]{start + 1}-{end}[/bold] to select"]
            if page > 0:
                hints.extend(["[bold]f[/bold]irst", "[bold]p[/bold]rev"])
            if page < total_pages - 1:
                hints.extend(["[bold]n[/bold]ext", "[bold]l[/bold]ast"])
            console.print(f"[dim]{' | '.join(hints)}[/dim]")
            console.print("[dim][bold]0[/bold] to go back | Ctrl+C to quit[/dim]")

            try:
                raw = console.input("[bold]>[/bold] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                raise _QuitSignal()

            if not raw:
                continue
            if raw == "0":
                return

            # Page navigation
            new_start = None
            if raw == "n" and page < total_pages - 1:
                new_start = start + page_size
            elif raw == "p" and page > 0:
                new_start = max(0, start - page_size)
            elif raw == "f" and page > 0:
                new_start = 0
            elif raw == "l" and page < total_pages - 1:
                new_start = max(0, total - page_size)

            if new_start is not None:
                with console.status("Loading..."):
                    results, total = fetch_page(new_start, page_size)
                if not results:
                    console.print("[dim]No more results.[/dim]")
                    return
                start = new_start
                render(results, start)
                continue

            # Item selection
            try:
                choice = int(raw)
            except ValueError:
                continue

            idx = choice - 1 - start
            if not (0 <= idx < len(results)):
                continue

            item = results[idx]

            # Lyrics
            if item.get("song_id") and item.get("has_lyrics"):
                api = next(iter(self.apis.values()))
                with console.status("Fetching lyrics..."):
                    lyrics = api.fetch_lyrics(item["song_id"])
                if lyrics:
                    print_text_panel(lyrics, item["name"])
                else:
                    console.print("[dim]Lyrics not available.[/dim]")
                continue

            # Entity navigation
            if not item.get("_type") or not item.get("url"):
                console.print("[dim]This item is not navigable.[/dim]")
                continue

            target = self.fetch(item["_type"], item["url"])
            if not target:
                console.print("[red]Could not fetch details.[/red]")
                continue

            if full:
                display_details(target)
                return

            self.navigate(target)
            if not loop:
                return
            # Re-display after coming back
            render(results, start)

    def _interactive_loop(self, entity: dict) -> None:
        display_header(entity)

        entity_type = entity["_type"]
        sections = ENTITY_SECTIONS.get(entity_type, [])
        if not sections:
            return

        lazy_keys = LAZY_SECTIONS.get(entity_type, set())
        header_links = self._get_header_links(entity)

        while True:
            console.print()
            for i, (key, label, _fn) in enumerate(sections, 1):
                has_data = bool(entity.get(key))
                is_lazy = key in lazy_keys
                is_nav = (entity_type, key) in NAVIGABLE_SECTIONS

                suffix = " [dim]→[/dim]" if is_nav else ""

                if has_data or is_lazy:
                    console.print(f"  [bold cyan]\\[{i}][/bold cyan] {label}{suffix}")
                else:
                    console.print(f"  [dim]\\[{i}] {label} (empty)[/dim]")

            for j, (link_label, link_type, link_url) in enumerate(header_links):
                idx = len(sections) + 1 + j
                console.print(
                    f"  [bold cyan]\\[{idx}][/bold cyan] {link_label} [dim]→[/dim]"
                )

            console.print()
            back_label = "go back" if len(self._history) > 1 else "exit"
            console.print(
                f"  [dim][bold]0[/bold] to {back_label} | Ctrl+C to quit[/dim]"
            )

            try:
                raw = console.input("\n[bold]Choose:[/bold] ").strip()
            except (KeyboardInterrupt, EOFError):
                raise _QuitSignal()

            if not raw:
                continue

            try:
                choice = int(raw)
            except ValueError:
                continue

            if choice == 0:
                break

            total_sections = len(sections)
            total_header_links = len(header_links)

            if 1 <= choice <= total_sections:
                key, label, _fn = sections[choice - 1]

                # Lazy fetch if needed
                if not entity.get(key) and (entity_type, key) in LAZY_FETCHERS:
                    api = self.apis[entity_type]
                    with console.status(f"Fetching {label}..."):
                        entity[key] = LAZY_FETCHERS[(entity_type, key)](api, entity)

                display_section(entity, key)

                # Offer navigation into section items
                nav_fn = NAVIGABLE_SECTIONS.get((entity_type, key))
                if nav_fn:
                    items = nav_fn(entity)
                    n = len(items)
                    self.browse(
                        fetch_page=lambda s, c: (items[s : s + c], n),
                        render_page=lambda r, s: display_section_page(
                            items, s, len(r)
                        ),
                        page_size=n if n <= 100 else 25,
                        loop=True,
                    )

            elif total_sections < choice <= total_sections + total_header_links:
                _, link_type, link_url = header_links[choice - total_sections - 1]
                target = self.fetch(link_type, link_url)
                if target:
                    self.navigate(target)
                    display_header(entity)
                else:
                    console.print("[red]Could not fetch details.[/red]")
            else:
                console.print("[red]Invalid choice.[/red]")

    def _get_header_links(self, entity: dict) -> list[tuple[str, str, str]]:
        """Return navigable links from the header: (display_label, entity_type, url)."""
        links = []
        entity_type = entity["_type"]

        if entity_type == "band":
            if entity.get("label_url"):
                label_name = entity.get("current_label", "Label")
                links.append((f"Label: {label_name}", "label", entity["label_url"]))

        elif entity_type == "album":
            band_ids = entity.get("band_id", [])
            if len(band_ids) == 1:
                band_name = entity.get("band", "Band")
                links.append(
                    (f"Band: {band_name}", "band", BandAPI.url(band_ids[0]))
                )
            label_ids = entity.get("label_id", [])
            if len(label_ids) == 1:
                label_name = entity.get("label", "Label")
                links.append(
                    (
                        f"Label: {label_name}",
                        "label",
                        LabelAPI.url(label_ids[0]),
                    )
                )

        return links
