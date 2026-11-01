import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
from tagger.gpx import Match, Point, utc_time
from tagger.workflow import Options, Photo, Plan


class CLITests(unittest.TestCase):
    def run_cli(self, arguments, answer="NO"):
        photo = Photo({"id": "one"}, "photo.jpg", "backed_up",
                      match=Match(Point(utc_time("2024-01-01T10:01:00Z"), 52, 13), "exact", 0, "track.gpx"))
        plan = Plan("Trip", Path("runs/test-run"), Options(), [photo], "backed_up")
        events = []
        def matched(*_):
            events.append("match")
            photo.status, plan.stage = "ready", "review"
            return plan
        with patch("sys.argv", ["app.py", "--album", "https://photos.test/albums/12345678-1234-4234-8234-123456789abc", *arguments]), \
             patch.dict("os.environ", {"IMMICH_API_KEY": "test-key"}), \
             patch("tagger.immich.Immich"), \
             patch("tagger.metadata.ExifTool") as tool, \
             patch("tagger.workflow.backup", side_effect=lambda *_: (events.append("backup"), plan)[1]), \
             patch("tagger.workflow.match_images", side_effect=matched), \
             patch("tagger.workflow.apply", side_effect=lambda *_: events.append("update")), \
             patch("tagger.workflow.verify", side_effect=lambda *_: events.append("verify")), \
             patch("builtins.input", return_value=answer), contextlib.redirect_stdout(io.StringIO()):
            result = app.main()
        return result, events, tool

    def test_backup_only_needs_no_gpx_or_exiftool(self):
        result, events, tool = self.run_cli([])
        self.assertEqual((result, events), (0, ["backup"]))
        tool.assert_not_called()

    def test_review_rejection_never_updates(self):
        result, events, _ = self.run_cli(["--gpx", "track.gpx", "--apply"])
        self.assertEqual((result, events), (0, ["backup", "match"]))

    def test_approved_review_updates_then_verifies(self):
        result, events, _ = self.run_cli(["--gpx", "track.gpx", "--apply"], "APPLY")
        self.assertEqual((result, events), (0, ["backup", "match", "update", "verify"]))


if __name__ == "__main__":
    unittest.main()
