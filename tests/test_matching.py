import tempfile
import unittest
from pathlib import Path

from tagger.gpx import Track, utc_time
from tagger.immich import album_address
from tagger.metadata import camera_zone, has_gps, photo_time
from tagger.workflow import Options


def gpx(points, split=False):
    content = ""
    for time, lat, lon, ele in points:
        if content and split:
            content += "</trkseg><trkseg>"
        content += f'<trkpt lat="{lat}" lon="{lon}"><time>{time}</time>'
        if ele is not None:
            content += f"<ele>{ele}</ele>"
        content += "</trkpt>"
    return '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>' + content + "</trkseg></trk></gpx>"


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "track.gpx"
        self.addCleanup(self.temp.cleanup)

    def track(self, points, split=False):
        self.path.write_text(gpx(points, split), encoding="utf-8")
        return Track([self.path])

    def test_exact_interpolated_and_nearest(self):
        track = self.track([("2024-01-01T10:00:00Z", 52, 13, -10),
                            ("2024-01-01T10:02:00Z", 54, 15, 10)])
        match = track.match(utc_time("2024-01-01T10:01:00Z"), 0, 300)
        self.assertEqual((match.method, match.point.latitude, match.point.longitude, match.point.elevation),
                         ("interpolated", 53, 14, 0))
        self.assertEqual(track.match(utc_time("2024-01-01T10:00:00Z"), 0, 0).method, "exact")
        self.assertEqual(track.match(utc_time("2024-01-01T09:59:30Z"), 30, 0).method, "nearest")
        self.assertIsNone(track.match(utc_time("2024-01-01T09:59:29Z"), 30, 300))

    def test_no_interpolation_across_segments_or_long_gaps(self):
        points = [("2024-01-01T10:00:00Z", 52, 13, None), ("2024-01-01T10:02:00Z", 54, 15, None)]
        when = utc_time("2024-01-01T10:01:00Z")
        self.assertIsNone(self.track(points, split=True).match(when, 10, 300))
        self.assertIsNone(self.track(points).match(when, 10, 60))

    def test_multiple_files_and_shortest_interpolation(self):
        self.track([("2024-01-01T10:00:00Z", 0, 0, None), ("2024-01-01T10:02:00Z", 0, 4, None)])
        other = self.path.with_name("other.gpx")
        other.write_text(gpx([("2024-01-01T10:00:30Z", 10, 10, None), ("2024-01-01T10:01:30Z", 12, 12, None)]))
        match = Track([self.path, other]).match(utc_time("2024-01-01T10:01:00Z"), 60, 300)
        self.assertEqual((match.source, match.point.latitude), ("other.gpx", 11))

    def test_date_line(self):
        track = self.track([("2024-01-01T10:00:00Z", 0, 179, None), ("2024-01-01T10:02:00Z", 0, -179, None)])
        self.assertEqual(abs(track.match(utc_time("2024-01-01T10:01:00Z"), 0, 300).point.longitude), 180)

    def test_invalid_track_fails_clearly(self):
        for points in [
            [("2024-01-01T10:00:00", 52, 13, None)],
            [("2024-01-01T10:00:00Z", 91, 13, None)],
            [("2024-01-01T10:00:00Z", "nan", 13, None)],
            [("2024-01-01T10:01:00Z", 52, 13, None), ("2024-01-01T10:00:00Z", 52, 13, None)],
            [("2024-01-01T10:00:00Z", 52, 13, None), ("2024-01-01T10:00:00Z", 53, 13, None)],
        ]:
            with self.subTest(points=points), self.assertRaises(ValueError):
                self.track(points)

    def test_untimed_points_break_interpolation(self):
        self.path.write_text('<gpx><trk><trkseg><trkpt lat="1" lon="1"><time>2024-01-01T10:00:00Z</time></trkpt>'
                             '<trkpt lat="2" lon="2"/><trkpt lat="3" lon="3"><time>2024-01-01T10:02:00Z</time></trkpt>'
                             '</trkseg></trk></gpx>')
        track = Track([self.path])
        self.assertEqual(track.untimed_points, 1)
        self.assertIsNone(track.match(utc_time("2024-01-01T10:01:00Z"), 0, 300))

    def test_xml_entities_are_rejected(self):
        self.path.write_text('<!DOCTYPE gpx [<!ENTITY evil "bad">]><gpx>&evil;</gpx>')
        with self.assertRaises(ValueError):
            Track([self.path])

    def test_timezone_and_correction(self):
        zone = camera_zone("Europe/Berlin")
        when, _ = photo_time({"DateTimeOriginal": "2024:07:01 12:00:00"}, zone, -120)
        self.assertEqual(when.isoformat(), "2024-07-01T09:58:00+00:00")
        when, _ = photo_time({"DateTimeOriginal": "2024:07:01 12:00:00", "OffsetTimeOriginal": "+03:00"}, zone, 0)
        self.assertEqual(when.hour, 9)
        when, _ = photo_time({"SubSecDateTimeOriginal": "2024:07:01 12:00:00.125-04:00"}, zone, 0)
        self.assertEqual((when.hour, when.microsecond), (16, 125000))

    def test_dst_and_missing_dates_are_not_guessed(self):
        for raw in ["2024:10:27 02:30:00", "2024:03:31 02:30:00", "bad"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                photo_time({"DateTimeOriginal": raw}, camera_zone("Europe/Berlin"), 0)
        with self.assertRaises(ValueError):
            photo_time({}, camera_zone("UTC"), 0)
        self.assertTrue(has_gps({"GPSLatitude": 0, "GPSLongitude": 0}))

    def test_album_url_parsing(self):
        identifier = "12345678-1234-4234-8234-123456789abc"
        self.assertEqual(album_address(f"https://photos.test/immich/albums/{identifier}?x=y"),
                         ("https://photos.test/immich/api", identifier))
        self.assertEqual(album_address(f"http://localhost:2283/api/albums/{identifier}/")[0], "http://localhost:2283/api")
        for value in ["file:///albums/" + identifier, "https://user:pass@photos.test/albums/" + identifier,
                      "https://photos.test/share/token", "https://photos.test/albums/not-an-id"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                album_address(value)

    def test_invalid_options(self):
        for options in [Options(tolerance=-1), Options(max_gap=float("nan")), Options(timezone="Nowhere")]:
            with self.assertRaises(ValueError):
                options.validate()


if __name__ == "__main__":
    unittest.main()
