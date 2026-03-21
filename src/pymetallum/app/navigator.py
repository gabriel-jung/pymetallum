"""Interactive entity browser with back-navigation, pagination, and lazy fetching."""

from ..core.api import AlbumAPI, ArtistAPI, BandAPI, LabelAPI, SongAPI
from ..core.client import BASE_URL, MetalArchivesClient

from .display import (
    ENTITY_SECTIONS,
    LAZY_SECTIONS,
    print_text_panel,
    console,
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

    def _interactive_loop(self, entity: dict) -> None:
        display_header(entity)

        entity_type = entity["_type"]
        sections = ENTITY_SECTIONS.get(entity_type, [])
        if not sections:
            return

        lazy_keys = LAZY_SECTIONS.get(entity_type, set())

        # Collect header-level navigable links (e.g. band → label)
        header_links = self._get_header_links(entity)

        while True:
            console.print()
            for i, (key, label, _fn) in enumerate(sections, 1):
                has_data = bool(entity.get(key))
                is_lazy = key in lazy_keys
                is_nav = (entity_type, key) in NAVIGABLE_SECTIONS

                suffix = ""
                if is_nav:
                    suffix = " [dim]→[/dim]"

                if has_data or is_lazy:
                    console.print(f"  [bold cyan]\\[{i}][/bold cyan] {label}{suffix}")
                else:
                    console.print(f"  [dim]\\[{i}] {label} (empty)[/dim]")

            # Header links
            for j, (link_label, link_type, link_url) in enumerate(header_links):
                idx = len(sections) + 1 + j
                console.print(
                    f"  [bold cyan]\\[{idx}][/bold cyan] {link_label} [dim]→[/dim]"
                )

            back_label = "Back" if len(self._history) > 1 else "Exit"
            console.print(f"  [bold cyan]\\[0][/bold cyan] {back_label}")
            console.print("  [dim]Ctrl+C to quit[/dim]")

            try:
                raw = console.input("\n[bold]Choose:[/bold] ").strip()
            except (KeyboardInterrupt, EOFError):
                raise _QuitSignal()

            if not raw:
                break

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
                    if items:
                        self._offer_navigation(items)

            elif total_sections < choice <= total_sections + total_header_links:
                _, link_type, link_url = header_links[choice - total_sections - 1]
                target = self.fetch(link_type, link_url)
                if target:
                    self.navigate(target)
                    # Re-display header when coming back
                    display_header(entity)
                else:
                    console.print("[red]Could not fetch details.[/red]")
            else:
                console.print("[red]Invalid choice.[/red]")

    def _offer_navigation(self, items: list[dict]) -> None:
        """Paginated navigation through section items."""
        has_navigable = any(item.get("_type") and item.get("url") for item in items)
        has_lyrics = any(item.get("has_lyrics") for item in items)
        if not has_navigable and not has_lyrics:
            return

        page_size = 25
        page = 0
        total = len(items)
        total_pages = (total + page_size - 1) // page_size

        while True:
            start = page * page_size
            end = min(start + page_size, total)

            console.print()
            if total_pages > 1:
                console.print(
                    f"[dim]Page {page + 1}/{total_pages} "
                    f"(items {start + 1}-{end} of {total})[/dim]"
                )

            hints = [f"[bold]1-{total}[/bold] to select"]
            if page > 0:
                hints.append("[bold]f[/bold]irst page")
                hints.append("[bold]p[/bold]rev page")
            if page < total_pages - 1:
                hints.append("[bold]n[/bold]ext page")
                hints.append("[bold]l[/bold]ast page")
            hints.append("Enter to go back")

            console.print(f"[dim]{' | '.join(hints)}[/dim]")

            try:
                raw = console.input("[bold]>[/bold] ").strip()
            except (KeyboardInterrupt, EOFError):
                raise _QuitSignal()

            if not raw:
                return

            raw = raw.lower()

            new_page = page
            if raw == "n" and page < total_pages - 1:
                new_page = page + 1
            elif raw == "p" and page > 0:
                new_page = page - 1
            elif raw == "f" and page > 0:
                new_page = 0
            elif raw == "l" and page < total_pages - 1:
                new_page = total_pages - 1

            if new_page != page:
                page = new_page
                display_section_page(items, start=page * page_size, count=page_size)
                continue

            try:
                idx = int(raw) - 1
            except ValueError:
                continue

            if 0 <= idx < total:
                item = items[idx]

                # Track with lyrics — fetch_lyrics is on BaseAPI, any API works
                if item.get("song_id") and item.get("has_lyrics"):
                    api = next(iter(self.apis.values()))
                    with console.status("Fetching lyrics..."):
                        lyrics = api.fetch_lyrics(item["song_id"])
                    if lyrics:
                        print_text_panel(lyrics, item["name"])
                    else:
                        console.print("[dim]Lyrics not available.[/dim]")
                    continue

                # Regular entity navigation
                if not item.get("_type") or not item.get("url"):
                    console.print("[dim]This item is not navigable.[/dim]")
                    continue
                target = self.fetch(item["_type"], item["url"])
                if target:
                    self.navigate(target)
                    # Re-display current page after coming back
                    display_section_page(items, start=page * page_size, count=page_size)

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
                    (f"Band: {band_name}", "band", f"{BASE_URL}/bands//{band_ids[0]}")
                )
            label_ids = entity.get("label_id", [])
            if len(label_ids) == 1:
                label_name = entity.get("label", "Label")
                links.append(
                    (
                        f"Label: {label_name}",
                        "label",
                        f"{BASE_URL}/labels//{label_ids[0]}",
                    )
                )

        return links
