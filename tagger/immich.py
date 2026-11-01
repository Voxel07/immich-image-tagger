"""Small, explicit client for the Immich v2/v3 API."""
import base64
import hashlib
import math
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import requests

from .gpx import Match


def album_address(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a full http(s) album URL without embedded credentials")
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) < 3 or parts[-2] != "albums":
        raise ValueError("Use the album's /albums/<UUID> URL, not a public /share/ link")
    try:
        album_id = str(UUID(parts[-1]))
    except ValueError as exc:
        raise ValueError("The album URL must end in an album UUID") from exc
    prefix = "/".join(parts[:-2]).removesuffix("/api")
    return urlunsplit((parsed.scheme, parsed.netloc, prefix + "/api", "", "")), album_id


def sha1(path: Path) -> str:
    with path.open("rb") as stream:
        return base64.b64encode(hashlib.file_digest(stream, "sha1").digest()).decode("ascii")


class Immich:
    def __init__(self, album_url: str, api_key: str):
        self.base, self.album_id = album_address(album_url)
        if not api_key.strip():
            raise ValueError("An Immich API key is required")
        self._key = api_key.strip()
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": self._key, "Accept": "application/json"})
        self.version: tuple[int, int, int] | None = None

    def close(self):
        self.session.close()

    def _request(self, method: str, endpoint: str, **kwargs):
        try:
            response = self.session.request(method, self.base + endpoint, timeout=(10, 180),
                                            allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise ValueError(f"Immich {method} {endpoint}: {str(exc).replace(self._key, '[redacted]')}") from exc
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            hint = {401: "Check your API key.", 403: "Check key permissions and album access.",
                    404: "Check the album URL and Immich version."}.get(status, "Check the server logs.")
            if 300 <= status < 400:
                hint = "Use the final server URL; redirects are refused to protect the API key."
            raise ValueError(f"Immich {method} {endpoint}: HTTP {status}. {hint}")
        return response

    def _json(self, method: str, endpoint: str, **kwargs):
        with self._request(method, endpoint, **kwargs) as response:
            try:
                return response.json()
            except requests.exceptions.JSONDecodeError as exc:
                raise ValueError(f"Immich {endpoint} returned invalid JSON; check the server URL") from exc

    def album(self) -> tuple[str, list[dict]]:
        version = self._json("GET", "/server/version")
        try:
            self.version = tuple(int(version[key]) for key in ("major", "minor", "patch"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Unrecognized Immich server version response") from exc
        if self.version[0] not in {2, 3}:
            raise ValueError("This tool supports Immich v2 and v3")
        album = self._json("GET", f"/albums/{self.album_id}")
        # v2 includes assets in the album response; v3 requires paginated search.
        if isinstance(album.get("assets"), list) and len(album["assets"]) == album.get("assetCount", len(album["assets"])):
            return album.get("albumName", "Album"), album["assets"]
        assets = []
        seen_ids = set()
        seen_pages = set()
        body = {"size": 500, "withExif": True, "withStacked": True}
        if self.version >= (3, 2, 0):
            body["filter"] = {"albumIds": {"any": [self.album_id]}}
        else:
            body.update({"albumIds": [self.album_id], "page": 1})
        while True:
            result = self._json("POST", "/search/metadata", json=body)["assets"]
            items = result["items"]
            for item in items:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    assets.append(item)
            if self.version >= (3, 2, 0):
                token = result.get("nextCursor")
                if not token:
                    break
                body["cursor"] = token
            else:
                token = result.get("nextPage")
                if not token:
                    break
                body["page"] = int(token)
            if token in seen_pages or not items:
                raise ValueError("Immich returned a repeated/empty search page; refusing an incomplete album")
            seen_pages.add(token)
        return album.get("albumName", "Album"), assets

    def download(self, asset_id: str, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._request("GET", f"/assets/{asset_id}/original", stream=True) as response:
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
            temporary.replace(destination)
        except requests.RequestException as exc:
            raise ValueError("The original download was interrupted; download the backups again") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def check_original(self, asset: dict, backup_checksum: str) -> dict:
        asset_id = asset["id"]
        before = self._json("GET", f"/assets/{asset_id}")
        if before.get("checksum") != backup_checksum:
            raise ValueError("The original in Immich has changed since backup; download a new backup")
        dates = ("fileCreatedAt", "fileModifiedAt", "localDateTime")
        for field in dates:
            if asset.get(field) is not None and not same_date(asset[field], before.get(field)):
                raise ValueError(f"{field} changed since backup; download a new backup")
        if before.get("isTrashed") or before.get("isOffline"):
            raise ValueError("Asset is now offline or trashed; download a new backup before proceeding")
        return before

    def update_location(self, asset_id: str, match: Match):
        # Only GPS fields are sent. In particular, no date, timezone, or file is sent.
        with self._request("PUT", "/assets", json={"ids": [asset_id],
                           "latitude": match.point.latitude, "longitude": match.point.longitude}):
            pass

    def verify_location(self, asset: dict, match: Match, backup_checksum: str):
        asset_id = asset["id"]
        after = self._json("GET", f"/assets/{asset_id}")
        exif = after.get("exifInfo") or {}
        for field, expected in (("latitude", match.point.latitude), ("longitude", match.point.longitude)):
            actual = exif.get(field)
            if actual is None or not math.isclose(float(actual), expected, abs_tol=1e-6):
                raise ValueError(f"Immich GPS verification failed ({field}); inspect the asset before retrying")
        if after.get("id") != asset_id or after.get("checksum") != backup_checksum:
            raise ValueError("Asset identity/file changed during update; inspect the asset")
        for field in ("fileCreatedAt", "fileModifiedAt", "localDateTime"):
            if asset.get(field) is not None and not same_date(asset[field], after.get(field)):
                raise ValueError(f"Date verification failed ({field}); inspect the asset")


def same_date(first, second) -> bool:
    if first == second:
        return True
    try:
        return datetime.fromisoformat(first.replace("Z", "+00:00")) == datetime.fromisoformat(second.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return False
