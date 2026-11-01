# Immich GPX Tagger

A local desktop app with a four-stage workflow:

1. **Download image originals** to keep local backups.
2. **Match GPX timestamps** to the backed-up images, then **review** the proposed locations and statistics.
3. **Update location metadata** on the existing images in Immich.
4. **Verify the changes** by reading the images' metadata back from Immich.

The app does not tag local images or create `taggedImages` copies. Immich keeps one asset per image, with the same ID and photo date. Updates send only asset IDs, latitude, and longitude. Immich stores these edits in its database and XMP sidecars; the original image binary remains unchanged. See the [Immich FAQ](https://docs.immich.app/FAQ/) and [API specification](https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json).

## Requirements

- Python **3.11 or newer**, with Tkinter for the desktop interface.
- [ExifTool](https://exiftool.org/index.html), used **only to read** timestamps and existing GPS from backups. Backup downloads do not require ExifTool.
- An **Immich v2 or v3** server and a normal `/albums/<UUID>` album URL.
- An API key with `album.read`, `asset.read`, `asset.download`, and `asset.update`, belonging to a user who can edit the images. Upload and delete permissions are not needed.
- GPX tracks with timestamps. Timestamped routes and waypoints also work.

All available image originals in the album are downloaded, including images that already have GPS and RAW files. Matching requires a timestamp that ExifTool can read from the file. Videos, offline images, and trashed images are listed as skipped. Downloads and image reads are checked with real JPEG files in the integration tests.

## Windows setup

Open PowerShell in the project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Follow the [ExifTool Windows instructions](https://exiftool.org/install.html): extract the Windows ZIP, rename `exiftool(-k).exe` to `exiftool.exe`, and keep `exiftool_files` beside it. Add the executable's directory to `PATH`, enter its full path in the app, or extract the portable package under `.tools`. The app detects `.tools/exiftool.exe` and `.tools/<package-folder>/exiftool.exe`.

Double-click **`start.bat`**, or run:

```powershell
.\.venv\Scripts\python.exe app.py
```

This working copy already has a local `.venv` and portable ExifTool under `.tools`. Both are excluded by `.gitignore`.

For macOS/Linux, install Python, Tkinter, and ExifTool using your system's package tools, then:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

The CLI works without Tkinter.

## Desktop workflow

### 1. Download backups

Enter the album's full URL, API key, and backups/reports folder. Use a normal album link such as `https://photos.example.com/albums/12345678-1234-4234-8234-123456789abc`; public `/share/` links are not supported.

Click **1. Download backups**. The app downloads all available image originals to a new run folder, records their checksums, and checks them against Immich's checksums when available. It saves the original Immich metadata in the report as well. No GPX file or ExifTool installation is needed for this step.

The entire download pass finishes before matching starts. Failed downloads are reported, and those photos cannot be updated. Images that already have GPS are still backed up.

### 2. Match GPX, then review

Choose one or more GPX files. Set the camera timezone to the timezone the camera clock used, such as `Europe/Berlin`. An embedded EXIF offset takes priority; the default fallback timezone is `UTC`.

Set a clock correction if needed, then click **2. Match GPX / review**. This step reads the local backups and proposes coordinates without changing any local image or Immich metadata.

Review the image rows and summary statistics. **Open run folder** opens the originals and `report.csv`/`report.json`. Select rows and click **Exclude selected** to leave those matches unchanged; Ctrl/Shift supports multiple selection.

The summary cards show **Matched**, **Skipped**, **No GPX match**, and **Errors**, updating as each image is processed. Matched includes matches you later exclude; the smaller summary also shows excluded, ready, backed-up, and verified counts. The settings collapse after matching to give the review table more space; use **Show settings** to adjust them.

**Preview map ↗** opens a read-only map in your browser. It shows proposed locations and existing GPS as different marker colors, with GPX tracks for context. Click a marker or filename to see its time, coordinates, matching method, and a local photo preview for browser-supported formats. Search filenames, filter by result, toggle GPX tracks, or use **Fit all locations**. Double-click a desktop table row to focus that photo on the map. Unmatched photos remain in the list without a guessed location. Exclude photos in the desktop app; the open map refreshes automatically.

Changing GPX files or matching settings disables updating until you run step 2 again. **Existing backups are reused** when rematching in the same desktop session. Changing the album, API key, or output folder requires starting step 1 again. Matching and review are locked once updates start; start a new backup run for further edits.

Photos with existing GPS are skipped during matching unless **Include photos that already have GPS** is enabled. Coordinates equal to zero count as valid existing GPS.

### 3. Update location data in Immich

Click **3. Update + 4. Verify** after reviewing. Only remaining ready matches are updated.

For each image, the app checks the local backup's checksum and confirms that the original file and photo dates in Immich have not changed since the backup. It records the update attempt before sending the GPS update. No dates, timezones, files, or duplicate assets are sent to Immich.

The update pass finishes before the final verification pass starts. At this point an accepted update is marked **updated**, with verification still pending.

### 4. Verify changes

Verification runs automatically as a separate pass after step 3. It reads each attempted update back from Immich and checks:

- Latitude and longitude match the reviewed location.
- Asset ID and original file checksum are unchanged.
- `fileCreatedAt`, `fileModifiedAt`, and `localDateTime` are unchanged.

Successful checks are marked **verified**; failures are marked **verification_failed**, with a per-image reason. **4. Verify again** repeats only these read-back checks, without sending another location update.

An update request that times out may already have been applied by the server. Such attempts are included in verification; a matching read-back confirms the outcome. Preflight failures, such as a missing backup, are not sent to Immich and are not marked verified. Check the report before retrying failures. There is no automatic rollback.

The API key stays in memory and is not saved to reports. You can set `IMMICH_API_KEY` before launching to prefill it. Verification checks the API state; Immich's background sidecar-writing jobs may finish later.

### Progress and map availability

The progress panel highlights the current workflow stage and shows processed/total counts, percentage, current filename, elapsed time, and an estimated time remaining for that stage. Updates and verification have separate totals. Before the album size is known, the bar shows a preparation animation. Skips and errors count as processed items, and their result counts remain visible.

The map runs on a temporary, randomly addressed **127.0.0.1** server while the desktop app is open. It does not receive your API key or make changes to Immich. Only the known backup photos needed for clicked previews can be served. Browser support determines which image formats can be displayed; other formats still have map markers and metadata.

The Leaflet map library is bundled locally under `tagger/assets/leaflet` with its license. Street-map tiles load from OpenStreetMap, which receives normal browser tile requests for the area you view. An internet connection is needed for street-map backgrounds; markers and GPX tracks still work when tiles are unavailable. **Hide street map** turns off further tile requests. No map tiles are downloaded in bulk or bundled with the app. See the [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/).

## Local folders and reports

Each download run creates a directory under `runs` or the selected output directory:

```text
runs/
  20261101T120000Z-a1b2c3d4/
    original/
      <asset-token>/IMG_1234.JPG
      <asset-token>/IMG_5678.JPG
    noMatchingGPX/
      <asset-token>/IMG_5678.JPG
    report.csv
    report.json
```

- **`original`** contains untouched downloaded backups. Each available image is backed up before matching, even if it is later skipped or excluded.
- **`noMatchingGPX`** contains unchanged copies of images without a usable timestamp or a GPX match within the configured limits. Their originals remain in `original`. Rematching removes stale unmatched copies from the current run when the results change.
- **`report.csv`** lists backups, matching results, proposed coordinates, verification status, and reasons for unmatched/skipped/failed images.
- **`report.json`** additionally includes the workflow stage, statistics, checksums, original metadata, update attempts, and verification results. It is saved throughout every stage.

No `taggedImages` folder is created. Older run folders, if any, are left as they were. Asset-token subdirectories prevent filename collisions. Files remain until you delete them; reports cannot currently be resumed after restarting the app.

Statistics include **total assets, backed up, matched, unmatched, skipped, excluded, ready, updated, verified, awaiting verification, verification failures, and errors**. Counts such as matched, updated, and verified intentionally overlap. A successful final run has every attempted update verified, with zero errors. Empty albums produce zero-count reports without changes.

## Matching rules and photo dates

The app reads embedded `DateTimeOriginal`, including subsecond precision when present, falling back to embedded `CreateDate`. It does not guess capture time from filesystem dates.

An embedded UTC offset takes priority; otherwise the camera timezone is used, including the rules for daylight-saving time on that date. Ambiguous or nonexistent times at a daylight-saving transition are reported as unmatched.

Clock correction is **added** to the photo time only for matching. If the camera was two minutes fast, use `-120` seconds. The original image date and the date in Immich stay unchanged.

Matching uses:

1. An exact GPX timestamp, when available.
2. Otherwise, linear interpolation between surrounding points in the same segment if their gap is at most **300 seconds** by default.
3. Otherwise, the nearest recorded point if it is within **60 seconds** by default.
4. Otherwise, no match and no update.

Interpolation does not cross files, track segments, or untimed points. For overlapping tracks, exact matches take priority, followed by the shortest valid interpolation interval, then the closest recorded point. Nearest-point matching can use a point just outside the track's time range if it is within the configured limit. Set its limit to `0` to disable that fallback; set the interpolation gap to `0` for exact/nearest matching only.

Only latitude and longitude are updated. GPX elevation may appear in the detailed match report but is not written to Immich. Photo-date sorting stays unchanged. An album sorted by last modification can reorder because its metadata was edited.

## Command-line usage

Back up, match, and produce a review without changing Immich:

```powershell
$env:IMMICH_API_KEY = 'your-api-key'
.\.venv\Scripts\python.exe app.py `
  --album 'https://photos.example.com/albums/12345678-1234-4234-8234-123456789abc' `
  --gpx '.\tracks\day1.gpx' '.\tracks\day2.gpx' `
  --camera-timezone Europe/Berlin
```

Omit the environment variable to be prompted for the key without displaying it. Add `--apply` to pause at the review stage and ask for **`APPLY`**. Only then does the command update locations and run the separate verification pass:

```powershell
.\.venv\Scripts\python.exe app.py `
  --album 'https://photos.example.com/albums/12345678-1234-4234-8234-123456789abc' `
  --gpx '.\tracks\day1.gpx' `
  --camera-timezone Europe/Berlin `
  --clock-offset -120 `
  --tolerance 60 `
  --max-gap 300 `
  --apply
```

Omit `--gpx` for a backup-only run. Each CLI invocation creates a new run; use the desktop session to rematch the same backups and exclude individual rows.

Other options: `--output '.\my-runs'`, `--overwrite-gps`, `--exiftool 'C:\Tools\ExifTool\exiftool.exe'`, and `--help`. On macOS/Linux use `.venv/bin/python` and your shell's line continuation. Commands return `1` for processing or verification failures, `0` otherwise (including unmatched/skipped images), or `130` on interruption.

## Troubleshooting and tests

- **ExifTool not found**: stage 1 still works. Before stage 2, enter its full executable path or install it as described above.
- **401 / 403**: check the key permissions and the user's access to the album and its photos.
- **404 / redirect / invalid JSON**: use the final normal album URL. Redirects are refused so the API key cannot be forwarded to another host.
- **Few matches**: check the reported UTC times, camera timezone, clock correction, and GPX coverage before increasing matching limits.
- **Backup or photo dates changed**: download a fresh backup before updating.
- **Verification failed**: inspect the per-image reason and the image in Immich. Use **4. Verify again** to recheck without repeating updates.
- **Tkinter unavailable**: install your Python distribution's Tk package, or use the CLI.

Run the tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover stage ordering, backup reuse, matching and timezones, review exclusions, missing backups, separate update and verification passes, uncertain request outcomes, unchanged dates, and failed verification. Integration tests generate a JPEG, read it with ExifTool without modifying it, and exercise the stages against a local HTTP server. They are skipped when ExifTool or Pillow is unavailable. No real Immich account is used by the tests.

Progress tests also check live counts and empty albums. Map tests check marker coordinates, missing/invalid GPS, preview refreshes, and access to only the map's known local images. The desktop layout and browser map are additionally checked with synthetic photos.
