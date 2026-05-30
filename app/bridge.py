#!/usr/bin/env python3
"""Apple Music favorites -> Lidarr bridge.

Polls the user's "Favorite Songs" library playlist via the Apple Music API and,
for each newly favorited song, asks Lidarr to add the artist (if missing) and
monitor + search the album the song is from.

Modes:
  --discover   List library playlists (id, name, track count) so you can pin
               FAVORITES_PLAYLIST_ID in config.env.
  --probe      Dump the tail of the favorites playlist and of recently-added,
               so you can confirm whether a streaming-only favorite is visible.
  --seed       Record every current favorite as a baseline WITHOUT acting.
  --once       Run a single sync cycle (verbose), then exit.
  --backfill   Process EVERY current favorite (ignores the baseline) — adds the
               whole favorites backlog to Lidarr. Slow; run on demand.
  (default)    Loop forever, polling every POLL_INTERVAL seconds. Each cycle adds
               newly-favorited albums to Lidarr and un-monitors albums whose only
               favorited songs were un-favorited.
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone

import jwt  # PyJWT, with the `cryptography` extra for ES256
import requests

# --------------------------------------------------------------------------- #
# Configuration (all from environment / config.env)
# --------------------------------------------------------------------------- #

TEAM_ID = os.environ.get("APPLEMUSIC_TEAM_ID", "").strip()
KEY_ID = os.environ.get("APPLEMUSIC_KEY_ID", "").strip()
P8_PATH = os.environ.get("APPLEMUSIC_P8_PATH", "/data/AuthKey.p8").strip()
STOREFRONT = os.environ.get("APPLEMUSIC_STOREFRONT", "us").strip() or "us"
FAVORITES_PLAYLIST_ID = os.environ.get("FAVORITES_PLAYLIST_ID", "").strip()

LIDARR_URL = os.environ.get("LIDARR_URL", "http://172.16.238.58:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "").strip()
QUALITY_PROFILE_ID = int(os.environ.get("LIDARR_QUALITY_PROFILE_ID", "2"))
METADATA_PROFILE_ID = int(os.environ.get("LIDARR_METADATA_PROFILE_ID", "1"))
ROOT_FOLDER = os.environ.get("LIDARR_ROOT_FOLDER", "/music").strip()
MONITOR_OPTION = os.environ.get("LIDARR_MONITOR", "none").strip() or "none"

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))
PROCESS_EXISTING = os.environ.get("PROCESS_EXISTING", "false").lower() in ("1", "true", "yes")
UNMONITOR_ON_REMOVE = os.environ.get("UNMONITOR_ON_REMOVE", "true").lower() in ("1", "true", "yes")
MAX_REMOVALS_PER_CYCLE = int(os.environ.get("MAX_REMOVALS_PER_CYCLE", "25"))
NOTIFY_ON_ADD = os.environ.get("NOTIFY_ON_ADD", "true").lower() in ("1", "true", "yes")
NOTIFY_DIGEST = os.environ.get("NOTIFY_DIGEST", "true").lower() in ("1", "true", "yes")
UNRESOLVED_RETRY_INTERVAL = int(os.environ.get("UNRESOLVED_RETRY_INTERVAL", "86400"))
ACTIVITY_KEEP = int(os.environ.get("ACTIVITY_KEEP", "100"))
DEV_TOKEN_WARN_DAYS = int(os.environ.get("DEV_TOKEN_WARN_DAYS", "14"))

APPRISE_URL = os.environ.get("APPRISE_URL", "http://172.16.238.45:8000").rstrip("/")
APPRISE_KEY = os.environ.get("APPRISE_KEY", "media").strip()

WEB_ENABLED = os.environ.get("WEB_ENABLED", "true").lower() in ("1", "true", "yes")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))

MB_USER_AGENT = os.environ.get(
    "MB_USER_AGENT", "applemusic-lidarr/1.0 ( matthew.gromer@icloud.com )"
).strip()

STATE_DIR = os.environ.get("STATE_DIR", "/data").rstrip("/")
MUT_PATH = os.path.join(STATE_DIR, "mut.txt")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")  # legacy v1 state, migrated on first load
STATE_PATH = os.path.join(STATE_DIR, "state.json")
UNRESOLVED_PATH = os.path.join(STATE_DIR, "unresolved.json")
ACTIVITY_PATH = os.path.join(STATE_DIR, "activity.json")

AM_BASE = "https://api.music.apple.com"
MB_BASE = "https://musicbrainz.org/ws/2"
ALBUM_WAIT_TIMEOUT = 120  # seconds to wait for Lidarr to ingest a new artist's albums

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("aml")


class MUTExpired(Exception):
    """Raised when the Music User Token is missing, invalid, or expired."""


# --------------------------------------------------------------------------- #
# Apple Music: tokens
# --------------------------------------------------------------------------- #

_dev_token_cache = {"token": None, "exp": 0}


def developer_token():
    """Return a cached ES256 developer token, re-signing when near expiry."""
    now = int(time.time())
    if _dev_token_cache["token"] and _dev_token_cache["exp"] - now > 86400:
        return _dev_token_cache["token"]
    if not (TEAM_ID and KEY_ID):
        raise RuntimeError("APPLEMUSIC_TEAM_ID and APPLEMUSIC_KEY_ID must be set")
    try:
        with open(P8_PATH, "r") as fh:
            private_key = fh.read()
    except OSError as exc:
        raise RuntimeError(f"Cannot read MusicKit private key at {P8_PATH}: {exc}")
    exp = now + 15552000  # 180 days, under Apple's 6-month ceiling
    token = jwt.encode(
        {"iss": TEAM_ID, "iat": now, "exp": exp},
        private_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": KEY_ID},
    )
    _dev_token_cache.update(token=token, exp=exp)
    log.info("Signed a new developer token (valid ~180 days)")
    return token


def read_mut():
    try:
        with open(MUT_PATH, "r") as fh:
            mut = fh.read().strip()
        return mut or None
    except OSError:
        return None


def write_mut(token):
    """Atomically write a freshly-minted Music User Token to disk. The poll loop
    re-reads MUT_PATH every cycle, so no restart is needed to pick it up."""
    token = (token or "").strip()
    if not token:
        raise ValueError("refusing to write an empty Music User Token")
    tmp = MUT_PATH + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(token)
    os.replace(tmp, MUT_PATH)


def token_status():
    """Report whether the stored user token currently works: 'valid', 'expired',
    or 'missing'. Used by the web status endpoint."""
    if not read_mut():
        return "missing"
    try:
        am_get("/v1/me/storefront")
        return "valid"
    except MUTExpired:
        return "expired"
    except Exception:
        return "valid"  # a transient non-auth error doesn't mean the token is bad


def developer_token_status():
    """Snapshot of the ES256 developer token's expiry.

    Returns {exp_at, days_remaining, level} where `level` is 'ok',
    'warning' (< DEV_TOKEN_WARN_DAYS), or 'critical' (< 3 days). Calls
    developer_token() so the cache is populated even on first request.
    """
    try:
        developer_token()  # ensure the cache has a real exp value
    except Exception as exc:
        return {"exp_at": None, "days_remaining": None,
                "level": "unknown", "error": str(exc)}
    exp = int(_dev_token_cache.get("exp") or 0)
    if not exp:
        return {"exp_at": None, "days_remaining": None, "level": "unknown"}
    remaining = exp - int(time.time())
    days = remaining / 86400.0
    if days < 3:
        level = "critical"
    elif days < DEV_TOKEN_WARN_DAYS:
        level = "warning"
    else:
        level = "ok"
    exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(timespec="seconds")
    return {"exp_at": exp_iso, "days_remaining": round(days, 2), "level": level}


# --------------------------------------------------------------------------- #
# Apple Music: HTTP
# --------------------------------------------------------------------------- #


def am_get(path, params=None, user=True):
    """GET an Apple Music API path. Adds the user token when `user` is True.

    Raises MUTExpired on 401/403 for user requests. Retries on 429.
    """
    url = path if path.startswith("http") else f"{AM_BASE}{path}"
    headers = {"Authorization": f"Bearer {developer_token()}"}
    if user:
        mut = read_mut()
        if not mut:
            raise MUTExpired("No Music User Token on disk")
        headers["Music-User-Token"] = mut

    for attempt in range(5):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5)) or 5
            log.warning("Apple Music 429; backing off %ss", wait)
            time.sleep(min(wait, 60))
            continue
        if user and resp.status_code in (401, 403):
            raise MUTExpired(f"Apple Music returned {resp.status_code} for {path}")
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Apple Music kept throttling: {path}")


def am_paginate(path, params=None):
    """Yield every `data` item across all pages of a paginated endpoint."""
    params = dict(params or {})
    params.setdefault("limit", 100)
    while True:
        payload = am_get(path, params=params)
        for item in payload.get("data", []):
            yield item
        nxt = payload.get("next")
        if not nxt:
            break
        path, params = nxt, None  # `next` already carries the offset


def get_storefront():
    try:
        data = am_get("/v1/me/storefront").get("data", [])
        return data[0]["id"] if data else STOREFRONT
    except Exception:
        return STOREFRONT


# --------------------------------------------------------------------------- #
# Track -> (artist, album) extraction
# --------------------------------------------------------------------------- #


def track_identity(track):
    """Stable id for dedup: prefer the catalog id, fall back to library id."""
    attrs = track.get("attributes", {})
    play = attrs.get("playParams", {}) or {}
    return play.get("catalogId") or play.get("id") or track.get("id")


def track_artist_album(track):
    """Pull (artist, album) from a library/catalog song's attributes."""
    attrs = track.get("attributes", {})
    artist = (attrs.get("artistName") or "").strip()
    album = (attrs.get("albumName") or "").strip()
    name = (attrs.get("name") or "").strip()
    return artist, album, name


# --------------------------------------------------------------------------- #
# Lidarr
# --------------------------------------------------------------------------- #


def lidarr(method, path, params=None, body=None):
    url = f"{LIDARR_URL}/api/v1{path}"
    headers = {"X-Api-Key": LIDARR_API_KEY}
    resp = requests.request(method, url, headers=headers, params=params, json=body, timeout=60)
    resp.raise_for_status()
    if resp.text:
        return resp.json()
    return None


_NORM_RE = re.compile(r"[^a-z0-9]+")
_SUFFIX_RE = re.compile(r"\s*-\s*(single|ep)\s*$", re.IGNORECASE)


def _norm(text):
    text = _SUFFIX_RE.sub("", text or "")
    return _NORM_RE.sub("", text.lower())


def _match_score(candidate_title, candidate_artist, want_album, want_artist):
    ct, ca = _norm(candidate_title), _norm(candidate_artist)
    wt, wa = _norm(want_album), _norm(want_artist)
    if not ct or not wt:
        return 0
    artist_ok = bool(wa) and (ca == wa or wa in ca or ca in wa)
    if ct == wt and artist_ok:
        return 3
    if (wt in ct or ct in wt) and artist_ok:
        return 2
    if ct == wt:
        return 1
    return 0


_mb_last = [0.0]


def mb_get(path, params):
    """GET musicbrainz.org/ws/2, honoring the 1 request/second courtesy limit."""
    wait = 1.0 - (time.time() - _mb_last[0])
    if wait > 0:
        time.sleep(wait)
    p = dict(params)
    p["fmt"] = "json"
    for attempt in range(3):
        resp = requests.get(
            f"{MB_BASE}/{path}", params=p,
            headers={"User-Agent": MB_USER_AGENT}, timeout=30,
        )
        _mb_last[0] = time.time()
        if resp.status_code == 503:  # MB asks us to slow down
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("MusicBrainz kept returning 503")


def _mb_artist_name(rg):
    for credit in rg.get("artist-credit", []):
        artist = credit.get("artist") or {}
        if artist.get("name"):
            return artist["name"]
        if credit.get("name"):
            return credit["name"]
    return ""


def _lucene_clean(text):
    return re.sub(r'["\\]', " ", text or "").strip()


def _lidarr_album_by_mbid(rg_mbid):
    results = lidarr("GET", "/album/lookup", params={"term": f"lidarr:{rg_mbid}"}) or []
    return results[0] if results else None


def album_display_meta(album_lookup):
    """Extract (artist_slug, cover_url) from a Lidarr album lookup payload.

    Both may be None — the activity feed and deep-links degrade gracefully when
    Lidarr's response doesn't include them.
    """
    nested = album_lookup.get("artist") or {}
    slug = (nested.get("titleSlug") or "").strip() or None
    cover = next((img.get("remoteUrl") or img.get("url")
                  for img in (album_lookup.get("images") or [])
                  if img.get("coverType") == "cover"), None)
    return slug, cover


def _lidarr_text_lookup(artist, album):
    term = f"{artist} {album}".strip()
    try:
        candidates = lidarr("GET", "/album/lookup", params={"term": term}) or []
    except requests.HTTPError as exc:
        log.warning("Lidarr album lookup failed for %r: %s", term, exc)
        return None
    best, best_score = None, 0
    for cand in candidates:
        score = _match_score(
            cand.get("title", ""),
            (cand.get("artist") or {}).get("artistName", ""),
            album, artist,
        )
        if score > best_score:
            best, best_score = cand, score
    return best if best_score > 0 else None


# Result of resolve_album. `album` is a Lidarr album lookup dict when the match
# is confident enough to add automatically; otherwise None. `score` is the best
# 0..3 MusicBrainz score we saw (3=title+artist exact, 2=substring+artist,
# 1=title-only). `candidate` is non-None when score==1: a {foreignAlbumId,
# title, artist} suggestion the dashboard can offer as "Apply suggested match",
# turning a silent miss into a one-click confirmation.
Resolution = namedtuple("Resolution", ["album", "score", "candidate"])


def resolve_album(artist, album):
    """Resolve a favorited song's album to an addable Lidarr album resource.

    MusicBrainz first (precise release-group match by title + artist), then ask
    Lidarr for that exact release-group id. Falls back to Lidarr's own loose
    text lookup if MusicBrainz finds nothing — which also covers the rare case
    where MusicBrainz is unreachable.

    Returns a Resolution. The dashboard uses `.candidate` (when set) to render
    a one-click "Apply suggested match" button on the unresolved entry.
    """
    album_q = _SUFFIX_RE.sub("", album).strip()
    candidate = None
    best_score = 0
    try:
        query = f'releasegroup:"{_lucene_clean(album_q)}" AND artist:"{_lucene_clean(artist)}"'
        data = mb_get("release-group", {"query": query, "limit": 10})
        best = None
        for rg in data.get("release-groups", []):
            score = _match_score(rg.get("title", ""), _mb_artist_name(rg), album, artist)
            if score > best_score:
                best, best_score = rg, score
        if best and best_score >= 2:
            album_resource = _lidarr_album_by_mbid(best["id"])
            if album_resource:
                return Resolution(album_resource, best_score, None)
        if best and best_score == 1:
            candidate = {
                "foreignAlbumId": best.get("id"),
                "title": best.get("title") or album,
                "artist": _mb_artist_name(best) or artist,
            }
    except Exception as exc:
        log.warning("MusicBrainz lookup failed for %s — %s: %s", artist, album, exc)
    fallback = _lidarr_text_lookup(artist, album)
    if fallback:
        return Resolution(fallback, max(best_score, 2), None)
    return Resolution(None, best_score, candidate)


def find_existing_artist(artist_mbid):
    for art in lidarr("GET", "/artist") or []:
        if art.get("foreignArtistId") == artist_mbid:
            return art
    return None


def build_artist_add_fields(artist_obj):
    artist_obj = dict(artist_obj)
    artist_obj["qualityProfileId"] = QUALITY_PROFILE_ID
    artist_obj["metadataProfileId"] = METADATA_PROFILE_ID
    artist_obj["rootFolderPath"] = ROOT_FOLDER
    artist_obj["monitored"] = False
    artist_obj["addOptions"] = {"monitor": MONITOR_OPTION, "searchForMissingAlbums": False}
    return artist_obj


def ensure_artist(artist_mbid, fallback_artist_obj):
    """Return (artist, was_new): the Lidarr artist for this MBID, adding it
    (monitor: none) if absent."""
    existing = find_existing_artist(artist_mbid)
    if existing:
        return existing, False
    results = lidarr("GET", "/artist/lookup", params={"term": f"lidarr:{artist_mbid}"}) or []
    artist_obj = results[0] if results else dict(fallback_artist_obj)
    created = lidarr("POST", "/artist", body=build_artist_add_fields(artist_obj))
    return created, True


def wait_for_artist_ready(artist_id, rg_mbid, deadline):
    """Wait until the add-triggered RefreshArtist finishes and the album exists.

    Adding an artist queues a RefreshArtist that ingests the discography and
    (re)applies the artist's monitor option to every album. If we set album
    monitoring before that finishes, the refresh clobbers it. RefreshArtist
    commands report a null artistId, so we just wait until none are active.
    """
    time.sleep(4)  # give Lidarr a moment to queue the refresh
    while time.time() < deadline:
        cmds = lidarr("GET", "/command") or []
        refreshing = any(
            c.get("name") == "RefreshArtist" and c.get("status") in ("queued", "started")
            for c in cmds
        )
        albums = lidarr("GET", "/album", params={"artistId": artist_id}) or []
        present = any(a.get("foreignAlbumId") == rg_mbid for a in albums)
        if present and not refreshing:
            return
        time.sleep(3)


def ensure_album_in_lidarr(album_lookup):
    """Add the artist if needed, then monitor + search the album.

    Adds the artist first (well-understood path), then locates the album among
    the artist's albums (Lidarr ingests them asynchronously) and monitors +
    searches it. If the album never appears (e.g. excluded by the metadata
    profile), it is added explicitly. Returns a short status string.
    """
    rg_mbid = album_lookup["foreignAlbumId"]
    nested_artist = album_lookup.get("artist") or {}
    artist_mbid = nested_artist.get("foreignArtistId")
    title = album_lookup.get("title", "?")
    artist_name = nested_artist.get("artistName", "?")
    if not artist_mbid:
        raise RuntimeError("album match had no artist MusicBrainz id")

    artist, was_new = ensure_artist(artist_mbid, nested_artist)
    artist_id = artist["id"]
    deadline = time.time() + ALBUM_WAIT_TIMEOUT

    # For a new artist, let the discography refresh settle first (it resets
    # album monitoring), otherwise our monitoring gets clobbered.
    if was_new:
        wait_for_artist_ready(artist_id, rg_mbid, deadline)

    # Find the album and make monitoring stick. Re-find by release-group id each
    # pass (ids can change during refresh) and re-apply until it persists.
    album_id = None
    monitored_ok = False
    while time.time() < deadline:
        match = next(
            (a for a in (lidarr("GET", "/album", params={"artistId": artist_id}) or [])
             if a.get("foreignAlbumId") == rg_mbid),
            None,
        )
        if match:
            album_id = match["id"]
            if match.get("monitored"):
                monitored_ok = True
                break
            lidarr("PUT", "/album/monitor", body={"albumIds": [album_id], "monitored": True})
            time.sleep(2)
        else:
            time.sleep(3)

    if album_id is None:
        # Album never appeared under the artist (excluded by metadata profile,
        # or refresh failed): add it explicitly.
        payload = dict(album_lookup)
        payload["artistId"] = artist_id
        payload["artist"] = artist
        payload["monitored"] = True
        payload["addOptions"] = {"searchForNewAlbum": True}
        lidarr("POST", "/album", body=payload)
        return f"added + searched '{title}' by {artist_name}"

    if not monitored_ok:
        lidarr("PUT", "/album/monitor", body={"albumIds": [album_id], "monitored": True})
        log.warning("Monitoring for '%s' did not confirm in time; applied once more", title)
    lidarr("POST", "/command", body={"name": "AlbumSearch", "albumIds": [album_id]})
    return f"monitored + searched '{title}' by {artist_name}"


def unmonitor_album(rg_mbid, artist_mbid):
    """Set an album back to unmonitored in Lidarr. Never deletes files or the
    artist — un-favoriting only stops Lidarr from chasing the album."""
    artist = find_existing_artist(artist_mbid) if artist_mbid else None
    if not artist:
        return f"artist not in Lidarr; nothing to un-monitor ({rg_mbid})"
    for alb in lidarr("GET", "/album", params={"artistId": artist["id"]}) or []:
        if alb.get("foreignAlbumId") == rg_mbid:
            if alb.get("monitored"):
                lidarr("PUT", "/album/monitor",
                       body={"albumIds": [alb["id"]], "monitored": False})
                return f"un-monitored '{alb.get('title')}' by {artist.get('artistName')}"
            return f"'{alb.get('title')}' was already un-monitored"
    return f"album not found under {artist.get('artistName')} ({rg_mbid})"


# --------------------------------------------------------------------------- #
# Apprise
# --------------------------------------------------------------------------- #


def alert(title, body, notify_type="warning"):
    try:
        requests.post(
            f"{APPRISE_URL}/notify/{APPRISE_KEY}",
            json={"title": title, "body": body, "type": notify_type},
            timeout=15,
        )
        log.info("Sent Apprise alert: %s", title)
    except Exception as exc:  # alerts must never crash the loop
        log.warning("Failed to send Apprise alert: %s", exc)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

# Guards every read-modify-write of state.json / unresolved.json so the poll
# loop and the web UI (which run in the same process, different threads) can't
# clobber each other. Callers hold it around the whole load->modify->save.
_STATE_LOCK = threading.RLock()

# Snapshot of the last completed loop cycle, surfaced by the web status endpoint.
_last_cycle = {"at": None, "added": 0, "removed": 0}


def load_state():
    """Return {"favorites": {track_id: {"rg": rg_mbid|None, "aid": artist_mbid|None}}}.

    `favorites` holds the favorites present as of the last sync. A track whose
    `rg` is set was added to Lidarr by us (so it can be un-monitored if removed);
    `rg` of None means baselined/unresolved (nothing of ours to undo).
    """
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as fh:
                data = json.load(fh)
            data.setdefault("favorites", {})
            return data
        except (OSError, ValueError):
            log.warning("state.json unreadable; starting fresh")
    # Migrate legacy v1 seen.json (a flat list of ids) into the new shape.
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH) as fh:
                ids = json.load(fh)
            log.info("Migrating %d ids from seen.json into state.json", len(ids))
            return {"favorites": {tid: {"rg": None, "aid": None} for tid in ids}}
        except (OSError, ValueError):
            pass
    return {"favorites": {}}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def load_unresolved():
    """Return the list of songs the bridge couldn't confidently match."""
    if not os.path.exists(UNRESOLVED_PATH):
        return []
    try:
        with open(UNRESOLVED_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_unresolved(entries):
    tmp = UNRESOLVED_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(entries, fh, indent=2)
    os.replace(tmp, UNRESOLVED_PATH)


def record_unresolved(entry):
    try:
        with _STATE_LOCK:
            existing = load_unresolved()
            existing.append(entry)
            save_unresolved(existing)
    except Exception as exc:
        log.warning("Could not record unresolved favorite: %s", exc)


def remove_unresolved(track_id):
    """Drop the unresolved entry for a track id. Returns True if one was removed."""
    with _STATE_LOCK:
        entries = load_unresolved()
        kept = [e for e in entries if e.get("id") != track_id]
        if len(kept) == len(entries):
            return False
        save_unresolved(kept)
        return True


# --------------------------------------------------------------------------- #
# Activity feed (ring buffer of recent loop/web actions)
# --------------------------------------------------------------------------- #


def load_activity():
    """Return the activity log (newest first), capped at ACTIVITY_KEEP."""
    if not os.path.exists(ACTIVITY_PATH):
        return []
    try:
        with open(ACTIVITY_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_activity(entries):
    tmp = ACTIVITY_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(entries, fh)
    os.replace(tmp, ACTIVITY_PATH)


def append_activity(entry):
    """Prepend an activity entry to the ring buffer, capped at ACTIVITY_KEEP.

    Entry shape: {at, kind, artist, album, status?, slug?, cover?}. `kind` is
    one of 'add', 'remove', 'auto-retry-match', 'auto-retry-no-match'. Failures
    are swallowed — the activity log is best-effort and must not crash a cycle.
    """
    try:
        entry = dict(entry)
        entry.setdefault("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with _STATE_LOCK:
            entries = load_activity()
            entries.insert(0, entry)
            del entries[ACTIVITY_KEEP:]
            _save_activity(entries)
    except Exception as exc:
        log.warning("Could not record activity entry: %s", exc)


# --------------------------------------------------------------------------- #
# Notification digest + auto-retry sweep state
# --------------------------------------------------------------------------- #

# Per-cycle list of handoffs that should be summarized into one Apprise message
# when the loop finishes a sync. Drained by _drain_pending_handoffs(). When
# NOTIFY_DIGEST is false, process_one sends per-handoff alerts and leaves this
# empty.
_pending_handoffs = []

# Epoch seconds the auto-retry sweep last ran (0 = never). Gated by
# UNRESOLVED_RETRY_INTERVAL so cmd_loop only walks unresolved entries
# occasionally, not every cycle.
_last_retry_at = 0.0

# The dev-token exp value we last sent a "renew soon" Apprise warning for.
# Reset implicitly whenever developer_token() re-signs (the exp changes), so a
# new warning fires on the next near-expiry window.
_dev_warn_sent_for_exp = 0


def _queue_handoff(artist, album, status):
    """Buffer a successful handoff for the cycle-end digest. Holds the lock so
    concurrent web-driven adds don't race the loop's drain."""
    with _STATE_LOCK:
        _pending_handoffs.append({"artist": artist, "album": album, "status": status})


def _drain_pending_handoffs():
    """If any handoffs were buffered this cycle, send a single combined Apprise
    success message and clear the buffer."""
    if not (NOTIFY_ON_ADD and NOTIFY_DIGEST):
        return
    with _STATE_LOCK:
        pending = list(_pending_handoffs)
        _pending_handoffs.clear()
    if not pending:
        return
    n = len(pending)
    lines = [f'• "{p["album"] or "?"}" by {p["artist"] or "?"} — {p["status"]}'
             for p in pending]
    body = f"Handed {n} favorited album{'s' if n != 1 else ''} to Lidarr:\n" + "\n".join(lines)
    alert("Apple Music → Lidarr", body, notify_type="success")


def _apply_resolution(track_id, album_lookup):
    """Add/monitor an album in Lidarr, upgrade the saved favorite from a
    {rg:None} baseline to the real ids, drop the unresolved entry, and record
    an 'add' activity entry. Returns the Lidarr status string.

    The network call runs outside the state lock; only the state writes are
    guarded. Notification (per-handoff or digest) is the caller's job — this
    helper is shared by the web UI (silent: user sees the result on screen)
    and the auto-retry sweep (which DOES notify, via _queue_handoff)."""
    status = ensure_album_in_lidarr(album_lookup)
    slug, cover = album_display_meta(album_lookup)
    with _STATE_LOCK:
        state = load_state()
        state["favorites"][track_id] = {
            "rg": album_lookup.get("foreignAlbumId"),
            "aid": (album_lookup.get("artist") or {}).get("foreignArtistId"),
            "slug": slug,
        }
        save_state(state)
        remove_unresolved(track_id)
    append_activity({
        "kind": "add",
        "artist": (album_lookup.get("artist") or {}).get("artistName"),
        "album": album_lookup.get("title"),
        "status": status,
        "slug": slug,
        "cover": cover,
    })
    return status


# --------------------------------------------------------------------------- #
# Core flow
# --------------------------------------------------------------------------- #


def favorite_tracks():
    if not FAVORITES_PLAYLIST_ID:
        raise RuntimeError("FAVORITES_PLAYLIST_ID is not set — run --discover first")
    path = f"/v1/me/library/playlists/{FAVORITES_PLAYLIST_ID}/tracks"
    return list(am_paginate(path))


def process_one(track, notify=True):
    """Resolve a favorited track and add it to Lidarr.

    Returns {"rg": rg_mbid, "aid": artist_mbid} on success, or {"rg": None,
    "aid": None} if it had no usable metadata or couldn't be matched. When
    `notify` and NOTIFY_ON_ADD are both set, a success alert naming the matched
    album is sent on handoff (backfill passes notify=False to avoid a flood and
    sends one summary instead).
    """
    artist, album, name = track_artist_album(track)
    label = f"{artist} — {album or name}"
    tid = track_identity(track)

    if not artist or not album:
        log.warning("Skipping (no artist/album metadata): %s", name or tid)
        record_unresolved({"id": tid, "name": name, "artist": artist,
                           "album": album, "reason": "missing metadata"})
        return {"rg": None, "aid": None}

    try:
        res = resolve_album(artist, album)
        if not res.album:
            reason = "low confidence" if res.candidate else "no match"
            log.warning("No confident match for: %s (%s)", label, reason)
            entry = {"id": tid, "name": name, "artist": artist,
                     "album": album, "reason": reason}
            if res.candidate:
                entry["suggestion"] = res.candidate
            record_unresolved(entry)
            return {"rg": None, "aid": None}
        match = res.album
        status = ensure_album_in_lidarr(match)
        log.info("%s -> %s", label, status)
        slug, cover = album_display_meta(match)
        append_activity({
            "kind": "add", "artist": artist, "album": album,
            "status": status, "slug": slug, "cover": cover,
        })
        if notify and NOTIFY_ON_ADD:
            if NOTIFY_DIGEST:
                _queue_handoff(artist, album, status)
            else:
                alert(
                    "Apple Music → Lidarr",
                    f'Favorited "{name or album}" — {status}.',
                    notify_type="success",
                )
        return {"rg": match.get("foreignAlbumId"),
                "aid": (match.get("artist") or {}).get("foreignArtistId"),
                "slug": slug}
    except MUTExpired:
        raise
    except Exception as exc:
        log.error("Failed to process %s: %s", label, exc)
        record_unresolved({"id": tid, "name": name, "artist": artist,
                           "album": album, "reason": str(exc)})
        return {"rg": None, "aid": None}


def sync_favorites(act=True):
    """Lock-guarded wrapper so the poll loop and web UI don't race on state.json."""
    with _STATE_LOCK:
        return _sync_favorites_unlocked(act)


def _sync_favorites_unlocked(act=True):
    """Diff the current favorites against saved state; add new, un-monitor removed.

    Returns (added, added_ok, removed, removed_done). With act=False it only
    records the current favorites as a baseline (no Lidarr changes).
    """
    state = load_state()
    favorites = state["favorites"]
    current = {tid: t for tid, t in
               ((track_identity(t), t) for t in favorite_tracks()) if tid}
    current_ids = set(current)
    prev_ids = set(favorites)
    added_ids = current_ids - prev_ids
    removed_ids = prev_ids - current_ids

    added_ok = 0
    for tid in added_ids:
        if not act:
            favorites[tid] = {"rg": None, "aid": None}
            continue
        entry = process_one(current[tid])
        favorites[tid] = entry
        if entry.get("rg"):
            added_ok += 1

    removed_done = 0
    if not act:
        for tid in removed_ids:
            favorites.pop(tid, None)
    elif removed_ids:
        if not UNMONITOR_ON_REMOVE:
            for tid in removed_ids:
                favorites.pop(tid, None)
        elif len(removed_ids) > MAX_REMOVALS_PER_CYCLE:
            log.warning(
                "%d favorites vanished this cycle (> MAX_REMOVALS_PER_CYCLE=%d) — "
                "treating it as an incomplete read, NOT un-monitoring anything. "
                "Raise the limit if this was a deliberate bulk cleanup.",
                len(removed_ids), MAX_REMOVALS_PER_CYCLE,
            )
        else:
            for tid in removed_ids:
                info = favorites.pop(tid, {})
                rg = info.get("rg")
                if not rg:
                    continue
                if any(v.get("rg") == rg for v in favorites.values()):
                    log.info("A song was un-favorited but another favorite still "
                             "maps to that album; leaving it monitored")
                    continue
                try:
                    status = unmonitor_album(rg, info.get("aid"))
                    log.info("Un-favorited -> %s", status)
                    append_activity({
                        "kind": "remove",
                        "artist": None, "album": None,
                        "status": status, "slug": info.get("slug"),
                    })
                    removed_done += 1
                except Exception as exc:
                    log.error("Failed to un-monitor %s: %s", rg, exc)

    save_state(state)
    return len(added_ids), added_ok, len(removed_ids), removed_done


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def cmd_discover():
    print(f"Storefront: {get_storefront()}\n")
    print("Library playlists (pin the Favorite Songs one as FAVORITES_PLAYLIST_ID):\n")
    for pl in am_paginate("/v1/me/library/playlists"):
        attrs = pl.get("attributes", {})
        name = attrs.get("name", "(unnamed)")
        flag = "  <-- likely FAVORITES" if "favorite" in name.lower() or "favourite" in name.lower() else ""
        print(f"  id={pl.get('id')}  name={name!r}{flag}")


def cmd_probe():
    print(f"Storefront: {get_storefront()}")
    if FAVORITES_PLAYLIST_ID:
        tracks = favorite_tracks()
        print(f"\nFavorite Songs playlist has {len(tracks)} tracks. Last 15:")
        for t in tracks[-15:]:
            a, al, n = track_artist_album(t)
            print(f"  - {n}  |  {a}  |  {al}  |  id={track_identity(t)}")
    else:
        print("\nFAVORITES_PLAYLIST_ID not set; skipping playlist dump.")
    print("\nLibrary recently-added (top 15):")
    count = 0
    for item in am_paginate("/v1/me/library/recently-added", params={"limit": 25}):
        a, al, n = track_artist_album(item)
        kind = item.get("type")
        print(f"  - [{kind}] {n or al}  |  {a}  |  {al}")
        count += 1
        if count >= 15:
            break


def cmd_seed():
    a, _, r, _ = sync_favorites(act=False)
    total = len(load_state()["favorites"])
    log.info("Baseline updated (+%d, -%d); tracking %d favorites, no Lidarr changes.", a, r, total)


def cmd_once():
    a, aok, r, rdone = sync_favorites(act=True)
    log.info("Cycle: +%d new (%d added to Lidarr), -%d removed (%d un-monitored)", a, aok, r, rdone)


def cmd_backfill():
    """Process EVERY current favorite regardless of baseline — adds the whole
    backlog to Lidarr. Slow (paced by MusicBrainz + Lidarr refresh waits)."""
    with _STATE_LOCK:
        return _cmd_backfill_unlocked()


def _cmd_backfill_unlocked():
    state = load_state()
    favorites = state["favorites"]
    tracks = favorite_tracks()
    total = len(tracks)
    log.info("Backfill: processing all %d favorites — slow, and adds a lot to Lidarr.", total)
    done = added = 0
    for track in tracks:
        tid = track_identity(track)
        if not tid:
            continue
        done += 1
        entry = process_one(track, notify=False)
        favorites[tid] = entry
        if entry.get("rg"):
            added += 1
        if done % 25 == 0:
            save_state(state)
            log.info("Backfill progress: %d/%d (%d added so far)", done, total, added)
    save_state(state)
    log.info("Backfill complete: %d processed, %d added to Lidarr.", done, added)
    if NOTIFY_ON_ADD and added:
        alert(
            "Apple Music → Lidarr",
            f"Backfill complete: handed {added} of {done} favorited albums to Lidarr.",
            notify_type="success",
        )


def _auto_retry_unresolved():
    """Re-resolve every unresolved entry against MusicBrainz/Lidarr.

    Many "no match" entries are just *not catalogued yet* — MusicBrainz fills
    in over days. This sweep replays the resolve for each entry; on a hit the
    album is added to Lidarr, the favorite's baseline is upgraded with the
    real ids, the unresolved entry is dropped, and the handoff is queued for
    the cycle's digest.

    Lookups happen outside the state lock so MB's 1-req/s pacing doesn't block
    web requests. Bounded by however many entries you have — a 50-entry sweep
    takes ~50s. Skipped entries (missing artist/album) stay put.
    """
    entries = load_unresolved()
    if not entries:
        return 0, 0
    log.info("Auto-retry sweep: %d unresolved entr%s",
             len(entries), "y" if len(entries) == 1 else "ies")
    matched = stayed = 0
    for entry in entries:
        tid = entry.get("id")
        artist, album = entry.get("artist"), entry.get("album")
        if not (tid and artist and album):
            continue
        try:
            res = resolve_album(artist, album)
        except MUTExpired:
            raise
        except Exception as exc:
            log.warning("Auto-retry lookup failed for %s — %s: %s", artist, album, exc)
            res = Resolution(None, 0, None)
        if not res.album:
            stayed += 1
            append_activity({"kind": "auto-retry-no-match",
                             "artist": artist, "album": album})
            continue
        try:
            status = _apply_resolution(tid, res.album)
        except MUTExpired:
            raise
        except Exception as exc:
            log.error("Auto-retry apply failed for %s — %s: %s", artist, album, exc)
            continue
        log.info("Auto-retry matched: %s — %s -> %s", artist, album, status)
        matched += 1
        append_activity({"kind": "auto-retry-match",
                         "artist": artist, "album": album, "status": status})
        if NOTIFY_ON_ADD and NOTIFY_DIGEST:
            _queue_handoff(artist, album, status)
        elif NOTIFY_ON_ADD:
            alert("Apple Music → Lidarr",
                  f'Auto-retry matched "{album}" by {artist} — {status}.',
                  notify_type="success")
    return matched, stayed


def _maybe_warn_dev_token_expiry():
    """Fire a one-shot Apprise warning when the developer token drops below
    DEV_TOKEN_WARN_DAYS. Re-arms when developer_token() re-signs (the cached
    exp changes), so a subsequent warning fires for the next near-expiry."""
    global _dev_warn_sent_for_exp
    try:
        info = developer_token_status()
    except Exception:
        return
    exp_at = info.get("exp_at")
    days = info.get("days_remaining")
    if info.get("level") not in ("warning", "critical") or days is None or not exp_at:
        return
    current_exp = int(_dev_token_cache.get("exp") or 0)
    if current_exp == _dev_warn_sent_for_exp:
        return  # already warned for this signing
    alert(
        "Apple Music developer token expiring",
        f"The MusicKit developer token expires in ~{days:.1f} day(s) "
        f"({exp_at}). Re-sign by restarting the container with the .p8 in "
        f"place (developer_token() re-signs automatically on next use).",
        notify_type="warning",
    )
    _dev_warn_sent_for_exp = current_exp


def cmd_loop():
    fresh = not os.path.exists(STATE_PATH) and not os.path.exists(SEEN_PATH)
    if fresh:
        if PROCESS_EXISTING:
            log.info("First run, PROCESS_EXISTING=true: backfilling the whole favorites list")
            cmd_backfill()
        else:
            sync_favorites(act=False)
            log.info("First run: baselined existing favorites; acting on changes only.")

    alerted = False
    global _last_retry_at
    while True:
        try:
            a, aok, r, rdone = sync_favorites(act=True)
            _last_cycle.update(
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                added=a, removed=r)
            if a or r:
                log.info("Cycle: +%d new (%d added), -%d removed (%d un-monitored)", a, aok, r, rdone)
            alerted = False

            # Auto-retry sweep: walk unresolved entries once per
            # UNRESOLVED_RETRY_INTERVAL and clear any that now match.
            now = time.time()
            if UNRESOLVED_RETRY_INTERVAL > 0 and now - _last_retry_at >= UNRESOLVED_RETRY_INTERVAL:
                try:
                    _auto_retry_unresolved()
                finally:
                    _last_retry_at = now

            _drain_pending_handoffs()
            _maybe_warn_dev_token_expiry()
        except MUTExpired as exc:
            log.error("Music User Token problem: %s", exc)
            if not alerted:
                alert(
                    "Apple Music token expired",
                    "The Apple Music favorites bridge can no longer read your "
                    "favorites. Open the web UI and click 'Renew sign-in token' "
                    "(no restart needed), or re-run mint_mut.py as a fallback.",
                )
                alerted = True
        except Exception as exc:
            log.error("Cycle failed: %s", exc)
        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Apple Music favorites -> Lidarr bridge")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--discover", action="store_true", help="list library playlists")
    group.add_argument("--probe", action="store_true", help="dump favorites + recently-added")
    group.add_argument("--seed", action="store_true", help="baseline current favorites, no actions")
    group.add_argument("--once", action="store_true", help="run a single sync cycle")
    group.add_argument("--backfill", action="store_true", help="add ALL current favorites to Lidarr")
    args = parser.parse_args()

    if not LIDARR_API_KEY and not (args.discover or args.probe):
        log.error("LIDARR_API_KEY is not set")
        sys.exit(1)

    try:
        if args.discover:
            cmd_discover()
        elif args.probe:
            cmd_probe()
        elif args.seed:
            cmd_seed()
        elif args.once:
            cmd_once()
        elif args.backfill:
            cmd_backfill()
        elif WEB_ENABLED:
            # Default mode with the web UI: poll loop in a daemon thread, Flask
            # in the main thread. Both share /data, guarded by _STATE_LOCK.
            import web  # local import so CLI one-shots don't need Flask installed

            # Ensure web talks to THIS module instance, not a second copy (only
            # an issue when bridge.py is run directly as __main__; harmless via
            # run.py, where it's already the same object).
            web.bridge = sys.modules[__name__]
            threading.Thread(target=cmd_loop, name="poll-loop", daemon=True).start()
            log.info("Starting web UI on port %d", WEB_PORT)
            web.app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)
        else:
            cmd_loop()
    except MUTExpired as exc:
        log.error("Music User Token problem: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
