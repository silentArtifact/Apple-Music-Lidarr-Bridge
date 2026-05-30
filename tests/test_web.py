"""Unit tests for the Flask web UI (token renewal + no-match repair).

All network access (Apple Music, Lidarr, MusicBrainz) is mocked and state files
are redirected into a temp dir, so the suite runs offline. Run with the rest:
    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import bridge  # noqa: E402
import web  # noqa: E402  (web does `import bridge` -> same module instance)


class WebTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = web.app.test_client()
        self._patches = [
            mock.patch.object(bridge, "STATE_PATH", os.path.join(self.tmp, "state.json")),
            mock.patch.object(bridge, "SEEN_PATH", os.path.join(self.tmp, "seen.json")),
            mock.patch.object(bridge, "UNRESOLVED_PATH", os.path.join(self.tmp, "unresolved.json")),
            mock.patch.object(bridge, "ACTIVITY_PATH", os.path.join(self.tmp, "activity.json")),
            mock.patch.object(bridge, "MUT_PATH", os.path.join(self.tmp, "mut.txt")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_state(self, favorites):
        bridge.save_state({"favorites": favorites})

    def seed_unresolved(self, entries):
        bridge.save_unresolved(entries)

    def read_state(self):
        return bridge.load_state()["favorites"]


class TestStatusEndpoint(WebTestBase):
    def test_reports_counts_and_token(self):
        self.seed_state({"a": {"rg": "r", "aid": "x"}, "b": {"rg": None, "aid": None}})
        self.seed_unresolved([{"id": "b", "artist": "A", "album": "Alb", "reason": "no match"}])
        with mock.patch.object(bridge, "token_status", return_value="valid"), \
             mock.patch.object(bridge, "developer_token_status",
                               return_value={"exp_at": "2026-11-26T00:00:00+00:00",
                                             "days_remaining": 180.0, "level": "ok"}):
            r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["token"], "valid")
        self.assertEqual(body["favorites"], 2)
        self.assertEqual(body["unresolved"], 1)
        self.assertEqual(body["developer_token"]["level"], "ok")
        self.assertIn("lidarr_url", body)


class TestTokenStatus(unittest.TestCase):
    def test_missing_when_no_mut(self):
        with mock.patch.object(bridge, "read_mut", return_value=None):
            self.assertEqual(bridge.token_status(), "missing")

    def test_valid_when_apple_ok(self):
        with mock.patch.object(bridge, "read_mut", return_value="m"), \
             mock.patch.object(bridge, "am_get", return_value={}):
            self.assertEqual(bridge.token_status(), "valid")

    def test_expired_on_mutexpired(self):
        with mock.patch.object(bridge, "read_mut", return_value="m"), \
             mock.patch.object(bridge, "am_get", side_effect=bridge.MUTExpired("401")):
            self.assertEqual(bridge.token_status(), "expired")


class TestTokenEndpoint(WebTestBase):
    def test_writes_mut(self):
        r = self.client.post("/api/token", json={"token": "  new-token  "})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with open(bridge.MUT_PATH) as fh:
            self.assertEqual(fh.read(), "new-token")

    def test_rejects_empty(self):
        r = self.client.post("/api/token", json={"token": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(os.path.exists(bridge.MUT_PATH))


class TestLidarrSearch(WebTestBase):
    def test_shapes_candidates(self):
        candidates = [{
            "foreignAlbumId": "rg1", "title": "OK Computer", "albumType": "Album",
            "releaseDate": "1997-05-21T00:00:00Z",
            "artist": {"artistName": "Radiohead"},
            "images": [{"coverType": "cover", "remoteUrl": "http://img/cover.jpg"}],
        }]
        with mock.patch.object(bridge, "lidarr", return_value=candidates):
            r = self.client.get("/api/lidarr-search?term=radiohead+ok+computer")
        out = r.get_json()
        self.assertEqual(out[0], {
            "foreignAlbumId": "rg1", "title": "OK Computer", "artist": "Radiohead",
            "albumType": "Album", "year": "1997", "cover": "http://img/cover.jpg",
        })

    def test_empty_term_returns_empty(self):
        with mock.patch.object(bridge, "lidarr") as lid:
            r = self.client.get("/api/lidarr-search?term=")
        self.assertEqual(r.get_json(), [])
        lid.assert_not_called()

    def test_lidarr_error_returns_502(self):
        with mock.patch.object(bridge, "lidarr", side_effect=RuntimeError("down")):
            r = self.client.get("/api/lidarr-search?term=x")
        self.assertEqual(r.status_code, 502)
        self.assertIn("error", r.get_json())


class TestResolve(WebTestBase):
    ALBUM = {"foreignAlbumId": "rg1", "title": "Alb",
             "artist": {"foreignArtistId": "aid1", "artistName": "A"}}

    def test_applies_and_clears_unresolved(self):
        self.seed_state({"t1": {"rg": None, "aid": None}})
        self.seed_unresolved([{"id": "t1", "artist": "A", "album": "Alb", "reason": "no match"}])
        with mock.patch.object(bridge, "_lidarr_album_by_mbid", return_value=self.ALBUM), \
             mock.patch.object(bridge, "ensure_album_in_lidarr",
                               return_value="monitored + searched 'Alb' by A") as ea:
            r = self.client.post("/api/resolve", json={"trackId": "t1", "foreignAlbumId": "rg1"})
        self.assertTrue(r.get_json()["ok"])
        ea.assert_called_once_with(self.ALBUM)
        self.assertEqual(self.read_state()["t1"], {"rg": "rg1", "aid": "aid1", "slug": None})
        self.assertEqual(bridge.load_unresolved(), [])

    def test_404_when_album_not_found(self):
        with mock.patch.object(bridge, "_lidarr_album_by_mbid", return_value=None):
            r = self.client.post("/api/resolve", json={"trackId": "t1", "foreignAlbumId": "rg1"})
        self.assertEqual(r.status_code, 404)

    def test_400_when_params_missing(self):
        r = self.client.post("/api/resolve", json={"trackId": "t1"})
        self.assertEqual(r.status_code, 400)


class TestDismiss(WebTestBase):
    def test_removes_entry_and_baselines(self):
        self.seed_unresolved([{"id": "t1", "artist": "A", "album": "Alb", "reason": "no match"}])
        r = self.client.post("/api/dismiss", json={"trackId": "t1"})
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["removed"])
        self.assertEqual(bridge.load_unresolved(), [])
        self.assertEqual(self.read_state()["t1"], {"rg": None, "aid": None})

    def test_400_without_trackid(self):
        r = self.client.post("/api/dismiss", json={})
        self.assertEqual(r.status_code, 400)


class TestRetry(WebTestBase):
    ALBUM = {"foreignAlbumId": "rg1", "title": "Alb",
             "artist": {"foreignArtistId": "aid1", "artistName": "A"}}

    def setUp(self):
        super().setUp()
        self.seed_state({"t1": {"rg": None, "aid": None}})
        self.seed_unresolved([{"id": "t1", "artist": "A", "album": "Alb", "reason": "no match"}])

    def test_match_applies(self):
        with mock.patch.object(bridge, "resolve_album", return_value=self.ALBUM), \
             mock.patch.object(bridge, "ensure_album_in_lidarr", return_value="ok"):
            r = self.client.post("/api/retry", json={"trackId": "t1"})
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["matched"])
        self.assertEqual(self.read_state()["t1"], {"rg": "rg1", "aid": "aid1", "slug": None})
        self.assertEqual(bridge.load_unresolved(), [])

    def test_still_no_match_keeps_entry(self):
        with mock.patch.object(bridge, "resolve_album", return_value=None), \
             mock.patch.object(bridge, "ensure_album_in_lidarr") as ea:
            r = self.client.post("/api/retry", json={"trackId": "t1"})
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["matched"])
        ea.assert_not_called()
        self.assertEqual(len(bridge.load_unresolved()), 1)

    def test_404_for_unknown_track(self):
        r = self.client.post("/api/retry", json={"trackId": "nope"})
        self.assertEqual(r.status_code, 404)


class TestPagesRender(WebTestBase):
    def test_dashboard_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Apple Music Bridge", r.data)
        self.assertIn(b"Recent activity", r.data)
        self.assertIn(b"Sync now", r.data)
        self.assertIn(b"Developer token", r.data)

    def test_token_page_injects_dev_token(self):
        with mock.patch.object(bridge, "developer_token", return_value="DEVTOKEN123"):
            r = self.client.get("/token")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"DEVTOKEN123", r.data)


class TestHealthz(WebTestBase):
    def test_503_when_no_cycle_has_run(self):
        with mock.patch.object(bridge, "_last_cycle", {"at": None, "added": 0, "removed": 0}):
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])

    def test_200_when_recent(self):
        from datetime import datetime, timezone
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with mock.patch.object(bridge, "_last_cycle",
                               {"at": fresh, "added": 0, "removed": 0}), \
             mock.patch.object(bridge, "POLL_INTERVAL", 900):
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertLess(body["age_seconds"], 10)

    def test_503_when_stale(self):
        from datetime import datetime, timezone, timedelta
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        with mock.patch.object(bridge, "_last_cycle",
                               {"at": stale, "added": 0, "removed": 0}), \
             mock.patch.object(bridge, "POLL_INTERVAL", 900):
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 503)
        self.assertGreater(r.get_json()["age_seconds"], 1800)


class TestActivityEndpoint(WebTestBase):
    def test_returns_log_newest_first(self):
        bridge.append_activity({"kind": "add", "artist": "A", "album": "1"})
        bridge.append_activity({"kind": "remove", "artist": "B", "album": "2"})
        r = self.client.get("/api/activity")
        self.assertEqual(r.status_code, 200)
        out = r.get_json()
        self.assertEqual([e["kind"] for e in out], ["remove", "add"])

    def test_empty_when_no_activity(self):
        r = self.client.get("/api/activity")
        self.assertEqual(r.get_json(), [])


class TestSyncEndpoint(WebTestBase):
    def test_runs_sync_and_drains_digest(self):
        with mock.patch.object(bridge, "sync_favorites",
                               return_value=(2, 2, 1, 1)) as sf, \
             mock.patch.object(bridge, "_drain_pending_handoffs") as drain:
            r = self.client.post("/api/sync", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["added"], 2)
        self.assertEqual(body["removed_done"], 1)
        sf.assert_called_once_with(act=True)
        drain.assert_called_once()

    def test_401_when_token_expired(self):
        with mock.patch.object(bridge, "sync_favorites",
                               side_effect=bridge.MUTExpired("nope")):
            r = self.client.post("/api/sync", json={})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
