"""Run the desktop application, or use --album and --gpx for the CLI."""
import argparse
import getpass
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="1. Back up images. 2. Match GPX and review. 3. Update Immich GPS. 4. Verify changes.")
    parser.add_argument("--album", help="Full Immich /albums/<UUID> URL; omit to open the desktop window")
    parser.add_argument("--gpx", nargs="+", type=Path, help="One or more GPX files")
    parser.add_argument("--camera-timezone", default="UTC", help="IANA timezone for camera timestamps without an offset (default: UTC)")
    parser.add_argument("--clock-offset", type=float, default=0, help="Seconds to ADD to the photo time before matching")
    parser.add_argument("--tolerance", type=float, default=60, help="Maximum seconds to a nearest GPX point (default: 60)")
    parser.add_argument("--max-gap", type=float, default=300, help="Maximum gap between points for interpolation (default: 300 seconds)")
    parser.add_argument("--overwrite-gps", action="store_true", help="Include photos that already have location metadata")
    parser.add_argument("--output", type=Path, default=Path("runs"), help="Local original backups, noMatchingGPX and reports (default: runs)")
    parser.add_argument("--exiftool", default="exiftool", help="ExifTool executable name or full path")
    parser.add_argument("--apply", action="store_true", help="After review, ask to update Immich GPS, then verify changes; preserves IDs and dates")
    args = parser.parse_args()
    try:
        from tagger.immich import Immich
        from tagger.metadata import ExifTool
        from tagger.workflow import Options, apply, backup, match_images, verify
    except ModuleNotFoundError as exc:
        parser.exit(1, f"Missing dependency: {exc.name}. Run: python -m pip install -r requirements.txt\n")
    if not args.album:
        if len(sys.argv) > 1:
            parser.error("Use --album with CLI options, or launch without arguments for the desktop window")
        try:
            from tagger.gui import launch
            launch()
        except ImportError:
            parser.exit(1, "Tkinter is unavailable. Install your system's Python Tk package or use the CLI.\n")
        return 0
    if args.apply and not args.gpx:
        parser.error("--gpx is required with --apply")
    client = None
    try:
        options = Options(args.camera_timezone, args.clock_offset, args.tolerance, args.max_gap,
                          args.overwrite_gps, args.output)
        key = os.environ.get("IMMICH_API_KEY") or getpass.getpass("Immich API key: ")
        client = Immich(args.album, key)
        plan = backup(client, args.output)
        if not args.gpx:
            print("Backup only: no matching or Immich updates were requested.")
            return int(any(photo.status == "error" for photo in plan.photos))
        tool = ExifTool(args.exiftool)
        match_images(plan, tool, args.gpx, options)
        for photo in plan.ready:
            point = photo.match.point
            print(f"  {photo.filename}: {photo.capture_time} -> {point.latitude:.6f}, {point.longitude:.6f}")
        if args.apply:
            if plan.ready:
                answer = input(f"Review {plan.folder / 'report.csv'}. Type APPLY to update GPS on {len(plan.ready)} existing photos, then verify: ")
                if answer.strip() == "APPLY":
                    apply(client, plan)
                    verify(client, plan)
                else:
                    print("No Immich metadata changed.")
        else:
            print("Preview only. Rerun with --apply to review and apply GPS metadata to existing photos.")
        return int(any(photo.status in {"error", "verification_failed"} for photo in plan.photos))
    except (ValueError, OSError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Inspect the latest report and Immich for any completed GPS updates.", file=sys.stderr)
        return 130
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
