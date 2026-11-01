"""Real ExifTool and HTTP checks. Requires ExifTool and requirements-dev.txt."""
import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tagger.immich import Immich, sha1
from tagger.metadata import ExifTool, camera_zone, photo_time
from tagger.gpx import utc_time
from tagger.workflow import Options, apply, backup, match_images, verify

try:
    from PIL import Image
except ImportError:
    Image = None

FIXTURES = Path(__file__).parent / "fixtures"


@unittest.skipIf(Image is None, "Install requirements-dev.txt for image tests")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.tool = ExifTool()
        except ValueError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.photo = self.folder / "photo.jpg"
        image = Image.new("RGB", (16, 16), (55, 100, 160))
        exif = Image.Exif()
        exif[36867] = "2024:01:01 11:01:00"
        exif[306] = "2024:01:01 11:01:00"
        image.save(self.photo, exif=exif)

    def test_metadata_reads_do_not_modify_original_bytes(self):
        before = self.photo.read_bytes()
        tags = self.tool.read(self.photo)
        self.assertEqual(self.photo.read_bytes(), before)
        self.assertEqual(tags["DateTimeOriginal"], "2024:01:01 11:01:00")
        self.assertEqual(photo_time(tags, camera_zone("Europe/Berlin"), 0)[0], utc_time("2024-01-01T10:01:00Z"))
        self.assertFalse(hasattr(self.tool, "write"))

    def test_real_http_preview_review_and_in_place_update(self):
        original_bytes = self.photo.read_bytes()
        asset = {"id": "12345678-1234-4234-8234-123456789abd", "type": "IMAGE", "originalFileName": "photo.jpg",
                 "fileCreatedAt": "2024-01-01T10:01:00.000Z", "fileModifiedAt": "2024-01-01T10:01:00.000Z",
                 "localDateTime": "2024-01-01T11:01:00.000Z", "checksum": sha1(self.photo), "exifInfo": {}}
        mutations = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send_json(self, data):
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                if self.path == "/api/server/version":
                    self.send_json({"major": 3, "minor": 2, "patch": 0})
                elif "/albums/" in self.path:
                    self.send_json({"albumName": "Trip", "assetCount": 1})
                elif self.path.endswith("/original"):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(original_bytes)))
                    self.end_headers()
                    self.wfile.write(original_bytes)
                else:
                    self.send_json(asset)

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/search/metadata":
                    self.send_error(400)
                    return
                owner.assertEqual(data["filter"]["albumIds"]["any"], ["12345678-1234-4234-8234-123456789abc"])
                self.send_json({"assets": {"items": [copy.deepcopy(asset)], "nextCursor": None}})

            def do_PUT(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mutations.append(data)
                asset["exifInfo"] = {"latitude": data["latitude"], "longitude": data["longitude"]}
                self.send_response(204)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Immich(f"http://127.0.0.1:{server.server_port}/albums/12345678-1234-4234-8234-123456789abc", "test-key")
        self.addCleanup(client.close)
        plan = backup(client, self.folder / "runs", lambda _: None)
        self.assertEqual(plan.stage, "backed_up")
        match_images(plan, self.tool, [FIXTURES / "track.gpx"], Options(timezone="Europe/Berlin"), lambda _: None)
        self.assertEqual((len(plan.ready), mutations), (1, []))
        self.assertEqual(plan.photos[0].original.read_bytes(), original_bytes)
        self.assertAlmostEqual(plan.photos[0].match.point.latitude, 52.001)
        old_dates = {key: asset[key] for key in ("fileCreatedAt", "fileModifiedAt", "localDateTime")}
        apply(client, plan, lambda _: None)
        self.assertEqual(plan.photos[0].status, "updated")
        self.assertEqual(plan.photos[0].verification, "pending")
        verify(client, plan, lambda _: None)
        self.assertEqual(plan.photos[0].status, "verified")
        self.assertEqual(len(mutations), 1)
        self.assertEqual(set(mutations[0]), {"ids", "latitude", "longitude"})
        self.assertEqual({key: asset[key] for key in old_dates}, old_dates)
        self.assertEqual(asset["checksum"], sha1(self.photo))
        self.assertFalse((plan.folder / "taggedImages").exists())
        second = backup(client, self.folder / "runs", lambda _: None)
        match_images(second, self.tool, [FIXTURES / "track.gpx"], Options(timezone="Europe/Berlin"), lambda _: None)
        self.assertEqual(second.photos[0].status, "skipped")
        self.assertTrue(second.photos[0].original.is_file())
        self.assertEqual(len(mutations), 1)


if __name__ == "__main__":
    unittest.main()
