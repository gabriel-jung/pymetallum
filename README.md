# pymetallum

An interactive terminal browser for [Metal Archives](https://www.metal-archives.com).

Search bands, albums, artists, songs, and labels — then navigate between them
with a menu-driven UI. Band logos and album covers display inline on supported
terminals.

## Install

```bash
pip install pymetallum
# or
uv tool install pymetallum
```

For local development:

```bash
git clone https://github.com/gabriel-jung/pymetallum.git
cd pymetallum
uv sync
```

## Usage

### Search

```bash
metallum Summoning                  # search across all categories
metallum --band Summoning           # bands only
metallum --album "Minas Morgul"     # albums only
metallum --artist "Protector"       # artists only
metallum --song Lugburz             # songs only
metallum --label "Napalm Records"   # labels only
```

By default, exact name matches are shown first. Use `-s` to see all results:

```bash
metallum --band Summoning -s
```

### Advanced search (filters)

Filters can be combined and work with `--band`, `--album`, and `--song`:

```bash
# bands
metallum --genre "black metal"
metallum --band --country norway --status active
metallum --band --genre thrash --themes war --year 1985-1992
metallum --band --location "Bay Area" --label-name "Megaforce"

# albums
metallum --album --genre doom --year 1990-1995
metallum --album --country finland

# songs
metallum --song --lyrics "ring of power"
metallum --song --genre "death metal"
```

Available filters by entity type:

| Filter         | `--band` | `--album` | `--song` |
|----------------|----------|-----------|----------|
| `--genre`      | x        | x         | x        |
| `--country`    | x        | x         |          |
| `--year`       | x        | x         |          |
| `--status`     | x        |           |          |
| `--themes`     | x        |           |          |
| `--location`   | x        | x         |          |
| `--label-name` | x        | x         |          |
| `--lyrics`     |          |           | x        |

`--country` accepts ISO codes (`NO`, `FI`, `US`) or full names (`Norway`, `Finland`).
`--status` accepts: `active`, `split-up`, `on hold`, `changed name`, `unknown`.
`--year` accepts a single year (`1995`) or a range (`1990-1995`).

### Interactive navigation

After selecting a result, you enter an interactive browser. A numbered menu
lets you explore sections — discography, members, tracklist, reviews, similar
artists, lyrics, and more. Select any linked entity (a band member, an album,
a label) to navigate to it. Press `0` to go back.

### Browse recent & upcoming

```bash
# recently added/modified bands (this month)
metallum --recent
metallum --new                       # only newly created
metallum --modified                  # only recently modified

# labels
metallum --label --recent
metallum --label --new

# specific month
metallum --new --month 2025-01

# date range (fetches full months then filters — slow for wide ranges)
metallum --new --from 2025-03-01
metallum --modified --from 2025-02-15 --to 2025-03-15

# upcoming album releases
metallum --upcoming
metallum --upcoming --from 2025-04-01 --to 2025-06-01
```

### Random band

```bash
metallum --random
```

### Output modes

```bash
metallum --band Summoning --full    # print all sections, no interaction
metallum --band Summoning --json    # dump full entity data as JSON
metallum --upcoming --json          # works with any mode
metallum -v ...                     # enable debug logging
```

### Terminal images

Band logos and album covers render inline on terminals that support the
iTerm2 or Kitty image protocol (iTerm2, Kitty, WezTerm, Mintty).

## Library

```python
from pymetallum.core import MetalArchivesClient, BandAPI, AlbumAPI, LabelAPI

with MetalArchivesClient() as client:
    bands = BandAPI(client)
    results = bands.search("Summoning")
    band = bands.get(results[0]["url"])

    results, total = bands.advanced_search(genre="black metal", country="AT")

    # random band
    band = bands.get_random()

    # recent/upcoming
    new_bands = bands.fetch_recently_created("2025-03")
    modified_bands = bands.fetch_recently_modified("2025-03")
    page, total = bands.fetch_recent_page("created", "2025-03", start=0, count=25)

    albums = AlbumAPI(client)
    album = albums.get(band["discography"][0]["url"])
    upcoming = albums.fetch_upcoming(from_date="2025-04-01", to_date="2025-06-01")

    labels = LabelAPI(client)
    new_labels = labels.fetch_recently_created("2025-03")
```

All data is returned as plain dicts. The `core` module has no terminal
dependencies — use it in scripts, pipelines, or other tools.

Available API classes: `BandAPI`, `AlbumAPI`, `ArtistAPI`, `SongAPI`, `LabelAPI`.

## License

MIT
