import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from tagger.gpx import Match, Point, utc_time
from tagger.map_preview import MapPreview, map_data
from tagger.progress_ui import duration, progress_text
from tagger.workflow import Options, Photo, Plan, Progress, apply, backup, match_images, verify

FIXTURES = Path(__file__).parent / "fixtures"


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def test_progress_tracks_each_stage_and_live_summary(self):
        client = Mock()
        client.album.return_value = ("Trip", [
            {"id": "photo", "originalFileName": "photo.jpg", "type": "IMAGE"},
            {"id": "video", "originalFileName": "video.mp4", "type": "VIDEO"}])
        def download(_, path):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"photo")
        client.download.side_effect = download
        client.check_original.side_effect = lambda asset, _: asset
        tool = Mock()
        tool.read.return_value = {"DateTimeOriginal": "2024:01:01 11:01:00"}
        events = []
        plan = backup(client, self.output, lambda _: None, events.append)
        self.assertIsNone(events[0].total)
        self.assertEqual((events[-1].stage, events[-1].completed, events[-1].total), ("backup", 2, 2))
        self.assertEqual(events[-1].statistics["skipped"], 1)
        match_images(plan, tool, [FIXTURES / "track.gpx"], Options(timezone="Europe/Berlin"), lambda _: None, events.append)
        self.assertEqual((events[-1].stage, events[-1].completed, events[-1].total), ("matching", 1, 1))
        self.assertEqual(events[-1].statistics["matched"], 1)
        apply(client, plan, lambda _: None, events.append)
        verify(client, plan, lambda _: None, events.append)
        self.assertEqual((events[-1].stage, events[-1].completed, events[-1].total), ("verifying", 1, 1))
        self.assertEqual(events[-1].statistics["verified"], 1)
        self.assertEqual(events[-1].current, "photo.jpg")
        for stage in ("backup", "matching", "updating", "verifying"):
            counts = [e.completed for e in events if e.stage == stage and e.total is not None]
            self.assertEqual(counts, sorted(counts))

    def test_empty_album_finishes_without_division_by_zero(self):
        client = Mock()
        client.album.return_value = ("Empty", [])
        events = []
        backup(client, self.output, lambda _: None, events.append)
        self.assertEqual(progress_text(events[-1], 1)[0], "0 / 0 processed · 100%")

    def test_progress_formatting_and_eta(self):
        count, timing = progress_text(Progress("matching", 25, 100), 10)
        self.assertIn("25%", count)
        self.assertIn("30s remaining", timing)
        self.assertIn("Estimating", progress_text(Progress("matching", 0, 100), 0)[1])
        self.assertIn("Preparing", progress_text(Progress("backup", 0, None), 5)[0])
        self.assertEqual(duration(3665), "1h 01m")


class MapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        original = root / "original" / "one" / "photo.jpg"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"local-photo")
        match = Match(Point(utc_time("2024-01-01T10:01:00Z"), 52.001, 13.002), "interpolated", 60, "track.gpx")
        self.plan = Plan("Trip", root, Options(), [
            Photo({"id": "one", "sensitive": "do-not-expose"}, "<script>photo.jpg", "ready", match=match, original=original),
            Photo({"id": "two"}, "unmatched.jpg", "unmatched"),
            Photo({"id": "three", "exifInfo": {"latitude": 0, "longitude": 0}}, "gps.jpg", "skipped"),
            Photo({"id": "four", "exifInfo": {"latitude": "nan", "longitude": 1}}, "invalid.jpg", "error")], "review",
            routes=[{"name": "track.gpx", "points": [[52, 13], [52.002, 13.004]]}])

    def test_payload_never_invents_coordinates_or_exposes_credentials(self):
        data = map_data(self.plan, stale=True)
        self.assertEqual(data["photos"][0]["location"], [52.001, 13.002])
        self.assertIsNone(data["photos"][1]["location"])
        self.assertEqual(data["photos"][2]["location"], [0, 0])
        self.assertIsNone(data["photos"][3]["location"])
        self.assertEqual(data["photos"][2]["kind"], "existing")
        self.assertTrue(data["stale"])
        self.assertEqual(len(data["routes"]), 1)
        self.assertNotIn("do-not-expose", json.dumps(data))
        self.assertNotIn(str(self.plan.folder), json.dumps(data))

    def test_preview_server_serves_only_map_and_authorized_local_photos(self):
        server = MapPreview()
        self.addCleanup(server.close)
        server.update(self.plan, focus="0")
        response = requests.get(server.url + "data", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["focus"], "0")
        self.assertTrue(response.json()["photos"][0]["preview"])
        self.assertNotIn("do-not-expose", response.text)
        self.assertEqual(requests.get(server.url + "photo/0", timeout=5).content, b"local-photo")
        self.assertEqual(requests.get(server.url + "photo/1", timeout=5).status_code, 404)
        self.assertEqual(requests.get(server.url + "../report.json", timeout=5).status_code, 404)
        self.assertEqual(requests.get(server.url.replace(server.token, "wrong-token"), timeout=5).status_code, 404)
        page = requests.get(server.url, timeout=5)
        self.assertIn("Photo location review", page.text)
        self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
        self.assertEqual(requests.get(server.url + "leaflet.js", timeout=5).status_code, 200)
        self.assertEqual(requests.post(server.url + "data", timeout=5).status_code, 501)

    def test_preview_refreshes_exclusions_and_invalidates_old_run(self):
        server = MapPreview()
        self.addCleanup(server.close)
        server.update(self.plan)
        first = requests.get(server.url + "data", timeout=5).json()
        self.plan.photos[0].status = "excluded"
        server.update(self.plan)
        second = requests.get(server.url + "data", timeout=5).json()
        self.assertGreater(second["revision"], first["revision"])
        self.assertEqual(second["photos"][0]["status"], "excluded")
        server.update(None)
        empty = requests.get(server.url + "data", timeout=5).json()
        self.assertEqual(empty["photos"], [])
        self.assertEqual(requests.get(server.url + "photo/0", timeout=5).status_code, 404)


if __name__ == "__main__":
    unittest.main()
