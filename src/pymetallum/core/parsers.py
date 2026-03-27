"""HTML page parsers for Metal Archives band, album, artist, song, and label pages."""

import re
from abc import ABC, abstractmethod
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger

from .utils import (
    create_link_dict,
    extract_cell,
    extract_id_from_url,
    extract_text_block,
    normalize_text,
    parse_dt_dd_pairs,
    safe_get_attr,
    safe_get_text,
)


class BasePageParser(ABC):
    """Base class for all Metal Archives page parsers.

    Each subclass receives a BeautifulSoup tree and the page URL, extracts
    structured data, and returns it as a plain dict with a ``_type`` key.

    Args:
        soup: Parsed HTML tree of the page.
        url: The URL that was fetched (used to extract entity IDs).
    """

    def __init__(self, soup: BeautifulSoup, url: str):
        self.soup = soup
        self.url = url

    @abstractmethod
    def parse(self) -> dict | None:
        """Parse the page and return a structured dict, or None on failure."""

    def _parse_artist_rows(self, container, row_selector="tr") -> list[dict]:
        """Parse a table of artists with roles from a lineup container.

        Args:
            container: BeautifulSoup element containing the artist table.
            row_selector: CSS selector for row elements within the container.

        Returns:
            List of artist dicts with ``_type``, ``name``, ``url``, ``id``,
            and ``role``.
        """
        members = []
        for row in container.select(row_selector):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            name_link = cells[0].find("a")
            if not name_link:
                continue
            artist = create_link_dict(name_link)
            if not artist:
                continue
            artist["_type"] = "artist"
            artist["role"] = safe_get_text(cells[1])
            members.append(artist)
        return members


class BandPageParser(BasePageParser):
    """Parser for band detail pages.

    Extracts band info (name, genre, country, status, etc.), label link,
    logo/photo URLs, and member lineups with "see also" bands.
    """

    def parse(self) -> dict | None:
        """Parse band page into a dict with stats, label, and members."""
        name_tag = self.soup.find("h1", class_="band_name")
        if not name_tag:
            return None

        band = {
            "_type": "band",
            "name": safe_get_text(name_tag),
            "url": self.url,
            "id": extract_id_from_url(self.url),
            "logo_url": safe_get_attr(self.soup.select_one("#logo img"), "src"),
            "photo_url": safe_get_attr(self.soup.select_one("#photo img"), "src"),
        }

        band.update(parse_dt_dd_pairs(self.soup, "#band_stats"))
        self._extract_label_link(band)
        band["members"] = self._parse_members()

        return band

    def _extract_label_link(self, band: dict) -> None:
        """Extract label URL and ID from the band stats section."""
        stats = self.soup.select_one("#band_stats")
        if not stats:
            return
        for dt in stats.find_all("dt"):
            if "label" in dt.get_text().lower():
                dd = dt.find_next_sibling("dd")
                if dd:
                    label_link = dd.find("a")
                    if label_link:
                        band["label_url"] = label_link.get("href", "")
                        band["label_id"] = extract_id_from_url(
                            label_link.get("href", "")
                        )
                break

    def _parse_members(self) -> dict[str, list]:
        """Parse band members by lineup type (current/past/live).

        For each member, also extracts "see also" bands from the
        ``lineupBandsRow`` that follows their row in the HTML. Done in a
        single pass per lineup type.
        """
        members = {}

        for lineup_type in ["current", "past", "live"]:
            lineup_div = self.soup.find("div", id=f"band_tab_members_{lineup_type}")
            if not lineup_div:
                continue

            member_list = self._parse_artist_rows(lineup_div, "tr.lineupRow")

            # Build a name→member lookup for attaching "see also" in one pass
            members_by_name = {m["name"]: m for m in member_list}
            for row in lineup_div.select("tr.lineupRow"):
                name_link = row.select_one("td a")
                if not name_link:
                    continue
                name = safe_get_text(name_link)
                if name not in members_by_name:
                    continue
                sibling = row.find_next_sibling("tr")
                if sibling and "lineupBandsRow" in sibling.get("class", []):
                    see_also = [
                        {
                            "name": safe_get_text(a),
                            "url": safe_get_attr(a, "href"),
                            "id": extract_id_from_url(safe_get_attr(a, "href")),
                        }
                        for a in sibling.find_all("a")
                    ]
                    if see_also:
                        members_by_name[name]["see_also"] = see_also

            if member_list:
                members[lineup_type] = member_list

        return members


class AlbumPageParser(BasePageParser):
    """Parser for album detail pages.

    Extracts album metadata, tracklist, lineup, reviews, and additional notes.
    Tracklist and lineup parsing are opt-in via constructor flags since they
    are not needed for every use case.
    """

    def __init__(
        self,
        soup: BeautifulSoup,
        url: str,
        with_tracklist: bool = False,
        with_lineup: bool = False,
    ):
        super().__init__(soup, url)
        self.with_tracklist = with_tracklist
        self.with_lineup = with_lineup

    def parse(self) -> dict | None:
        """Parse album page into a dict with metadata, tracklist, and lineup."""
        album_name_tag = self.soup.select_one("h1.album_name a")
        band_tags = self.soup.find("h2", class_="band_name").find_all("a")
        if not album_name_tag or not band_tags:
            return None
        band_names = " / ".join(tag.text.strip() for tag in band_tags)
        band_ids = [extract_id_from_url(tag.get("href", "")) for tag in band_tags]

        album_info = self._parse_album_info()

        album = {
            "_type": "album",
            "name": safe_get_text(album_name_tag),
            "band": band_names,
            "band_id": band_ids,
            "url": self.url,
            "id": extract_id_from_url(self.url),
            "cover_url": self._parse_cover_url(),
            "album_type": album_info.get("type"),
            "release_date": album_info.get("release_date"),
            "label": album_info.get("label"),
            "label_id": album_info.get("label_id", []),
            "review_summary": album_info.get("reviews"),
            "catalog_id": album_info.get("catalog_id"),
            "format": album_info.get("format"),
        }

        if self.with_tracklist:
            album["tracklist"] = self._parse_tracklist()
        if self.with_lineup:
            album["lineup"] = self._parse_lineup()

        reviews = self._parse_reviews()
        if reviews:
            album["reviews"] = reviews

        notes_div = self.soup.select_one("#album_tabs_notes")
        if notes_div:
            album["additional_notes"] = extract_text_block(notes_div)

        return album

    def _parse_album_info(self) -> dict[str, Any]:
        """Parse album metadata (type, date, label, format) from dt/dd pairs."""
        album_info = {}
        for dt in self.soup.select("#album_info dt"):
            key = dt.get_text(strip=True).replace(":", "")
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue

            if key == "Label":
                label_tags = dd.find_all("a")
                if label_tags:
                    album_info["label"] = " / ".join(
                        tag.text.strip() for tag in label_tags
                    )
                    album_info["label_id"] = [
                        extract_id_from_url(tag.get("href", "")) for tag in label_tags
                    ]
                else:
                    album_info["label"] = dd.get_text(strip=True)
                    album_info["label_id"] = []
            else:
                album_info[key.lower().replace(" ", "_")] = normalize_text(
                    dd.get_text()
                )

        return album_info

    def _parse_cover_url(self) -> str | None:
        """Extract cover art URL, preferring the full-size link over thumbnail."""
        if cover_link := self.soup.select_one("#cover a"):
            return safe_get_attr(cover_link, "href")
        if cover_img := self.soup.select_one("#cover img"):
            return safe_get_attr(cover_img, "src")
        return None

    def _parse_reviews(self) -> list[dict[str, str]]:
        """Parse individual reviews from the ``#review_list`` table."""
        reviews = []
        for row in self.soup.select("#review_list tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            reviews.append(
                {
                    "title": safe_get_text(cells[1]),
                    "score": safe_get_text(cells[2]),
                    "reviewer": safe_get_text(cells[3]),
                    "date": safe_get_text(cells[4]),
                }
            )
        return reviews

    def _parse_tracklist(self) -> list[dict[str, Any]]:
        """Parse the album tracklist with song IDs, durations, and lyrics flags."""
        tracks = []
        for row in self.soup.select("tr.odd, tr.even"):
            cells = row.find_all("td")
            if len(cells) < 4 or not cells[0].text.strip().endswith("."):
                continue

            # Song ID from anchor
            anchor = cells[0].find("a", class_="anchor")
            song_id = anchor.get("name") if anchor else None

            # Lyrics availability and notes from 4th cell
            has_lyrics = bool(
                cells[3].find("a", id=lambda x: x and x.startswith("lyricsButton"))
            )
            note = ""
            em_tag = cells[3].find("em")
            if em_tag:
                note = safe_get_text(em_tag)

            track = {
                "name": safe_get_text(cells[1]),
                "track_number": safe_get_text(cells[0]).rstrip("."),
                "duration": safe_get_text(cells[2]),
                "song_id": song_id,
                "has_lyrics": has_lyrics,
            }
            if has_lyrics and song_id:
                track["_type"] = "song"
            if note:
                track["note"] = note

            tracks.append(track)

        return tracks

    def _parse_lineup(self) -> dict[str, list[dict[str, Any]]]:
        """Parse album lineup, handling split albums with multiple bands."""
        members_tab = self.soup.find("div", id="album_members_lineup")

        lineup_per_band = {}
        current_band = " "
        if members_tab:
            rows = members_tab.select(".lineupTable tr")

            if not any(tr.find("strong") for tr in rows):
                lineup_per_band[current_band] = []

            for tr in rows:
                band_header = tr.find("strong")
                if band_header:
                    current_band = band_header.get_text(strip=True)
                    lineup_per_band[current_band] = []
                    continue

                if "lineupRow" in tr.get("class", []):
                    cols = tr.find_all("td")
                    if len(cols) >= 2:
                        link = cols[0].find("a")
                        artist = (
                            create_link_dict(link)
                            if link
                            else {
                                "name": cols[0].get_text(strip=True),
                                "url": "",
                                "id": None,
                            }
                        )
                        artist["_type"] = "artist"
                        artist["role"] = safe_get_text(cols[1])
                        lineup_per_band[current_band].append(artist)

        return lineup_per_band


class ArtistPageParser(BasePageParser):
    """Parser for artist/member detail pages.

    Extracts personal details (real name, gender, country, birth date),
    biography text, photo URL, and a complete overview of all band
    memberships grouped by status (active, past, live, guest, misc).
    """

    def parse(self) -> dict | None:
        """Parse artist page into a dict with details, bio, and bands."""
        name_tag = self.soup.select_one("h1.band_member_name")
        if not name_tag:
            return None

        details = self._parse_details()
        photo_link = self.soup.select_one("#member_sidebar .member_img a")

        return {
            "_type": "artist",
            "name": safe_get_text(name_tag),
            "url": self.url,
            "id": extract_id_from_url(self.url),
            "real_name": details.get("real_name"),
            "gender": details.get("gender"),
            "country": details.get("country"),
            "place_of_birth": details.get("place_of_birth"),
            "birth_date": details.get("birth_date"),
            "photo_url": safe_get_attr(photo_link, "href") if photo_link else None,
            "biography": self._parse_biography(),
            "bands_overview": self._parse_bands_overview(),
        }

    def _parse_biography(self) -> str | None:
        """Extract and clean the biography/trivia text block."""
        bio_section = self.soup.select_one(".band_comment")
        if not bio_section:
            return None

        # Remove the "Biography" heading before extracting text
        h2 = bio_section.find("h2")
        if h2:
            h2.extract()

        return extract_text_block(bio_section)

    def _parse_details(self) -> dict[str, Any]:
        """Parse personal details (real name, age, country, gender) from dt/dd pairs."""
        details = {}
        for dt in self.soup.select("#member_info dt"):
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue

            dt_text = dt.text.strip().rstrip(":").lower()
            if "real/full name" in dt_text:
                details["real_name"] = dd.text.strip()
            elif "age" in dt_text:
                if match := re.search(r"born\s+(.+?)\)", dd.text.strip()):
                    details["birth_date"] = match.group(1).strip()
            elif "place of birth" in dt_text:
                details["place_of_birth"] = dd.get_text().strip()
                if country_link := dd.find("a"):
                    details["country"] = country_link.text.strip()
            elif "gender" in dt_text:
                details["gender"] = dd.text.strip()
        return details

    def _parse_bands_overview(self) -> list[dict[str, Any]]:
        """Parse all band memberships across all tabs (active, past, live, guest, misc)."""
        all_bands = []
        tab_configs = [
            ("#artist_tab_active", "active"),
            ("#artist_tab_past", "past"),
            ("#artist_tab_live", "live"),
            ("#artist_tab_guest", "guest"),
            ("#artist_tab_misc", "misc"),
        ]

        for tab_id, membership_type in tab_configs:
            if tab := self.soup.select_one(tab_id):
                bands = self._extract_bands_from_tab(tab, membership_type)
                all_bands.extend(bands)
        return all_bands

    def _extract_bands_from_tab(
        self, tab_soup: BeautifulSoup, membership_type: str
    ) -> list[dict[str, Any]]:
        """Parse band entries from a single membership tab."""
        bands = []
        for band_div in tab_soup.select(".member_in_band"):
            band_name_tag = band_div.select_one("h3.member_in_band_name a")
            if band_name_tag:
                band_name = safe_get_text(band_name_tag)
                band_url = safe_get_attr(band_name_tag, "href")
            else:
                band_name_h3 = band_div.select_one("h3.member_in_band_name")
                if not band_name_h3:
                    continue
                band_name = safe_get_text(band_name_h3)
                band_url = ""

            band = {
                "_type": "band",
                "name": band_name,
                "url": band_url,
                "id": extract_id_from_url(band_url) if band_url else None,
            }

            role_info = self._extract_role_and_period(band_div)

            bands.append(
                {
                    "band": band,
                    "type": membership_type,
                    "role": role_info["role"],
                    "period": role_info["period"],
                }
            )

        return bands

    def _extract_role_and_period(self, band_div) -> dict[str, str | None]:
        """Extract the artist's role and active period from a band membership div."""
        role_p = band_div.select_one("p.member_in_band_role")
        if not role_p:
            return {"role": "", "period": None}

        full_text = normalize_text(role_p.get_text())

        period = None
        if period_match := re.search(r"\(([^)]+)\)$", full_text):
            period = period_match.group(1)
            full_text = full_text[: period_match.start()].strip()

        if strong_tags := role_p.find_all("strong"):
            role = ", ".join(tag.get_text().strip() for tag in strong_tags)
        else:
            role = re.sub(r"^As [^:]+:\s*", "", full_text).strip()

        return {"role": role, "period": period}


class SongPageParser(BasePageParser):
    """Parser for extracting a single song from an album page.

    Songs don't have their own pages on Metal Archives — they live inside
    album pages. This parser locates a specific song in the tracklist by
    matching either a fragment ID (``#song_id``) or the song name.

    After parsing, ``self.song_id`` holds the matched song's numeric ID.
    """

    def __init__(
        self,
        soup: BeautifulSoup,
        url: str,
        target_song_name: str = None,
    ):
        super().__init__(soup, url)
        self.song_id = url.split("#", 1)[1] if "#" in url else None
        self.target_song_name = target_song_name

    def parse(self) -> dict | None:
        """Parse the album page and extract the target song from the tracklist."""
        album_name_tag = self.soup.select_one("h1.album_name a")
        band_name_tag = self.soup.select_one("h2.band_name a")

        if not album_name_tag or not band_name_tag:
            return None

        track = self._find_song_in_tracklist()
        if not track:
            return None

        return {
            "_type": "song",
            "name": track["name"],
            "url": self.url,
            "id": self.song_id,
            "band": safe_get_text(band_name_tag),
            "album": safe_get_text(album_name_tag),
            "duration": track["duration"],
            "track_number": track["track_number"],
            "has_lyrics": track["has_lyrics"],
        }

    def _find_song_in_tracklist(self) -> dict[str, Any] | None:
        """Find a specific song in the tracklist by fragment ID or name.

        Tries matching by song ID first (from the URL fragment), then falls
        back to case-insensitive name matching.
        """
        logger.debug(f"Looking for song_id: {self.song_id}")
        logger.debug(f"Looking for target_song_name: {self.target_song_name}")

        for row in self.soup.select("table.display.table_lyrics tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            anchor = cells[0].find("a", class_="anchor")
            if not anchor:
                continue

            track_id = anchor.get("name")
            track = {
                "name": safe_get_text(cells[1]),
                "duration": safe_get_text(cells[2]),
                "track_number": safe_get_text(cells[0]).rstrip("."),
                "has_lyrics": bool(
                    len(cells) > 3
                    and cells[3].find(
                        "a", id=lambda x: x and x.startswith("lyricsButton")
                    )
                ),
                "song_id": track_id,
            }

            if self.song_id and track_id == self.song_id:
                logger.debug(f"Matched by song_id: {self.song_id}")
                return track

            if self.target_song_name:
                if (
                    track["name"].lower().strip()
                    == self.target_song_name.lower().strip()
                ):
                    self.song_id = track_id
                    logger.debug(f"Matched by name: '{track['name']}'")
                    return track

        logger.debug("No song found matching criteria")
        return None


class LabelPageParser(BasePageParser):
    """Parser for label detail pages.

    Combines the main label page with pre-fetched AJAX data for rosters
    and releases (since those are served from separate endpoints).

    Args:
        soup: Parsed HTML tree of the label page.
        url: The label page URL.
        roster_data: Pre-fetched roster tables (``current`` and ``past`` keys).
        releases_data: Pre-fetched releases table rows.
    """

    def __init__(
        self,
        soup: BeautifulSoup,
        url: str,
        roster_data: dict[str, list[list[str]]] | None = None,
        releases_data: list[list[str]] | None = None,
    ):
        super().__init__(soup, url)
        self.roster_data = roster_data or {}
        self.releases_data = releases_data or []

    def parse(self) -> dict | None:
        """Parse label page into a dict with info, contact, rosters, and releases."""
        name_tag = self.soup.find("h1", class_="label_name")
        if not name_tag:
            return None

        label = {
            "_type": "label",
            "name": safe_get_text(name_tag),
            "url": self.url,
            "id": extract_id_from_url(self.url),
        }

        self._parse_label_info(label)
        self._parse_contact_info(label)
        self._parse_notes(label)

        logo_img = self.soup.select_one("#label_logo img")
        if logo_img:
            label["logo_url"] = logo_img.get("src")

        try:
            label["current_roster"] = self._parse_roster(
                self.roster_data.get("current", [])
            )
            label["past_roster"] = self._parse_roster(self.roster_data.get("past", []))
            label["releases"] = self._parse_releases()
        except Exception as e:
            logger.warning(f"Failed to load AJAX data for label {label['id']}: {e}")

        return label

    def _parse_label_info(self, label: dict) -> None:
        """Parse label metadata (address, country, status, styles, etc.) into the dict."""
        label_info = self.soup.select_one("#label_info")
        if not label_info:
            return

        for dt in label_info.find_all("dt"):
            dt_text = safe_get_text(dt).strip().rstrip(":")
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue

            match dt_text.lower():
                case "address":
                    label["address"] = dd.get_text(separator=", ").strip()
                case "country":
                    country_link = dd.find("a")
                    label["country"] = (
                        safe_get_text(country_link)
                        if country_link
                        else safe_get_text(dd)
                    )
                case "phone number":
                    label["phone"] = safe_get_text(dd)
                case "status":
                    status_span = dd.find("span")
                    label["status"] = (
                        safe_get_text(status_span) if status_span else safe_get_text(dd)
                    )
                case s if s.startswith("styles") or s.startswith("specialties"):
                    styles_text = safe_get_text(dd)
                    if styles_text:
                        label["styles"] = [
                            s.strip() for s in styles_text.split(",") if s.strip()
                        ]
                case s if s.startswith("founding date"):
                    label["founding_date"] = safe_get_text(dd)
                case "online shopping":
                    label["online_shopping"] = safe_get_text(dd).lower() == "yes"
                case "sub-labels":
                    label["sub_labels"] = [
                        {
                            "name": safe_get_text(link),
                            "url": link.get("href", ""),
                            "id": extract_id_from_url(link.get("href", "")),
                        }
                        for link in dd.find_all("a")
                    ]

    def _parse_contact_info(self, label: dict) -> None:
        """Extract website and email from the contact section."""
        contact_section = self.soup.select_one("#label_contact")
        if not contact_section:
            return
        website_link = contact_section.select_one("a.external")
        if website_link:
            label["website"] = website_link.get("href")
        email_link = contact_section.select_one("a.nospam")
        if email_link:
            label["email"] = safe_get_text(email_link)

    def _parse_notes(self, label: dict) -> None:
        """Extract label notes/trivia text."""
        notes_section = self.soup.select_one("#label_tabs_notes")
        if not notes_section:
            return

        trivia_p = notes_section.find("p")
        if trivia_p:
            label["notes"] = extract_text_block(trivia_p)

    def _parse_roster(self, data: list[list]) -> list:
        """Parse a roster (current or past) from pre-fetched AJAX data."""
        roster = []
        for row in data:
            if len(row) < 3:
                continue

            band_name, band_url = extract_cell(row[0])
            genre, _ = extract_cell(row[1])
            country, _ = extract_cell(row[2])

            roster.append(
                {
                    "_type": "band",
                    "name": band_name,
                    "url": band_url,
                    "id": extract_id_from_url(band_url),
                    "genre": genre,
                    "country": country,
                }
            )

        return roster

    def _parse_releases(self) -> list:
        """Parse label releases from pre-fetched AJAX table data."""
        releases = []
        for row in self.releases_data:
            if len(row) < 4:
                continue

            band_name, _ = extract_cell(row[0])
            album_title, album_url = extract_cell(row[1])
            album_type, _ = extract_cell(row[2])
            release_date, _ = extract_cell(row[3])

            releases.append(
                {
                    "_type": "album",
                    "name": album_title,
                    "band": band_name,
                    "url": album_url,
                    "id": extract_id_from_url(album_url),
                    "album_type": album_type,
                    "release_date": release_date,
                }
            )
        return releases
