"""Four stages: back up, match/review, update locations, verify changes."""
import csv
import hashlib
import json
import math
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .gpx import Match, Track
from .immich import Immich, sha1
from .metadata import ExifTool, camera_zone, has_gps, photo_time


@dataclass
class Options:
    timezone: str = "UTC"
    clock_offset: float = 0
    tolerance: float = 60
    max_gap: float = 300
    overwrite_gps: bool = False
    output: Path = Path("runs")

    def validate(self):
        camera_zone(self.timezone)
        for key in ("clock_offset", "tolerance", "max_gap"):
            if not math.isfinite(getattr(self, key)):
                raise ValueError(f"{key} must be a finite number")
        if self.tolerance < 0 or self.max_gap < 0:
            raise ValueError("Tolerance and interpolation gap cannot be negative")


@dataclass
class Photo:
    asset: dict
    filename: str
    status: str
    detail: str = ""
    capture_time: str = ""
    match: Match | None = None
    original: Path | None = None
    original_checksum: str = ""
    unmatched: Path | None = None
    backup_status: str = "pending"
    backup_detail: str = ""
    update_attempted: bool = False
    update_succeeded: bool = False
    before_update: dict | None = None
    update_error: str = ""
    verification: str = "not_requested"


@dataclass
class Plan:
    album_name: str
    folder: Path
    options: Options
    photos: list[Photo] = field(default_factory=list)
    stage: str = "backup"
    routes: list[dict] = field(default_factory=list)

    @property
    def ready(self) -> list[Photo]:
        return [photo for photo in self.photos if photo.status == "ready"]

    @property
    def statistics(self) -> dict:
        return {"total_assets": len(self.photos),
                "backed_up": sum(p.backup_status == "backed_up" for p in self.photos),
                "matched": sum(p.match is not None for p in self.photos),
                "ready": len(self.ready),
                "updated": sum(p.update_succeeded for p in self.photos),
                "verified": sum(p.verification == "verified" for p in self.photos),
                "verification_pending": sum(p.verification == "pending" for p in self.photos),
                "verification_failed": sum(p.verification == "failed" for p in self.photos),
                "excluded": sum(p.status == "excluded" for p in self.photos),
                "unmatched": sum(p.status == "unmatched" for p in self.photos),
                "skipped": sum(p.status == "skipped" for p in self.photos),
                "errors": sum(p.status in {"error", "verification_failed"} for p in self.photos)}


@dataclass(frozen=True)
class Progress:
    stage: str
    completed: int
    total: int | None
    current: str = ""
    statistics: dict = field(default_factory=dict)


def report_progress(callback, plan: Plan, stage: str, completed: int, total: int, current: str = ""):
    if callback:
        callback(Progress(stage, completed, total, current, plan.statistics))


def save_json(path: Path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def save_report(plan: Plan):
    save_json(plan.folder / "report.json", {
        "album": plan.album_name, "stage": plan.stage,
        "options": asdict(plan.options), "statistics": plan.statistics,
        "photos": [{"asset_id": p.asset["id"], "filename": p.filename,
                    "status": p.status, "detail": p.detail, "capture_time_utc": p.capture_time,
                    "match": asdict(p.match) if p.match else None,
                    "original": p.original, "no_matching_gpx": p.unmatched,
                    "backup_status": p.backup_status, "original_checksum": p.original_checksum,
                    "original_immich_metadata": p.asset, "before_update": p.before_update,
                    "update_attempted": p.update_attempted, "update_succeeded": p.update_succeeded,
                    "update_error": p.update_error, "verification": p.verification}
                   for p in plan.photos]})
    with (plan.folder / "report.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Asset ID", "Filename", "Backup", "Status", "Photo time UTC (for matching)",
                         "Latitude", "Longitude", "Match method", "Verification", "Details"])
        for p in plan.photos:
            writer.writerow([p.asset["id"], p.filename, p.backup_status, p.status, p.capture_time,
                             p.match.point.latitude if p.match else "", p.match.point.longitude if p.match else "",
                             p.match.method if p.match else "", p.verification, p.detail])


def summary(plan: Plan) -> str:
    s = plan.statistics
    return (f"{s['total_assets']} assets: {s['backed_up']} backed up, {s['matched']} matched, "
            f"{s['unmatched']} unmatched, {s['skipped']} skipped, {s['excluded']} excluded, {s['errors']} errors; "
            f"{s['ready']} ready, {s['updated']} updated, {s['verified']} verified, "
            f"{s['verification_pending']} awaiting verification.")


def backup(client: Immich, output: Path, log: Callable[[str], None] = print,
           progress: Callable[[Progress], None] | None = None) -> Plan:
    """Download all available image originals before reading GPX or matching."""
    log("1. Downloading original images for backup")
    if progress:
        progress(Progress("backup", 0, None, "Loading album…"))
    name, assets = client.album()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    folder = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    folder.mkdir()
    for subfolder in ("original", "noMatchingGPX"):
        (folder / subfolder).mkdir()
    plan = Plan(name, folder, Options(output=output))
    for asset in assets:
        filename = re.split(r"[/\\]", asset.get("originalFileName") or asset["id"])[-1]
        if filename in {"", ".", ".."}:
            filename = hashlib.sha256(asset["id"].encode()).hexdigest()[:24] + ".image"
        plan.photos.append(Photo(asset, filename, "pending"))
    save_report(plan)
    report_progress(progress, plan, "backup", 0, len(plan.photos))
    for index, photo in enumerate(plan.photos, 1):
        report_progress(progress, plan, "backup", index - 1, len(plan.photos), photo.filename)
        log(f"Backup [{index}/{len(plan.photos)}] {photo.filename}")
        try:
            if photo.asset.get("type") != "IMAGE":
                photo.status, photo.detail = "skipped", "Not a photo"
            elif photo.asset.get("isOffline") or photo.asset.get("isTrashed"):
                photo.status, photo.detail = "skipped", "Offline or trashed asset"
            else:
                token = hashlib.sha256(photo.asset["id"].encode()).hexdigest()[:24]
                photo.original = folder / "original" / token / photo.filename
                client.download(photo.asset["id"], photo.original)
                photo.original_checksum = sha1(photo.original)
                if photo.asset.get("checksum") and photo.original_checksum != photo.asset["checksum"]:
                    raise ValueError("Original download checksum does not match Immich")
                photo.status, photo.detail = "backed_up", "Original downloaded; checksum recorded"
        except (ValueError, OSError) as exc:
            photo.status, photo.detail = "error", str(exc)
        photo.backup_status, photo.backup_detail = photo.status, photo.detail
        save_report(plan)
        log(f"  {photo.status}: {photo.detail}")
        report_progress(progress, plan, "backup", index, len(plan.photos), photo.filename)
    plan.stage = "backed_up"
    save_report(plan)
    log(f"Backup complete. {summary(plan)} Files: {folder}")
    return plan


def reset_matches(plan: Plan):
    """Invalidate matching settings while retaining the completed backups."""
    if plan.stage not in {"backed_up", "matching", "review"}:
        raise ValueError("Start a new backup run after updating locations")
    for photo in plan.photos:
        if photo.unmatched:
            root = (plan.folder / "noMatchingGPX").resolve()
            path = photo.unmatched.resolve()
            if not path.is_relative_to(root):
                raise ValueError("Unmatched copy is outside the run folder")
            path.unlink(missing_ok=True)
        photo.status, photo.detail = photo.backup_status, photo.backup_detail
        photo.match, photo.capture_time, photo.unmatched = None, "", None
    plan.stage = "backed_up"
    save_report(plan)


def check_backup(photo: Photo):
    if not photo.original or not photo.original_checksum or sha1(photo.original) != photo.original_checksum:
        raise ValueError("Local original backup is missing or changed; download a new backup before proceeding")


def match_images(plan: Plan, tool: ExifTool, files: list[Path], options: Options,
                 log: Callable[[str], None] = print,
                 progress: Callable[[Progress], None] | None = None) -> Plan:
    options.validate()
    track = Track(files)
    zone = camera_zone(options.timezone)
    reset_matches(plan)
    # The run folder is fixed by stage 1, regardless of subsequent matching options.
    options.output = plan.options.output
    plan.options, plan.stage = options, "matching"
    plan.routes = [{"name": name, "points": [[p.latitude, p.longitude] for p in points]}
                   for name, points, _ in track.segments]
    log("2. Matching GPX timestamps to backed-up images")
    log(f"Read {sum(len(points) for _, points, _ in track.segments)} timed GPX points; "
        f"ignored {track.untimed_points} untimed points.")
    candidates = [photo for photo in plan.photos if photo.backup_status == "backed_up"]
    report_progress(progress, plan, "matching", 0, len(candidates))
    for index, photo in enumerate(candidates, 1):
        report_progress(progress, plan, "matching", index - 1, len(candidates), photo.filename)
        try:
            check_backup(photo)
            existing = photo.asset.get("exifInfo") or {}
            if not options.overwrite_gps and all(existing.get(k) is not None for k in ("latitude", "longitude")):
                photo.status, photo.detail = "skipped", "Already has GPS in Immich (original backed up)"
            else:
                tags = tool.read(photo.original)
                if has_gps(tags) and not options.overwrite_gps:
                    photo.status, photo.detail = "skipped", "Already has embedded GPS (original backed up)"
                else:
                    try:
                        when, origin = photo_time(tags, zone, options.clock_offset)
                    except ValueError as exc:
                        photo.status, photo.detail = "unmatched", str(exc)
                    else:
                        photo.capture_time = when.isoformat()
                        photo.match = track.match(when, options.tolerance, options.max_gap)
                        if photo.match is None:
                            photo.status, photo.detail = "unmatched", "No GPX match within the configured limits"
                        else:
                            photo.status = "ready"
                            photo.detail = f"{photo.match.method}; {origin}; {photo.match.source}"
                    if photo.status == "unmatched":
                        token = photo.original.parent.name
                        photo.unmatched = plan.folder / "noMatchingGPX" / token / photo.filename
                        photo.unmatched.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(photo.original, photo.unmatched)
        except (ValueError, OSError) as exc:
            photo.status, photo.detail = "error", str(exc)
        log(f"  {photo.filename}: {photo.status}: {photo.detail}")
        save_report(plan)
        report_progress(progress, plan, "matching", index, len(candidates), photo.filename)
    plan.stage = "review"
    save_report(plan)
    log(f"2.1 Review: {summary(plan)} Report: {plan.folder / 'report.csv'}")
    return plan


def apply(client: Immich, plan: Plan, log: Callable[[str], None] = print,
          progress: Callable[[Progress], None] | None = None):
    """Stage 3 only. Final verification is deliberately a separate pass."""
    if plan.stage != "review":
        raise ValueError("Match and review the backed-up images before updating Immich")
    plan.stage = "updating"
    save_report(plan)
    log("3. Updating reviewed locations in Immich")
    candidates = plan.ready
    report_progress(progress, plan, "updating", 0, len(candidates))
    for index, photo in enumerate(candidates, 1):
        report_progress(progress, plan, "updating", index - 1, len(candidates), photo.filename)
        try:
            check_backup(photo)
            photo.before_update = client.check_original(photo.asset, photo.original_checksum)
            photo.update_attempted, photo.verification = True, "pending"
            # Persist the intent before sending a request that may time out after applying.
            save_report(plan)
            client.update_location(photo.asset["id"], photo.match)
            photo.update_succeeded = True
            photo.status, photo.detail = "updated", "GPS update accepted; awaiting stage 4 verification"
        except (ValueError, OSError) as exc:
            photo.status, photo.detail, photo.update_error = "error", str(exc), str(exc)
        save_report(plan)
        log(f"  {photo.filename}: {photo.status}: {photo.detail}")
        report_progress(progress, plan, "updating", index, len(candidates), photo.filename)
    plan.stage = "verification_pending"
    save_report(plan)
    log(f"Update stage complete. {summary(plan)}")


def verify(client: Immich, plan: Plan, log: Callable[[str], None] = print,
           progress: Callable[[Progress], None] | None = None):
    """Read back attempted updates, including requests with an uncertain outcome."""
    if plan.stage not in {"verification_pending", "verified", "verification_failed"}:
        raise ValueError("Update the reviewed locations before verifying changes")
    log("4. Verifying locations, asset IDs, checksums and unchanged photo dates")
    candidates = [photo for photo in plan.photos if photo.update_attempted]
    report_progress(progress, plan, "verifying", 0, len(candidates))
    for index, photo in enumerate(candidates, 1):
        report_progress(progress, plan, "verifying", index - 1, len(candidates), photo.filename)
        try:
            client.verify_location(photo.before_update, photo.match, photo.original_checksum)
            photo.verification, photo.status = "verified", "verified"
            photo.update_succeeded = True
            photo.detail = "GPS, asset ID, original checksum and photo dates verified"
            if photo.update_error:
                photo.detail += "; confirmed despite an error from the update request"
        except (ValueError, OSError) as exc:
            photo.verification, photo.status, photo.detail = "failed", "verification_failed", str(exc)
        save_report(plan)
        log(f"  {photo.filename}: {photo.status}: {photo.detail}")
        report_progress(progress, plan, "verifying", index, len(candidates), photo.filename)
    plan.stage = "verification_failed" if any(p.status in {"error", "verification_failed"} for p in plan.photos) else "verified"
    save_report(plan)
    log(f"Verification complete. {summary(plan)} Report: {plan.folder / 'report.json'}")
