"""Unit tests for the Apple Music -> Lidarr bridge.

All network access (Apple Music, Lidarr, MusicBrainz, Apprise) is mocked, so the
suite runs offline. Run with:  python -m unittest discover -s tests -v
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


class TestNormalization(unittest.TestCase):
    def test_norm_strips_suffix_and_punctuation(self):
        self.assertEqual(bridge._norm("Six Upbeat Songs - EP"), bridge._norm("six upbeat songs"))
        self.assertEqual(bridge._norm("Foo - Single"), "foo")
        self.assertEqual(bridge._norm("Album (Deluxe)!"), "albumdeluxe")

    def test_match_score_levels(self):
        self.assertEqual(bridge._match_score("OK Computer", "Radiohead", "OK Computer", "Radiohead"), 3)
        # EP suffix on the wanted side still scores exact after stripping
        self.assertEqual(bridge._match_score("Songs", "Jazz Emu", "Songs - EP", "Jazz Emu"), 3)
        # substring + artist match
        self.assertGreaterEqual(
            bridge._match_score("OK Computer OKNOTOK", "Radiohead", "OK Computer", "Radiohead"), 2)
        # right title, wrong artist -> weak
        self.assertEqual(bridge._match_score("OK Computer", "Coldplay", "OK Computer", "Radiohead"), 1)
        # no match
        self.assertEqual(bridge._match_score("Kid A", "Radiohead", "OK Computer", "Radiohead"), 0)

    def test_lucene_clean_removes_breaking_chars(self):
        self.assertNotIn('"', bridge._lucene_clean('a"b'))
        self.assertNotIn("\\", bridge._lucene_clean("a\\b"))


class TestTrackParsing(unittest.TestCase):
    def test_identity_prefers_catalog_id(self):
        t = {"id": "lib1", "attributes": {"playParams": {"id": "p1", "catalogId": "cat1"}}}
        self.assertEqual(bridge.track_identity(t), "cat1")

    def test_identity_fallbacks(self):
        self.assertEqual(bridge.track_identity({"id": "lib1", "attributes": {}}), "lib1")
        self.assertEqual(
            bridge.track_identity({"id": "lib1", "attributes": {"playParams": {"id": "p1"}}}), "p1")

    def test_artist_album_extraction_trims(self):
        t = {"attributes": {"artistName": " A ", "albumName": "B", "name": "N"}}
        self.assertEqual(bridge.track_artist_album(t), ("A", "B", "N"))

    def test_mb_artist_name(self):
        self.assertEqual(bridge._mb_artist_name({"artist-credit": [{"artist": {"name": "Jazz Emu"}}]}), "Jazz Emu")
        self.assertEqual(bridge._mb_artist_name({}), "")


class TestResolveAlbum(unittest.TestCase):
    ALBUM = {"foreignAlbumId": "rg-mbid", "title": "OK Computer",
             "artist": {"foreignArtistId": "art-mbid", "artistName": "Radiohead"}}

    def test_musicbrainz_first_then_lidarr_by_mbid(self):
        rg = {"id": "rg-mbid", "title": "OK Computer",
              "artist-credit": [{"artist": {"name": "Radiohead"}}]}
        with mock.patch.object(bridge, "mb_get", return_value={"release-groups": [rg]}), \
             mock.patch.object(bridge, "lidarr", return_value=[self.ALBUM]) as lid:
            result = bridge.resolve_album("Radiohead", "OK Computer")
        self.assertEqual(result["foreignAlbumId"], "rg-mbid")
        lid.assert_called_with("GET", "/album/lookup", params={"term": "lidarr:rg-mbid"})

    def test_falls_back_to_text_lookup_when_mb_empty(self):
        with mock.patch.object(bridge, "mb_get", return_value={"release-groups": []}), \
             mock.patch.object(bridge, "lidarr", return_value=[self.ALBUM]):
            result = bridge.resolve_album("Radiohead", "OK Computer")
        self.assertEqual(result["title"], "OK Computer")

    def test_falls_back_when_musicbrainz_unreachable(self):
        with mock.patch.object(bridge, "mb_get", side_effect=RuntimeError("503")), \
             mock.patch.object(bridge, "lidarr", return_value=[self.ALBUM]):
            result = bridge.resolve_album("Radiohead", "OK Computer")
        self.assertEqual(result["title"], "OK Computer")

    def test_returns_none_when_nothing_matches(self):
        with mock.patch.object(bridge, "mb_get", return_value={"release-groups": []}), \
             mock.patch.object(bridge, "lidarr", return_value=[]):
            self.assertIsNone(bridge.resolve_album("Nobody", "Nothing"))


class TestSyncFavorites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(bridge, "STATE_PATH", os.path.join(self.tmp, "state.json")),
            mock.patch.object(bridge, "SEEN_PATH", os.path.join(self.tmp, "seen.json")),
            mock.patch.object(bridge, "UNMONITOR_ON_REMOVE", True),
            mock.patch.object(bridge, "MAX_REMOVALS_PER_CYCLE", 25),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        with open(bridge.STATE_PATH) as fh:
            return json.load(fh)["favorites"]

    @staticmethod
    def _track(tid, artist="A", album="Alb"):
        return {"id": tid, "attributes": {"playParams": {"catalogId": tid},
                "artistName": artist, "albumName": album, "name": "song"}}

    def _seed(self, tracks, rg_for=lambda tid: "rg-" + tid):
        with mock.patch.object(bridge, "favorite_tracks", return_value=tracks), \
             mock.patch.object(bridge, "process_one",
                               side_effect=lambda tr: {"rg": rg_for(bridge.track_identity(tr)), "aid": "aid"}):
            bridge.sync_favorites(act=True)

    def test_add_new_favorites(self):
        tracks = [self._track("t1"), self._track("t2")]
        with mock.patch.object(bridge, "favorite_tracks", return_value=tracks), \
             mock.patch.object(bridge, "process_one",
                               side_effect=lambda tr: {"rg": "rg-" + bridge.track_identity(tr), "aid": "a"}) as po:
            added, added_ok, removed, removed_done = bridge.sync_favorites(act=True)
        self.assertEqual((added, added_ok, removed, removed_done), (2, 2, 0, 0))
        self.assertEqual(po.call_count, 2)
        self.assertEqual(self._state()["t1"]["rg"], "rg-t1")

    def test_unchanged_is_noop(self):
        self._seed([self._track("t1")])
        with mock.patch.object(bridge, "favorite_tracks", return_value=[self._track("t1")]), \
             mock.patch.object(bridge, "process_one") as po:
            added, _, removed, _ = bridge.sync_favorites(act=True)
        po.assert_not_called()
        self.assertEqual((added, removed), (0, 0))

    def test_removal_unmonitors_album(self):
        self._seed([self._track("t1")], rg_for=lambda tid: "rg1")
        with mock.patch.object(bridge, "favorite_tracks", return_value=[]), \
             mock.patch.object(bridge, "unmonitor_album", return_value="un-monitored") as um:
            _, _, removed, removed_done = bridge.sync_favorites(act=True)
        um.assert_called_once_with("rg1", "aid")
        self.assertEqual((removed, removed_done), (1, 1))
        self.assertNotIn("t1", self._state())

    def test_shared_album_not_unmonitored(self):
        self._seed([self._track("t1"), self._track("t2")], rg_for=lambda tid: "rg1")
        with mock.patch.object(bridge, "favorite_tracks", return_value=[self._track("t2")]), \
             mock.patch.object(bridge, "unmonitor_album") as um:
            _, _, _, removed_done = bridge.sync_favorites(act=True)
        um.assert_not_called()
        self.assertEqual(removed_done, 0)

    def test_safety_threshold_skips_unmonitoring(self):
        tracks = [self._track(f"t{i}") for i in range(30)]
        self._seed(tracks)  # 30 favorites, each with a distinct rg
        with mock.patch.object(bridge, "favorite_tracks", return_value=[]), \
             mock.patch.object(bridge, "unmonitor_album") as um:
            _, _, removed, removed_done = bridge.sync_favorites(act=True)
        um.assert_not_called()
        self.assertEqual((removed, removed_done), (30, 0))
        # anomaly: entries are left in place rather than dropped
        self.assertEqual(len(self._state()), 30)

    def test_unmonitor_disabled_pops_without_unmonitoring(self):
        self._seed([self._track("t1")], rg_for=lambda tid: "rg1")
        with mock.patch.object(bridge, "UNMONITOR_ON_REMOVE", False), \
             mock.patch.object(bridge, "favorite_tracks", return_value=[]), \
             mock.patch.object(bridge, "unmonitor_album") as um:
            bridge.sync_favorites(act=True)
        um.assert_not_called()
        self.assertNotIn("t1", self._state())

    def test_seed_records_baseline_without_acting(self):
        with mock.patch.object(bridge, "favorite_tracks", return_value=[self._track("t1")]), \
             mock.patch.object(bridge, "process_one") as po, \
             mock.patch.object(bridge, "unmonitor_album") as um:
            bridge.sync_favorites(act=False)
        po.assert_not_called()
        um.assert_not_called()
        self.assertIsNone(self._state()["t1"]["rg"])


class TestStateMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paths(self, state="state.json", seen="seen.json"):
        return os.path.join(self.tmp, state), os.path.join(self.tmp, seen)

    def test_migrates_legacy_seen_list(self):
        state, seen = self._paths()
        with open(seen, "w") as fh:
            json.dump(["a", "b", "c"], fh)
        with mock.patch.object(bridge, "STATE_PATH", state), mock.patch.object(bridge, "SEEN_PATH", seen):
            favs = bridge.load_state()["favorites"]
        self.assertEqual(set(favs), {"a", "b", "c"})
        self.assertIsNone(favs["a"]["rg"])

    def test_reads_existing_state(self):
        state, seen = self._paths(seen="none.json")
        with open(state, "w") as fh:
            json.dump({"favorites": {"x": {"rg": "r", "aid": "a"}}}, fh)
        with mock.patch.object(bridge, "STATE_PATH", state), mock.patch.object(bridge, "SEEN_PATH", seen):
            favs = bridge.load_state()["favorites"]
        self.assertEqual(favs["x"]["rg"], "r")

    def test_empty_when_no_files(self):
        state, seen = self._paths(state="nope.json", seen="nope2.json")
        with mock.patch.object(bridge, "STATE_PATH", state), mock.patch.object(bridge, "SEEN_PATH", seen):
            self.assertEqual(bridge.load_state()["favorites"], {})

    def test_save_then_load_roundtrip(self):
        state, seen = self._paths()
        with mock.patch.object(bridge, "STATE_PATH", state), mock.patch.object(bridge, "SEEN_PATH", seen):
            bridge.save_state({"favorites": {"k": {"rg": "rg", "aid": "aid"}}})
            self.assertEqual(bridge.load_state()["favorites"]["k"]["rg"], "rg")


class TestDeveloperToken(unittest.TestCase):
    def test_signs_es256_jwt_with_kid_and_claims(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import jwt

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        tmp = tempfile.mkdtemp()
        p8 = os.path.join(tmp, "k.p8")
        with open(p8, "w") as fh:
            fh.write(pem)
        try:
            with mock.patch.object(bridge, "TEAM_ID", "TEAM123456"), \
                 mock.patch.object(bridge, "KEY_ID", "KEY1234567"), \
                 mock.patch.object(bridge, "P8_PATH", p8), \
                 mock.patch.dict(bridge._dev_token_cache, {"token": None, "exp": 0}):
                token = bridge.developer_token()
            header = jwt.get_unverified_header(token)
            self.assertEqual(header["alg"], "ES256")
            self.assertEqual(header["kid"], "KEY1234567")
            claims = jwt.decode(token, options={"verify_signature": False})
            self.assertEqual(claims["iss"], "TEAM123456")
            self.assertGreater(claims["exp"], claims["iat"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestUnmonitorAlbum(unittest.TestCase):
    def test_unmonitors_matching_monitored_album(self):
        put_bodies = []

        def fake_lidarr(method, path, params=None, body=None):
            if path == "/artist":
                return [{"id": 7, "foreignArtistId": "aid1", "artistName": "A"}]
            if path == "/album":
                return [{"id": 11, "foreignAlbumId": "rg1", "monitored": True, "title": "Alb"}]
            if path == "/album/monitor":
                put_bodies.append(body)
            return None

        with mock.patch.object(bridge, "lidarr", side_effect=fake_lidarr):
            status = bridge.unmonitor_album("rg1", "aid1")
        self.assertIn("un-monitored", status)
        self.assertEqual(put_bodies, [{"albumIds": [11], "monitored": False}])

    def test_noop_when_artist_absent(self):
        with mock.patch.object(bridge, "lidarr", return_value=[]):
            status = bridge.unmonitor_album("rg1", "missing-aid")
        self.assertIn("nothing to un-monitor", status)


class TestEnsureAlbumExistingArtist(unittest.TestCase):
    def test_monitors_and_searches_for_existing_artist(self):
        album_lookup = {"foreignAlbumId": "rg1", "title": "Alb",
                        "artist": {"foreignArtistId": "aid1", "artistName": "A"}}
        calls = []

        def fake_lidarr(method, path, params=None, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/artist":
                return [{"id": 7, "foreignArtistId": "aid1", "artistName": "A"}]
            if method == "GET" and path == "/album":
                return [{"id": 11, "foreignAlbumId": "rg1", "monitored": True, "title": "Alb"}]
            return None

        with mock.patch.object(bridge, "lidarr", side_effect=fake_lidarr), \
             mock.patch.object(bridge.time, "sleep"):
            status = bridge.ensure_album_in_lidarr(album_lookup)
        self.assertIn("searched", status)
        self.assertTrue(
            any(p == "/command" and (b or {}).get("name") == "AlbumSearch" for _, p, b in calls))


if __name__ == "__main__":
    unittest.main()
