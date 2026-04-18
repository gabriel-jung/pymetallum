# pymetallum

An interactive terminal browser for [Metal Archives](https://www.metal-archives.com).

Search bands, albums, artists, songs, and labels — then navigate between them
with a menu-driven UI. Band logos and album covers display inline on supported
terminals.

## Install

Requires Python 3.12+.

### Terminal CLI

```bash
uv tool install pymetallum
# or
pip install pymetallum
```

### Discord bot

```bash
uv tool install 'pymetallum[discord]'
# or
pip install 'pymetallum[discord]'
```

Create a bot application at the [Discord Developer Portal](https://discord.com/developers/applications),
enable the `bot` scope with `Send Messages` and `Use Slash Commands` permissions,
then invite it to your server with the generated OAuth2 URL.

Set your bot token and run:

```bash
export DISCORD_TOKEN=your-bot-token
metallum-discord
# or with a .env file in the current directory
metallum-discord
```

Use `--guild GUILD_ID` to sync slash commands instantly to a specific server
(global sync can take up to an hour).

Slash commands (all under `/metallum`):

| Command | Description |
|---------|-------------|
| `/metallum band <query>` | Search bands (with optional genre, country, year, status, themes, location, label filters) |
| `/metallum album <query>` | Search albums (with optional genre, country, year, location, label filters) |
| `/metallum artist <query>` | Search artists |
| `/metallum song <query>` | Search songs (with optional genre, lyrics filters) |
| `/metallum label <query>` | Search labels |
| `/metallum search <query>` | Search all categories |
| `/metallum random` | Show a random band |
| `/metallum recent` | Recently added/modified bands or labels |
| `/metallum upcoming` | Upcoming album releases |

### Development

```bash
git clone https://github.com/gabriel-jung/pymetallum.git
cd pymetallum
uv sync  # installs dev deps + [discord] extra
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
metallum --new --month 2026-01

# date range (fetches full months then filters — slow for wide ranges)
metallum --new --from 2026-03-01
metallum --modified --from 2026-02-15 --to 2026-03-15

# upcoming album releases
metallum --upcoming
metallum --upcoming --from 2026-04-01 --to 2026-06-01
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

All data is returned as plain dicts — use it in scripts, pipelines, or other tools.

```python
from pymetallum.core import MetalArchivesClient, BandAPI, AlbumAPI, ArtistAPI, SongAPI, LabelAPI

with MetalArchivesClient() as client:
    bands = BandAPI(client)

    # search and fetch
    results = bands.search("Summoning")
    band = bands.get(results[0]["url"])
    band = bands.get(results[0]["url"], full=True)  # includes description & similar artists

    # advanced search with filters
    results, total = bands.advanced_search(genre="black metal", country="AT")

    # random band
    band = bands.get_random()

    # recent entries
    new_bands = bands.fetch_recently_created("2026-03")
    modified_bands = bands.fetch_recently_modified("2026-03")
    page, total = bands.fetch_recent_page("created", "2026-03", start=0, count=25)

    # albums & upcoming releases
    albums = AlbumAPI(client)
    album = albums.get(band["discography"][0]["url"])
    upcoming = albums.fetch_upcoming(from_date="2026-04-01", to_date="2026-06-01")

    # artists, songs, labels
    artists = ArtistAPI(client)
    songs = SongAPI(client)
    labels = LabelAPI(client)
    new_labels = labels.fetch_recently_created("2026-03")

    # download images
    client.download_image(band["logo_url"], output_dir="./images/")
```

## License

MIT
