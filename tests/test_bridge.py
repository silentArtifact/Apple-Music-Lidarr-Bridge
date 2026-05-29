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

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bridge  # noqa: E402
import mint_mut  # noqa: E402


class FakeResp:
    """Stand-in for a requests.Response."""

    def __init__(self, status, json_data=None, headers=None, text="{}"):
        self.status_code = status
        self._json = {} if json_data is None else json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeClock:
    """Deterministic clock: sleep() advances time() instead of blocking."""

    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


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


class TestAmGet(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        resps = [FakeResp(429, headers={"Retry-After": "1"}),
                 FakeResp(200, {"data": ["ok"]})]
        with mock.patch.object(bridge, "developer_token", return_value="d"), \
             mock.patch.object(bridge, "read_mut", return_value="m"), \
             mock.patch.object(bridge.requests, "get", side_effect=resps) as g, \
             mock.patch.object(bridge.time, "sleep") as slp:
            out = bridge.am_get("/v1/test")
        self.assertEqual(out, {"data": ["ok"]})
        self.assertEqual(g.call_count, 2)
        slp.assert_called_once()

    def test_401_raises_mutexpired(self):
        with mock.patch.object(bridge, "developer_token", return_value="d"), \
             mock.patch.object(bridge, "read_mut", return_value="m"), \
             mock.patch.object(bridge.requests, "get", return_value=FakeResp(401)):
            with self.assertRaises(bridge.MUTExpired):
                bridge.am_get("/v1/test")

    def test_missing_mut_raises_without_calling_apple(self):
        with mock.patch.object(bridge, "developer_token", return_value="d"), \
             mock.patch.object(bridge, "read_mut", return_value=None), \
             mock.patch.object(bridge.requests, "get") as g:
            with self.assertRaises(bridge.MUTExpired):
                bridge.am_get("/v1/test")
        g.assert_not_called()

    def test_persistent_429_gives_up(self):
        with mock.patch.object(bridge, "developer_token", return_value="d"), \
             mock.patch.object(bridge, "read_mut", return_value="m"), \
             mock.patch.object(bridge.requests, "get",
                               return_value=FakeResp(429, headers={})) as g, \
             mock.patch.object(bridge.time, "sleep"):
            with self.assertRaises(RuntimeError):
                bridge.am_get("/v1/test")
        self.assertEqual(g.call_count, 5)

    def test_developer_token_only_when_user_false(self):
        with mock.patch.object(bridge, "developer_token", return_value="d"), \
             mock.patch.object(bridge, "read_mut") as rm, \
             mock.patch.object(bridge.requests, "get",
                               return_value=FakeResp(200, {"data": []})):
            bridge.am_get("/v1/catalog", user=False)
        rm.assert_not_called()


class TestAmPaginate(unittest.TestCase):
    def test_walks_next_pages(self):
        pages = [{"data": [1, 2], "next": "/page2"}, {"data": [3], "next": None}]
        with mock.patch.object(bridge, "am_get", side_effect=pages) as ag:
            items = list(bridge.am_paginate("/start"))
        self.assertEqual(items, [1, 2, 3])
        self.assertEqual(ag.call_count, 2)
        self.assertEqual(ag.call_args_list[0].kwargs["params"]["limit"], 100)
        # the second request follows `next` with no params (offset is baked in)
        self.assertEqual(ag.call_args_list[1], mock.call("/page2", params=None))


class TestMbGet(unittest.TestCase):
    def setUp(self):
        bridge._mb_last[0] = 0.0  # disable the rate-limit sleep under real time

    def test_retries_on_503_then_succeeds(self):
        resps = [FakeResp(503), FakeResp(200, {"release-groups": []})]
        with mock.patch.object(bridge.requests, "get", side_effect=resps) as g, \
             mock.patch.object(bridge.time, "sleep"):
            out = bridge.mb_get("release-group", {"query": "x"})
        self.assertEqual(out, {"release-groups": []})
        self.assertEqual(g.call_count, 2)

    def test_persistent_503_gives_up(self):
        with mock.patch.object(bridge.requests, "get",
                               return_value=FakeResp(503)) as g, \
             mock.patch.object(bridge.time, "sleep"):
            with self.assertRaises(RuntimeError):
                bridge.mb_get("release-group", {"query": "x"})
        self.assertEqual(g.call_count, 3)


class TestBuildArtistAddFields(unittest.TestCase):
    def test_adds_without_grabbing_discography(self):
        src = {"foreignArtistId": "x", "artistName": "A"}
        out = bridge.build_artist_add_fields(src)
        self.assertEqual(out["addOptions"]["monitor"], "none")
        self.assertFalse(out["addOptions"]["searchForMissingAlbums"])
        self.assertFalse(out["monitored"])
        self.assertEqual(out["qualityProfileId"], bridge.QUALITY_PROFILE_ID)
        self.assertEqual(out["metadataProfileId"], bridge.METADATA_PROFILE_ID)
        self.assertEqual(out["rootFolderPath"], bridge.ROOT_FOLDER)
        self.assertEqual(out["foreignArtistId"], "x")
        self.assertNotIn("monitored", src)  # input is not mutated


class TestEnsureArtist(unittest.TestCase):
    def test_returns_existing_without_posting(self):
        def fake(method, path, params=None, body=None):
            if path == "/artist":
                return [{"id": 7, "foreignArtistId": "aid1", "artistName": "A"}]
            raise AssertionError(f"unexpected {method} {path}")

        with mock.patch.object(bridge, "lidarr", side_effect=fake):
            artist, was_new = bridge.ensure_artist("aid1", {})
        self.assertFalse(was_new)
        self.assertEqual(artist["id"], 7)

    def test_adds_via_lookup_result(self):
        posted = {}

        def fake(method, path, params=None, body=None):
            if method == "GET" and path == "/artist":
                return []
            if method == "GET" and path == "/artist/lookup":
                return [{"foreignArtistId": "aid2", "artistName": "New"}]
            if method == "POST" and path == "/artist":
                posted["body"] = body
                return {"id": 9, **body}
            raise AssertionError(f"unexpected {method} {path}")

        with mock.patch.object(bridge, "lidarr", side_effect=fake):
            artist, was_new = bridge.ensure_artist("aid2", {"foreignArtistId": "aid2"})
        self.assertTrue(was_new)
        self.assertEqual(artist["id"], 9)
        self.assertEqual(posted["body"]["addOptions"]["monitor"], "none")
        self.assertFalse(posted["body"]["monitored"])

    def test_falls_back_to_supplied_object_when_lookup_empty(self):
        posted = {}

        def fake(method, path, params=None, body=None):
            if method == "GET" and path == "/artist":
                return []
            if method == "GET" and path == "/artist/lookup":
                return []
            if method == "POST" and path == "/artist":
                posted["body"] = body
                return {"id": 3, **body}
            raise AssertionError(f"unexpected {method} {path}")

        with mock.patch.object(bridge, "lidarr", side_effect=fake):
            artist, was_new = bridge.ensure_artist(
                "aidX", {"foreignArtistId": "aidX", "artistName": "FB"})
        self.assertTrue(was_new)
        self.assertEqual(posted["body"]["artistName"], "FB")


class TestWaitForArtistReady(unittest.TestCase):
    def test_returns_once_album_present_and_refresh_done(self):
        clock = FakeClock()

        def fake(method, path, params=None, body=None):
            if path == "/command":
                return []
            if path == "/album":
                return [{"foreignAlbumId": "rg1"}]
            raise AssertionError(path)

        with mock.patch.object(bridge.time, "time", clock.time), \
             mock.patch.object(bridge.time, "sleep", clock.sleep), \
             mock.patch.object(bridge, "lidarr", side_effect=fake) as lid:
            bridge.wait_for_artist_ready(7, "rg1", clock.time() + 100)
        # one settle sleep + a single poll that already sees the album
        self.assertEqual(lid.call_count, 2)

    def test_gives_up_at_deadline_while_still_refreshing(self):
        clock = FakeClock()

        def fake(method, path, params=None, body=None):
            if path == "/command":
                return [{"name": "RefreshArtist", "status": "started"}]
            return []  # album never shows up

        with mock.patch.object(bridge.time, "time", clock.time), \
             mock.patch.object(bridge.time, "sleep", clock.sleep), \
             mock.patch.object(bridge, "lidarr", side_effect=fake):
            bridge.wait_for_artist_ready(7, "rg1", clock.time() + 10)
        self.assertGreaterEqual(clock.now, 1010)  # ran until the deadline, then returned


class TestEnsureAlbumNewArtist(unittest.TestCase):
    ALBUM = {"foreignAlbumId": "rg1", "title": "Alb",
             "artist": {"foreignArtistId": "aid1", "artistName": "A"}}

    def test_new_artist_waits_then_monitors_and_searches(self):
        calls = []

        def fake(method, path, params=None, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/album":
                return [{"id": 11, "foreignAlbumId": "rg1", "monitored": True, "title": "Alb"}]
            return None

        with mock.patch.object(bridge, "ensure_artist",
                               return_value=({"id": 7, "foreignArtistId": "aid1",
                                              "artistName": "A"}, True)), \
             mock.patch.object(bridge, "wait_for_artist_ready") as war, \
             mock.patch.object(bridge, "lidarr", side_effect=fake), \
             mock.patch.object(bridge.time, "sleep"):
            status = bridge.ensure_album_in_lidarr(self.ALBUM)
        war.assert_called_once()
        self.assertIn("monitored + searched", status)
        self.assertTrue(any(p == "/command" and (b or {}).get("name") == "AlbumSearch"
                            for _, p, b in calls))

    def test_falls_back_to_explicit_add_when_album_never_appears(self):
        posted = {}
        clock = FakeClock()

        def fake(method, path, params=None, body=None):
            if method == "GET" and path == "/album":
                return []  # the album never shows up under the artist
            if method == "POST" and path == "/album":
                posted["body"] = body
                return {"id": 1}
            return None

        with mock.patch.object(bridge, "ensure_artist",
                               return_value=({"id": 7, "foreignArtistId": "aid1",
                                              "artistName": "A"}, True)), \
             mock.patch.object(bridge, "wait_for_artist_ready"), \
             mock.patch.object(bridge, "lidarr", side_effect=fake), \
             mock.patch.object(bridge.time, "time", clock.time), \
             mock.patch.object(bridge.time, "sleep", clock.sleep):
            status = bridge.ensure_album_in_lidarr(self.ALBUM)
        self.assertIn("added + searched", status)
        self.assertTrue(posted["body"]["monitored"])
        self.assertEqual(posted["body"]["addOptions"], {"searchForNewAlbum": True})
        self.assertEqual(posted["body"]["artistId"], 7)

    def test_raises_when_match_has_no_artist_mbid(self):
        with self.assertRaises(RuntimeError):
            bridge.ensure_album_in_lidarr({"foreignAlbumId": "rg1", "title": "x", "artist": {}})


class TestProcessOne(unittest.TestCase):
    @staticmethod
    def _track(artist="A", album="Alb", name="song", tid="t1"):
        return {"id": tid, "attributes": {"playParams": {"catalogId": tid},
                "artistName": artist, "albumName": album, "name": name}}

    def test_missing_metadata_records_unresolved(self):
        with mock.patch.object(bridge, "record_unresolved") as ru, \
             mock.patch.object(bridge, "resolve_album") as ra:
            out = bridge.process_one(self._track(album=""))
        ra.assert_not_called()
        ru.assert_called_once()
        self.assertEqual(out, {"rg": None, "aid": None})

    def test_no_match_records_unresolved(self):
        with mock.patch.object(bridge, "resolve_album", return_value=None), \
             mock.patch.object(bridge, "ensure_album_in_lidarr") as ea, \
             mock.patch.object(bridge, "record_unresolved") as ru:
            out = bridge.process_one(self._track())
        ea.assert_not_called()
        ru.assert_called_once()
        self.assertEqual(out, {"rg": None, "aid": None})

    def test_success_returns_ids(self):
        match = {"foreignAlbumId": "rg1", "artist": {"foreignArtistId": "aid1"}}
        with mock.patch.object(bridge, "resolve_album", return_value=match), \
             mock.patch.object(bridge, "ensure_album_in_lidarr", return_value="ok"):
            out = bridge.process_one(self._track())
        self.assertEqual(out, {"rg": "rg1", "aid": "aid1"})

    def test_mutexpired_propagates(self):
        match = {"foreignAlbumId": "rg1", "artist": {"foreignArtistId": "aid1"}}
        with mock.patch.object(bridge, "resolve_album", return_value=match), \
             mock.patch.object(bridge, "ensure_album_in_lidarr",
                               side_effect=bridge.MUTExpired("x")):
            with self.assertRaises(bridge.MUTExpired):
                bridge.process_one(self._track())

    def test_generic_error_records_unresolved(self):
        match = {"foreignAlbumId": "rg1", "artist": {"foreignArtistId": "aid1"}}
        with mock.patch.object(bridge, "resolve_album", return_value=match), \
             mock.patch.object(bridge, "ensure_album_in_lidarr",
                               side_effect=ValueError("boom")), \
             mock.patch.object(bridge, "record_unresolved") as ru:
            out = bridge.process_one(self._track())
        self.assertEqual(out, {"rg": None, "aid": None})
        self.assertEqual(ru.call_args.args[0]["reason"], "boom")


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(bridge, "STATE_PATH", os.path.join(self.tmp, "state.json")),
            mock.patch.object(bridge, "SEEN_PATH", os.path.join(self.tmp, "seen.json")),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _track(tid):
        return {"id": tid, "attributes": {"playParams": {"catalogId": tid},
                "artistName": "A", "albumName": "Alb", "name": "song"}}

    def test_processes_every_favorite_and_saves(self):
        tracks = [self._track(f"t{i}") for i in range(3)]
        with mock.patch.object(bridge, "favorite_tracks", return_value=tracks), \
             mock.patch.object(bridge, "process_one",
                               side_effect=lambda tr: {"rg": "rg-" + bridge.track_identity(tr),
                                                       "aid": "a"}) as po:
            bridge.cmd_backfill()
        self.assertEqual(po.call_count, 3)
        with open(bridge.STATE_PATH) as fh:
            favs = json.load(fh)["favorites"]
        self.assertEqual(set(favs), {"t0", "t1", "t2"})
        self.assertEqual(favs["t1"]["rg"], "rg-t1")


class TestRecordUnresolved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_appends_entries(self):
        path = os.path.join(self.tmp, "unresolved.json")
        with mock.patch.object(bridge, "UNRESOLVED_PATH", path):
            bridge.record_unresolved({"id": "1", "reason": "no match"})
            bridge.record_unresolved({"id": "2", "reason": "missing metadata"})
        with open(path) as fh:
            entries = json.load(fh)
        self.assertEqual([e["id"] for e in entries], ["1", "2"])


class TestGetStorefront(unittest.TestCase):
    def test_returns_account_storefront(self):
        with mock.patch.object(bridge, "am_get",
                               return_value={"data": [{"id": "gb"}]}):
            self.assertEqual(bridge.get_storefront(), "gb")

    def test_falls_back_on_error(self):
        with mock.patch.object(bridge, "am_get", side_effect=RuntimeError("nope")):
            self.assertEqual(bridge.get_storefront(), bridge.STOREFRONT)


class TestMintDeveloperToken(unittest.TestCase):
    def test_signs_one_hour_es256_token(self):
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
            token = mint_mut.sign_developer_token(p8, "KEY1234567", "TEAM123456")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "KEY1234567")
        claims = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(claims["iss"], "TEAM123456")
        self.assertEqual(claims["exp"] - claims["iat"], 3600)


if __name__ == "__main__":
    unittest.main()
