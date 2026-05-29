#!/usr/bin/env python3
"""Flask web UI for the Apple Music -> Lidarr bridge.

Two jobs:
  - Renew the Music User Token from any browser (no terminal, no restart): the
    page runs Apple's MusicKit JS, you sign in, and the captured token is written
    to /data/mut.txt. The poll loop re-reads it next cycle.
  - Repair songs the bridge couldn't confidently match: search Lidarr and apply
    the right album, dismiss an entry, or retry the automatic match.

Served by the bridge process itself (see bridge.main): the poll loop runs in a
daemon thread and this Flask app in the main thread, sharing /data guarded by
bridge._STATE_LOCK. Intended for internal LAN use only (no auth).
"""

import logging

from flask import Flask, jsonify, render_template, request

import bridge

log = logging.getLogger("aml.web")

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/token")
def token_page():
    return render_template("token.html", dev_token=bridge.developer_token())


# --------------------------------------------------------------------------- #
# Status + token
# --------------------------------------------------------------------------- #


@app.get("/api/status")
def api_status():
    state = bridge.load_state()
    return jsonify({
        "token": bridge.token_status(),
        "last_cycle": bridge._last_cycle,
        "favorites": len(state.get("favorites", {})),
        "unresolved": len(bridge.load_unresolved()),
        "poll_interval": bridge.POLL_INTERVAL,
    })


@app.post("/api/token")
def api_token():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "no token supplied"}), 400
    try:
        bridge.write_mut(token)
    except Exception as exc:
        log.error("Failed to write token: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    log.info("Music User Token updated via web UI")
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# No-match repair
# --------------------------------------------------------------------------- #


@app.get("/api/unresolved")
def api_unresolved():
    return jsonify(bridge.load_unresolved())


@app.get("/api/lidarr-search")
def api_lidarr_search():
    term = (request.args.get("term") or "").strip()
    if not term:
        return jsonify([])
    try:
        candidates = bridge.lidarr("GET", "/album/lookup", params={"term": term}) or []
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    out = []
    for c in candidates[:20]:
        art = c.get("artist") or {}
        cover = next((img.get("remoteUrl") or img.get("url")
                      for img in (c.get("images") or [])
                      if img.get("coverType") == "cover"), None)
        out.append({
            "foreignAlbumId": c.get("foreignAlbumId"),
            "title": c.get("title"),
            "artist": art.get("artistName"),
            "albumType": c.get("albumType"),
            "year": (c.get("releaseDate") or "")[:4],
            "cover": cover,
        })
    return jsonify(out)


def _apply_resolution(track_id, album_lookup):
    """Add/monitor the album in Lidarr, upgrade the saved favorite from a
    {rg:None} baseline to the real ids, and drop the unresolved entry. Returns
    the Lidarr status string. The network call runs outside the state lock; only
    the state write is guarded."""
    status = bridge.ensure_album_in_lidarr(album_lookup)
    with bridge._STATE_LOCK:
        state = bridge.load_state()
        state["favorites"][track_id] = {
            "rg": album_lookup.get("foreignAlbumId"),
            "aid": (album_lookup.get("artist") or {}).get("foreignArtistId"),
        }
        bridge.save_state(state)
        bridge.remove_unresolved(track_id)
    return status


@app.post("/api/resolve")
def api_resolve():
    data = request.get_json(silent=True) or {}
    track_id = data.get("trackId")
    mbid = data.get("foreignAlbumId")
    if not track_id or not mbid:
        return jsonify({"ok": False, "error": "trackId and foreignAlbumId required"}), 400
    album = bridge._lidarr_album_by_mbid(mbid)
    if not album:
        return jsonify({"ok": False, "error": "album not found via Lidarr lookup"}), 404
    try:
        status = _apply_resolution(track_id, album)
    except Exception as exc:
        log.error("Resolve failed for %s: %s", track_id, exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "status": status})


@app.post("/api/dismiss")
def api_dismiss():
    data = request.get_json(silent=True) or {}
    track_id = data.get("trackId")
    if not track_id:
        return jsonify({"ok": False, "error": "trackId required"}), 400
    with bridge._STATE_LOCK:
        state = bridge.load_state()
        state["favorites"].setdefault(track_id, {"rg": None, "aid": None})
        bridge.save_state(state)
        removed = bridge.remove_unresolved(track_id)
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/retry")
def api_retry():
    data = request.get_json(silent=True) or {}
    track_id = data.get("trackId")
    if not track_id:
        return jsonify({"ok": False, "error": "trackId required"}), 400
    entry = next((e for e in bridge.load_unresolved() if e.get("id") == track_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "no such unresolved entry"}), 404
    artist, album = entry.get("artist"), entry.get("album")
    if not artist or not album:
        return jsonify({"ok": True, "matched": False,
                        "error": "entry has no artist/album to search on"})
    try:
        match = bridge.resolve_album(artist, album)
        if not match:
            return jsonify({"ok": True, "matched": False})
        status = _apply_resolution(track_id, match)
    except Exception as exc:
        log.error("Retry failed for %s: %s", track_id, exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "matched": True, "status": status})
