import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from tagger.gpx import Match, Point, utc_time
from tagger.immich import Immich
from tagger.workflow import Options, apply, backup, match_images, verify

FIXTURES = Path(__file__).parent / "fixtures"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.asset = {"id": "original", "type": "IMAGE", "originalFileName": "photo.jpg", "exifInfo": {}}
        self.client = Mock()
        self.client.album.return_value = ("Trip", [self.asset])
        def download(_, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original-photo")
        self.client.download.side_effect = download
        self.client.check_original.side_effect = lambda asset, _: copy.deepcopy(asset)
        self.tool = Mock()
        self.tool.read.return_value = {"DateTimeOriginal": "2024:01:01 11:01:00"}

    def plan(self):
        plan = backup(self.client, self.folder, lambda _: None)
        return match_images(plan, self.tool, [FIXTURES / "track.gpx"],
                            Options(timezone="Europe/Berlin"), lambda _: None)

    def test_all_images_are_backed_up_before_matching_even_existing_gps_and_raw(self):
        second = {**self.asset, "id": "second", "originalFileName": "photo.cr3",
                  "exifInfo": {"latitude": 0, "longitude": 0}}
        self.client.album.return_value = ("Trip", [self.asset, second])
        plan = backup(self.client, self.folder, lambda _: None)
        self.assertEqual(plan.stage, "backed_up")
        self.assertEqual(plan.statistics["backed_up"], 2)
        self.tool.read.assert_not_called()
        self.client.update_location.assert_not_called()
        match_images(plan, self.tool, [FIXTURES / "track.gpx"], Options(timezone="Europe/Berlin"), lambda _: None)
        self.assertEqual(self.client.download.call_count, 2)
        self.assertEqual(plan.photos[1].status, "skipped")
        self.assertTrue(plan.photos[1].original.is_file())

    def test_matching_is_read_only_and_can_reuse_backups(self):
        plan = self.plan()
        self.assertEqual((plan.stage, len(plan.ready)), ("review", 1))
        before = plan.photos[0].original.read_bytes()
        self.assertFalse((plan.folder / "taggedImages").exists())
        self.tool.write.assert_not_called()
        self.client.update_location.assert_not_called()
        match_images(plan, self.tool, [FIXTURES / "track.gpx"], Options(timezone="Europe/Berlin"), lambda _: None)
        self.client.download.assert_called_once()
        self.assertEqual(plan.photos[0].original.read_bytes(), before)

    def test_update_and_verify_are_separate_passes(self):
        self.client.album.return_value = ("Trip", [self.asset, {**self.asset, "id": "second"}])
        plan = self.plan()
        events = []
        self.client.update_location.side_effect = lambda identifier, _: events.append("update:" + identifier)
        self.client.verify_location.side_effect = lambda asset, *_: events.append("verify:" + asset["id"])
        apply(self.client, plan, lambda _: None)
        self.assertEqual(plan.stage, "verification_pending")
        self.assertEqual(plan.statistics["verified"], 0)
        self.assertEqual(plan.statistics["verification_pending"], 2)
        self.client.verify_location.assert_not_called()
        verify(self.client, plan, lambda _: None)
        self.assertEqual(events, ["update:original", "update:second", "verify:original", "verify:second"])
        self.assertEqual((plan.stage, plan.statistics["verified"]), ("verified", 2))
        self.client.upload.assert_not_called()
        self.client.delete.assert_not_called()
        report = json.loads((plan.folder / "report.json").read_text())
        self.assertEqual(report["photos"][0]["verification"], "verified")

    def test_verification_can_confirm_a_request_that_timed_out(self):
        plan = self.plan()
        self.client.update_location.side_effect = ValueError("Request timed out")
        apply(self.client, plan, lambda _: None)
        self.assertEqual(plan.photos[0].status, "error")
        self.assertEqual(plan.photos[0].verification, "pending")
        verify(self.client, plan, lambda _: None)
        self.assertEqual(plan.photos[0].status, "verified")
        self.assertIn("despite an error", plan.photos[0].detail)

    def test_failed_verification_is_not_reported_as_verified(self):
        plan = self.plan()
        apply(self.client, plan, lambda _: None)
        self.client.verify_location.side_effect = ValueError("GPS mismatch")
        verify(self.client, plan, lambda _: None)
        self.assertEqual(plan.stage, "verification_failed")
        self.assertEqual((plan.statistics["updated"], plan.statistics["verified"], plan.statistics["errors"]), (1, 0, 1))
        self.client.verify_location.side_effect = None
        verify(self.client, plan, lambda _: None)
        self.assertEqual(plan.statistics["verified"], 1)
        self.client.update_location.assert_called_once()

    def test_missing_backup_prevents_server_changes(self):
        plan = self.plan()
        plan.photos[0].original.unlink()
        apply(self.client, plan, lambda _: None)
        self.assertEqual(plan.photos[0].status, "error")
        self.client.update_location.assert_not_called()
        verify(self.client, plan, lambda _: None)
        self.client.verify_location.assert_not_called()

    def test_excluded_matches_are_not_updated_or_verified(self):
        plan = self.plan()
        plan.photos[0].status = "excluded"
        apply(self.client, plan, lambda _: None)
        verify(self.client, plan, lambda _: None)
        self.client.update_location.assert_not_called()
        self.client.verify_location.assert_not_called()

    def test_wrong_order_is_rejected(self):
        plan = backup(self.client, self.folder, lambda _: None)
        with self.assertRaises(ValueError):
            apply(self.client, plan, lambda _: None)
        with self.assertRaises(ValueError):
            verify(self.client, plan, lambda _: None)

    def test_unmatched_photo_and_rematching(self):
        self.tool.read.return_value = {"DateTimeOriginal": "2024:01:02 10:00:00"}
        plan = self.plan()
        photo = plan.photos[0]
        self.assertEqual((photo.status, plan.statistics["unmatched"]), ("unmatched", 1))
        unmatched = photo.unmatched
        self.assertEqual(unmatched.read_bytes(), photo.original.read_bytes())
        self.tool.read.return_value = {"DateTimeOriginal": "2024:01:01 11:01:00"}
        match_images(plan, self.tool, [FIXTURES / "track.gpx"], Options(timezone="Europe/Berlin"), lambda _: None)
        self.assertEqual(photo.status, "ready")
        self.assertFalse(unmatched.exists())
        self.client.download.assert_called_once()


class APITests(unittest.TestCase):
    def setUp(self):
        self.client = Immich("https://photos.test/albums/12345678-1234-4234-8234-123456789abc", "test-secret")
        self.addCleanup(self.client.close)
        self.asset = {"id": "original", "checksum": "checksum", "fileCreatedAt": "2024-01-01T10:00:00Z",
                      "fileModifiedAt": "2024-01-01T11:00:00Z", "localDateTime": "2024-01-01T11:00:00Z"}
        self.match = Match(Point(utc_time("2024-01-01T10:00:00Z"), 52, 13), "exact", 0, "track.gpx")

    def test_v3_cursor_pagination_and_scope(self):
        self.client._json = Mock(side_effect=[
            {"major": 3, "minor": 2, "patch": 0}, {"albumName": "Trip", "assetCount": 2},
            {"assets": {"items": [{"id": "a"}], "nextCursor": "next", "nextPage": None}},
            {"assets": {"items": [{"id": "b"}], "nextCursor": None}},
        ])
        name, assets = self.client.album()
        self.assertEqual((name, [a["id"] for a in assets]), ("Trip", ["a", "b"]))
        body = self.client._json.call_args.kwargs["json"]
        self.assertEqual(body["filter"]["albumIds"]["any"], [self.client.album_id])
        self.assertEqual(body["cursor"], "next")

    def test_v3_legacy_page_pagination(self):
        self.client._json = Mock(side_effect=[
            {"major": 3, "minor": 0, "patch": 0}, {"albumName": "Trip"},
            {"assets": {"items": [{"id": "a"}], "nextPage": "2"}},
            {"assets": {"items": [{"id": "b"}], "nextPage": None}},
        ])
        self.assertEqual(len(self.client.album()[1]), 2)
        self.assertEqual(self.client._json.call_args.kwargs["json"]["page"], 2)

    def test_v2_embedded_album_assets(self):
        self.client._json = Mock(side_effect=[
            {"major": 2, "minor": 0, "patch": 0}, {"albumName": "Trip", "assets": [{"id": "a"}], "assetCount": 1}])
        self.assertEqual(self.client.album()[1], [{"id": "a"}])

    def test_location_update_sends_no_dates_or_files_or_verification_reads(self):
        self.client._request = MagicMock()
        self.client._json = Mock()
        self.client.update_location("original", self.match)
        self.client._request.assert_called_once_with("PUT", "/assets", json={"ids": ["original"], "latitude": 52, "longitude": 13})
        self.client._json.assert_not_called()

    def test_date_change_since_review_prevents_update(self):
        self.client._json = Mock(return_value={**self.asset, "fileCreatedAt": "2025-01-01T10:00:00Z"})
        self.client._request = Mock()
        with self.assertRaisesRegex(ValueError, "changed since backup"):
            self.client.check_original(self.asset, "checksum")
        self.client._request.assert_not_called()

    def test_verification_checks_coordinates_and_dates(self):
        after = {**self.asset, "exifInfo": {"latitude": 52, "longitude": 13}}
        self.client._json = Mock(return_value=after)
        self.client.verify_location(self.asset, self.match, "checksum")
        after["fileCreatedAt"] = "2025-01-01T10:00:00Z"
        with self.assertRaisesRegex(ValueError, "Date verification failed"):
            self.client.verify_location(self.asset, self.match, "checksum")
        after["exifInfo"]["latitude"] = 1
        with self.assertRaisesRegex(ValueError, "GPS verification failed"):
            self.client.verify_location(self.asset, self.match, "checksum")

    def test_redirects_refused_and_key_not_exposed(self):
        response = Mock(status_code=302)
        self.client.session.request = Mock(return_value=response)
        with self.assertRaisesRegex(ValueError, "redirects are refused"):
            self.client._request("GET", "/server/version")
        self.assertFalse(self.client.session.request.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
