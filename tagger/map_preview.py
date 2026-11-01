"""Read-only map preview served on loopback; credentials never enter the page."""
import json
import math
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .workflow import Plan

ASSETS = Path(__file__).parent / "assets"


def coordinates(latitude, longitude):
    try:
        lat, lon = float(latitude), float(longitude)
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return [lat, lon]
    except (ValueError, TypeError):
        pass
    return None


def map_data(plan: Plan, stale: bool = False) -> dict:
    photos = []
    for index, photo in enumerate(plan.photos):
        location, kind = None, "none"
        if photo.match:
            location = coordinates(photo.match.point.latitude, photo.match.point.longitude)
            kind = "proposed"
        else:
            exif = photo.asset.get("exifInfo") or {}
            location = coordinates(exif.get("latitude"), exif.get("longitude"))
            if location:
                kind = "existing"
        photos.append({"id": str(index), "filename": photo.filename, "status": photo.status,
                       "detail": photo.detail, "time": photo.capture_time,
                       "location": location, "kind": kind,
                       "method": photo.match.method if photo.match else "",
                       "source": photo.match.source if photo.match else ""})
    return {"album": plan.album_name, "stage": plan.stage, "statistics": plan.statistics,
            "stale": stale, "photos": photos, "routes": plan.routes}


class MapPreview:
    def __init__(self):
        self.lock = threading.Lock()
        self.revision = 0
        self.payload = {}
        self.photos = {}
        self.focus = None
        self.token = secrets.token_urlsafe(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                prefix = f"/{owner.token}/"
                path = self.path.split("?", 1)[0]
                if not path.startswith(prefix):
                    self.send_error(404)
                    return
                resource = path[len(prefix):]
                if resource == "data":
                    with owner.lock:
                        data = json.dumps({**owner.payload, "revision": owner.revision, "focus": owner.focus},
                                          allow_nan=False).encode("utf-8")
                    self.respond(data, "application/json; charset=utf-8")
                    return
                if resource.startswith("photo/"):
                    with owner.lock:
                        photo = owner.photos.get(resource.removeprefix("photo/"))
                    if photo:
                        try:
                            self.respond(photo.read_bytes(), mimetypes.guess_type(photo.name)[0] or "image/jpeg")
                            return
                        except OSError:
                            pass
                    self.send_error(404)
                    return
                files = {"": ASSETS / "map.html", "map.js": ASSETS / "map.js", "map.css": ASSETS / "map.css",
                         "leaflet.js": ASSETS / "leaflet/leaflet.js", "leaflet.css": ASSETS / "leaflet/leaflet.css"}
                if resource not in files:
                    self.send_error(404)
                    return
                try:
                    file = files[resource]
                    content_type = {".js": "application/javascript", ".css": "text/css", ".html": "text/html"}[file.suffix]
                    self.respond(file.read_bytes(), content_type + "; charset=utf-8")
                except OSError:
                    self.send_error(404)

            def respond(self, data, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://tile.openstreetmap.org; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{self.token}/"

    def update(self, plan: Plan | None, stale=False, focus: str | None = None):
        data = map_data(plan, stale) if plan else {"album": "", "stage": "", "statistics": {}, "photos": [], "routes": [], "stale": True}
        photos = {}
        if plan:
            root = (plan.folder / "original").resolve()
            for index, photo in enumerate(plan.photos):
                if photo.original and photo.original.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
                    path = photo.original.resolve()
                    if path.is_relative_to(root):
                        photos[str(index)] = path
        for item in data["photos"]:
            item["preview"] = item["id"] in photos
        with self.lock:
            self.payload, self.photos = data, photos
            self.revision += 1
            self.focus = focus

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
