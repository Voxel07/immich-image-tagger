"""Read camera timestamps using ExifTool without modifying image files."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .gpx import UTC


def camera_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown camera timezone '{name}'. Try Europe/Berlin or UTC; install tzdata.") from exc


def photo_time(tags: dict, zone: ZoneInfo, offset_seconds: float) -> tuple[datetime, str]:
    for tag, offset_tag in (("SubSecDateTimeOriginal", "OffsetTimeOriginal"),
                            ("DateTimeOriginal", "OffsetTimeOriginal"),
                            ("SubSecCreateDate", "OffsetTimeDigitized"),
                            ("CreateDate", "OffsetTimeDigitized")):
        raw = str(tags.get(tag) or "").strip()
        if not raw:
            continue
        # EXIF uses YYYY:MM:DD; ISO dates occasionally occur in XMP.
        raw = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", raw)
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            origin = tag
            if when.tzinfo is None and tags.get(offset_tag):
                when = datetime.fromisoformat(raw + str(tags[offset_tag]))
            if when.tzinfo is None:
                # Never silently guess during ambiguous/nonexistent DST hours.
                possibilities = {when.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                                 for fold in (0, 1)
                                 if when.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                                 .astimezone(zone).replace(tzinfo=None) == when}
                if len(possibilities) != 1:
                    raise ValueError("Camera time is ambiguous or nonexistent at a daylight-saving transition; add an EXIF offset")
                when = possibilities.pop()
                origin += f" ({zone.key})"
            return when.astimezone(UTC) + timedelta(seconds=offset_seconds), origin
        except ValueError as exc:
            # A present but invalid timestamp must not silently fall back to a file date.
            raise ValueError(f"Invalid {tag}: {exc}") from exc
    raise ValueError("No embedded DateTimeOriginal or CreateDate; file modification time is not used")


def has_gps(tags: dict) -> bool:
    return tags.get("GPSLatitude") is not None and tags.get("GPSLongitude") is not None


class ExifTool:
    def __init__(self, executable: str = "exiftool"):
        self.executable = shutil.which(executable)
        if not self.executable and executable == "exiftool":
            # Also support a portable install kept entirely inside the project.
            tools = Path(__file__).resolve().parent.parent / ".tools"
            candidates = sorted(tools.glob("*/exiftool.exe"))
            candidates += [tools / "exiftool.exe"]
            self.executable = next((str(path) for path in candidates if path.is_file()), None)
        if not self.executable:
            raise ValueError("ExifTool not found. Install it and add it to PATH, or supply its executable path. See README.md.")
        self._run(["-ver"])

    def _run(self, args: list[str]) -> str:
        try:
            result = subprocess.run([self.executable, *args], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"ExifTool could not run: {exc}") from exc
        if result.returncode:
            raise ValueError(f"ExifTool: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout

    def read(self, path: Path) -> dict:
        output = self._run(["-j", "-n", "-DateTimeOriginal", "-SubSecDateTimeOriginal",
                            "-OffsetTimeOriginal", "-CreateDate", "-SubSecCreateDate",
                            "-OffsetTimeDigitized", "-GPSLatitude", "-GPSLongitude",
                            "-GPSLatitudeRef", "-GPSLongitudeRef", "-GPSAltitude", "-GPSAltitudeRef",
                            str(path.resolve())])
        tags = json.loads(output)[0]
        if tags.get("Error"):
            raise ValueError(f"ExifTool: {tags['Error']}")
        return tags

