# Apple Music → Lidarr Bridge

Favorite (★) a song in Apple Music and have [Lidarr](https://lidarr.audio/)
automatically add the artist and download the album it's from. Un-favorite it and
the album is un-monitored again. It's the missing front door for Lidarr that
[Lidify](https://github.com/TheWicklowWolf/Lidify) is for Spotify — but driven by
your Apple Music favorites.

There is no official Apple Music → Lidarr integration, so this is a small,
self-contained Python service you run alongside Lidarr.

## How it works

Every cycle (default: 15 minutes) the bridge:

1. Reads your **Favorite Songs** playlist via the Apple Music API.
2. Diffs it against the favorites it saw last time.
3. **For each newly favorited song:** resolves its album against MusicBrainz,
   then asks Lidarr to add the artist (monitored: *none*, so the whole
   discography isn't grabbed) and monitor **+ search just that album**.
4. **For each un-favorited song:** un-monitors that album in Lidarr — unless
   another favorite still maps to it. It never deletes files or removes artists.

Everything after "the album is wanted in Lidarr" is your normal Lidarr download
pipeline; this bridge only manages what's wanted.

```
Apple Music "Favorite Songs"  ──poll──▶  bridge  ──▶  MusicBrainz (resolve album)
                                            │
                                            ▼
                                          Lidarr  (add artist + monitor/search album)
                                            │
                                            ▼
                              your existing download/import chain
```

## Requirements

- An **Apple Music subscription** (the account whose favorites you want to sync).
- An **Apple Developer Program** membership ($99/yr) — needed to sign Apple Music
  API tokens. There is no free tier for this API.
- A reachable **Lidarr** instance and its API key.
- Docker (recommended) or Python 3.12+ with the deps in `requirements.txt`.
- *(Optional)* an [Apprise](https://github.com/caronc/apprise) endpoint for the
  "your sign-in token expired" alert.

## Read this first — the honest caveats

- **The user token expires and there is no silent refresh.** Apple's API needs a
  *Music User Token* obtained by signing in through a browser. Apple expires it
  unpredictably (sometimes months, sometimes days), and you must re-mint it by
  hand. The bridge detects expiry and alerts you (via Apprise) so it fails loudly
  rather than going quietly stale.
- **Album matching is best-effort.** A favorited song is matched to a release
  group by title + artist (confirmed against MusicBrainz). Obscure or
  not-yet-catalogued releases may not match; those are logged to
  `data/unresolved.json` instead of guessing wrong.
- **Streaming-only favorites** (songs you favorite while streaming, never adding
  to your library) *are* visible via the Favorite Songs playlist in testing, but
  Apple doesn't document this. If yours go missing, enable Apple Music's "add
  favorites to your library" setting.

## Setup

### 1. Create an Apple Music API key

In the [Apple Developer portal](https://developer.apple.com/account/resources):

1. **Identifiers → + → Media IDs** → enable **MusicKit** → register. (A MusicKit
   key must be associated with a Media ID, or key creation fails with *"no
   identifiers available."*)
2. **Keys → + →** check **MusicKit**, Configure → select the Media ID → register
   → **download the `.p8`** (one chance only). Note the **Key ID**.
3. Your **Team ID** is on the Membership page.

### 2. Configure

```bash
cp data/config.env.example data/config.env
# edit data/config.env: set APPLEMUSIC_TEAM_ID, APPLEMUSIC_KEY_ID, LIDARR_URL,
# LIDARR_API_KEY, and the profile ids you want
cp /path/to/AuthKey_XXXXXXXXXX.p8 data/AuthKey.p8
```

### 3. Mint the Music User Token

On a machine with a browser (the token requires an interactive Apple ID sign-in):

```bash
pip install 'pyjwt[crypto]'
python mint_mut.py --p8 data/AuthKey.p8 --key-id <KEY_ID> --team-id <TEAM_ID>
# a browser opens; sign in; the token is written to ./mut.txt
cp mut.txt data/mut.txt
```

### 4. Find your Favorite Songs playlist id

```bash
docker compose run --rm applemusic-lidarr --discover
# copy the id of the playlist named "Favorite Songs" into
# FAVORITES_PLAYLIST_ID in data/config.env
```

### 5. Run

```bash
docker compose up -d applemusic-lidarr
```

#### Sample `compose.yaml` service

```yaml
services:
  applemusic-lidarr:
    build: .
    image: applemusic-lidarr:local
    container_name: applemusic-lidarr
    user: "1000:1000"            # so state/secrets aren't owned by root
    restart: unless-stopped
    env_file:
      - ./data/config.env
    volumes:
      - ./data:/data
```

## Modes

Run any of these as `docker compose run --rm applemusic-lidarr <flag>`:

| Flag | What it does |
|------|--------------|
| *(none)* | The polling loop (default container command). |
| `--discover` | List your library playlists so you can find the Favorite Songs id. |
| `--probe` | Dump the tail of the favorites playlist + recently-added (diagnostics). |
| `--seed` | Record current favorites as a baseline **without** touching Lidarr. |
| `--once` | Run a single sync cycle and exit (verbose). |
| `--backfill` | Add **every** current favorite to Lidarr (the whole backlog). Slow. |

The loop auto-seeds on a fresh first run (so your existing favorites aren't
mass-imported); set `PROCESS_EXISTING=true` to backfill on first run instead.

## Configuration

All settings come from `data/config.env` (see `data/config.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `APPLEMUSIC_TEAM_ID` | — | Apple Developer Team ID (10 chars). |
| `APPLEMUSIC_KEY_ID` | — | MusicKit key id (10 chars). |
| `APPLEMUSIC_P8_PATH` | `/data/AuthKey.p8` | Path to the MusicKit private key. |
| `APPLEMUSIC_STOREFRONT` | `us` | Storefront (auto-detected if wrong). |
| `FAVORITES_PLAYLIST_ID` | — | Your Favorite Songs playlist id (from `--discover`). |
| `LIDARR_URL` | `http://172.16.238.58:8686` | Lidarr base URL. |
| `LIDARR_API_KEY` | — | Lidarr API key. |
| `LIDARR_QUALITY_PROFILE_ID` | `2` | Quality profile for added artists. |
| `LIDARR_METADATA_PROFILE_ID` | `1` | Metadata profile for added artists. |
| `LIDARR_ROOT_FOLDER` | `/music` | Lidarr root folder. |
| `LIDARR_MONITOR` | `none` | Monitor option when adding a new artist. |
| `POLL_INTERVAL` | `900` | Seconds between cycles. |
| `PROCESS_EXISTING` | `false` | Backfill the whole backlog on first run. |
| `UNMONITOR_ON_REMOVE` | `true` | Un-monitor an album when its song is un-favorited. |
| `MAX_REMOVALS_PER_CYCLE` | `25` | If more favorites than this vanish at once, skip un-monitoring (guards against a partial API read wiping your monitoring). |
| `NOTIFY_ON_ADD` | `true` | Send a success notification (naming the matched album) each time a favorite is handed to Lidarr. Backfill sends one summary instead of per-album pings. |
| `APPRISE_URL` | `http://172.16.238.45:8000` | Apprise base URL (optional). |
| `APPRISE_KEY` | `media` | Apprise config key for the token-expiry alert and the add notifications. |
| `MB_USER_AGENT` | `applemusic-lidarr/1.0 ( you@example.com )` | MusicBrainz User-Agent (set a real contact). |

> Default IPs are placeholders from the author's setup — point them at your own
> Lidarr/Apprise. Secrets (`AuthKey.p8`, `mut.txt`) and runtime state
> (`state.json`) live in `data/` and are gitignored; never commit them.

## When the token expires

The bridge alerts your Apprise endpoint. To renew: re-run `mint_mut.py` (step 3),
copy the new `mut.txt` into `data/`, and restart the container.

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The suite mocks all network calls, so no Apple/Lidarr/MusicBrainz access is
needed. CI runs them on every push (see `.github/workflows/tests.yml`).

## License

See [LICENSE](LICENSE).

## Acknowledgements

Inspired by [Soularr](https://github.com/mrusse/soularr) (Lidarr → Soulseek) and
[Lidify](https://github.com/TheWicklowWolf/Lidify) (Spotify → Lidarr). Album
metadata from [MusicBrainz](https://musicbrainz.org/).
