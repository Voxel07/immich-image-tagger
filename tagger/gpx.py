"""Timestamp matching without connecting separate tracks or recording gaps."""
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

UTC = timezone.utc


def utc_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError(f"Timestamp needs a UTC offset: {value}")
    return result.astimezone(UTC)


@dataclass(frozen=True)
class Point:
    time: datetime
    latitude: float
    longitude: float
    elevation: float | None = None


@dataclass(frozen=True)
class Match:
    point: Point
    method: str
    distance_seconds: float
    source: str


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Track:
    def __init__(self, files: list[Path]):
        self.segments: list[tuple[str, list[Point], list[datetime]]] = []
        self.untimed_points = 0
        for path in files:
            try:
                root = ET.parse(path).getroot()
                if local_name(root.tag) != "gpx":
                    raise ValueError("Not a GPX document")
                for element in root.iter():
                    name = local_name(element.tag)
                    if name in {"trkseg", "rte"}:
                        self._segment(path, list(element))
                    elif name == "wpt":
                        self._segment(path, [element])
            except (OSError, ET.ParseError, ValueError, KeyError, TypeError, DefusedXmlException) as exc:
                raise ValueError(f"Cannot read GPX {path.name}: {exc}") from exc
        if not self.segments:
            raise ValueError("No timed GPX points found. Export tracks with timestamps.")

    def _segment(self, path: Path, elements: list):
        points: list[Point] = []
        for element in elements:
            if local_name(element.tag) not in {"trkpt", "rtept", "wpt"}:
                continue
            fields = {local_name(c.tag): c.text for c in element}
            if not fields.get("time"):
                self.untimed_points += 1
                # An untimed point also breaks continuity for interpolation.
                self._store(path, points)
                points = []
                continue
            lat, lon = float(element.attrib["lat"]), float(element.attrib["lon"])
            ele = float(fields["ele"]) if fields.get("ele") else None
            if not isfinite(lat) or not -90 <= lat <= 90:
                raise ValueError("Invalid GPX latitude")
            if not isfinite(lon) or not -180 <= lon <= 180:
                raise ValueError("Invalid GPX longitude")
            if ele is not None and not isfinite(ele):
                raise ValueError("Invalid GPX elevation")
            point = Point(utc_time(fields["time"]), lat, lon, ele)
            if points and point.time < points[-1].time:
                raise ValueError("GPX timestamps go backwards within a segment")
            if points and point.time == points[-1].time:
                if point != points[-1]:
                    raise ValueError("Conflicting GPX positions at the same timestamp")
                continue
            points.append(point)
        self._store(path, points)

    def _store(self, path: Path, points: list[Point]):
        if points:
            self.segments.append((path.name, points, [p.time for p in points]))

    def match(self, when: datetime, tolerance: float, max_gap: float) -> Match | None:
        interpolations = []
        nearest = []
        for source, points, times in self.segments:
            index = bisect_left(times, when)
            if index < len(points) and times[index] == when:
                return Match(points[index], "exact", 0, source)
            for i in (index - 1, index):
                if 0 <= i < len(points):
                    distance = abs((times[i] - when).total_seconds())
                    if distance <= tolerance:
                        nearest.append(Match(points[i], "nearest", distance, source))
            if 0 < index < len(points):
                a, b = points[index - 1], points[index]
                gap = (b.time - a.time).total_seconds()
                if gap <= max_gap:
                    fraction = (when - a.time).total_seconds() / gap
                    # The short path also works for a track crossing the date line.
                    delta_lon = (b.longitude - a.longitude + 180) % 360 - 180
                    lon = (a.longitude + fraction * delta_lon + 180) % 360 - 180
                    elevation = None
                    if a.elevation is not None and b.elevation is not None:
                        elevation = a.elevation + fraction * (b.elevation - a.elevation)
                    point = Point(when, a.latitude + fraction * (b.latitude - a.latitude), lon, elevation)
                    interpolations.append((gap, Match(point, "interpolated", min(
                        (when - a.time).total_seconds(), (b.time - when).total_seconds()), source)))
        if interpolations:
            return min(interpolations, key=lambda item: item[0])[1]
        return min(nearest, key=lambda match: match.distance_seconds) if nearest else None
