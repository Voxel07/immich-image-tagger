"use strict";
const map = L.map("map", {preferCanvas: true}).setView([30, 0], 2);
const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  updateWhenIdle: true, keepBuffer: 1
}).addTo(map);
const markers = L.layerGroup().addTo(map);
const routes = L.layerGroup().addTo(map);
const colors = {proposed: "#16815c", existing: "#347bd0", excluded: "#939daa", failed: "#d44555"};
let data = null, revision = -1, firstFit = true, selected = null, focusRevision = -1;
let markerById = new Map(), bounds = [], signature = "";
const el = id => document.getElementById(id);
function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function category(photo) {
  if (photo.status === "excluded") return "excluded";
  if (["error", "verification_failed"].includes(photo.status)) return "errors";
  if (photo.kind === "proposed") return "matched";
  if (photo.status === "unmatched") return "unmatched";
  return photo.status === "skipped" ? "skipped" : "other";
}
function colorKey(photo) {
  if (category(photo) === "errors") return "failed";
  return photo.status === "excluded" ? "excluded" : photo.kind;
}
function popup(photo) {
  const box = element("div", undefined, "popup");
  box.append(element("h2", photo.filename));
  box.append(element("p", photo.location.map(n => n.toFixed(6)).join(", ")));
  if (photo.time) box.append(element("p", "Photo time: " + photo.time));
  box.append(element("p", photo.kind === "existing" ? "Existing GPS · no location change proposed" : "GPX location · " + photo.status));
  if (photo.method) box.append(element("p", photo.method + " · " + photo.source));
  if (photo.detail) box.append(element("p", photo.detail));
  if (photo.preview) {
    const image = element("img");
    image.alt = photo.filename;
    image.src = "photo/" + encodeURIComponent(photo.id);
    image.addEventListener("error", () => image.remove());
    box.append(image);
  }
  return box;
}
function visiblePhotos() {
  const query = el("search").value.trim().toLocaleLowerCase();
  const filter = el("filter").value;
  return (data?.photos || []).filter(p => (!query || p.filename.toLocaleLowerCase().includes(query)) &&
    (filter === "all" || (filter === "matched" ? p.kind === "proposed" : category(p) === filter)));
}
function focusPhoto(photo) {
  selected = photo.id;
  const marker = markerById.get(photo.id);
  if (marker) {
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 15));
    marker.openPopup();
  }
  for (const row of el("photos").children) row.classList.toggle("selected", row.dataset.id === selected);
}
function renderPhotos() {
  markers.clearLayers(); markerById.clear(); bounds = [];
  const fragment = document.createDocumentFragment();
  const photos = visiblePhotos();
  let located = 0;
  for (const photo of photos) {
    const row = element("button", undefined, "photo-row");
    row.type = "button"; row.dataset.id = photo.id;
    row.classList.toggle("selected", selected === photo.id);
    row.append(element("span", photo.filename, "photo-name"));
    row.append(element("span", photo.time || "No matching time", "photo-meta"));
    row.append(element("span", photo.location ? photo.location.map(n => n.toFixed(5)).join(", ") : "No location proposed", "photo-meta"));
    const badgeClass = category(photo) === "errors" ? "failed" : photo.status === "excluded" ? "" : photo.status === "unmatched" ? "unmatched" : photo.kind === "proposed" ? "proposed" : "";
    row.append(element("span", photo.status.replaceAll("_", " "), "badge " + badgeClass));
    row.addEventListener("click", () => focusPhoto(photo));
    fragment.append(row);
    if (photo.location) {
      located++; bounds.push(photo.location);
      const marker = L.circleMarker(photo.location, {radius: 7, color: "#fff", weight: 2,
        fillColor: colors[colorKey(photo)] || colors.existing, fillOpacity: .95});
      marker.bindPopup(() => popup(photo), {maxWidth: 320});
      marker.bindTooltip(() => element("span", photo.filename));
      marker.on("click", () => {
        selected = photo.id;
        for (const item of el("photos").children) item.classList.toggle("selected", item.dataset.id === selected);
        row.scrollIntoView({block: "nearest"});
      });
      marker.addTo(markers); markerById.set(photo.id, marker);
    }
  }
  if (!photos.length) fragment.append(element("p", "No photos match this filter.", "empty-list"));
  el("photos").replaceChildren(fragment);
  el("list-count").textContent = `${photos.length} ${photos.length === 1 ? "photo" : "photos"} · ${located} with a location`;
  el("empty").hidden = located > 0 || routes.getLayers().length > 0;
}
function fitAll() {
  const all = [...bounds];
  if (el("routes").checked) for (const route of data?.routes || []) for (const point of route.points) all.push(point);
  if (all.length) map.fitBounds(L.latLngBounds(all), {padding: [55, 55], maxZoom: 16});
}
function renderRoutes() {
  routes.clearLayers();
  if (el("routes").checked) for (const route of data?.routes || []) {
    if (route.points.length > 1) L.polyline(route.points, {color: "#7473b9", opacity: .6, weight: 3})
      .bindTooltip(() => element("span", route.name)).addTo(routes);
  }
}
async function refresh() {
  try {
    const response = await fetch("data", {cache: "no-store"});
    if (!response.ok) throw new Error("Preview unavailable");
    const next = await response.json();
    el("connection").textContent = "Connected to local app";
    if (next.revision !== revision) {
      const nextSignature = JSON.stringify(next.photos.map(p => [p.id, p.location]));
      if (signature && signature !== nextSignature) firstFit = true;
      signature = nextSignature;
      data = next; revision = next.revision;
      el("album").textContent = next.album || "Photo locations";
      for (const name of ["matched", "skipped", "unmatched", "errors"]) el(name).textContent = (next.statistics[name] || 0).toLocaleString();
      el("stale").hidden = !next.stale;
      renderRoutes(); renderPhotos();
      if (firstFit) { fitAll(); firstFit = false; }
      if (next.focus !== null && focusRevision !== revision) {
        const photo = next.photos.find(p => p.id === next.focus);
        if (photo) focusPhoto(photo);
        focusRevision = revision;
      }
    }
  } catch (_) { el("connection").textContent = "Disconnected · keep the desktop app open"; }
  setTimeout(refresh, 2000);
}
el("fit").addEventListener("click", fitAll);
el("filter").addEventListener("change", () => {renderPhotos(); fitAll();});
el("search").addEventListener("input", renderPhotos);
el("routes").addEventListener("change", () => {renderRoutes(); renderPhotos();});
el("basemap").addEventListener("click", () => {
  const active = map.hasLayer(tiles);
  if (active) map.removeLayer(tiles); else tiles.addTo(map);
  el("basemap").textContent = active ? "Show street map" : "Hide street map";
  if (active) el("tile-warning").hidden = true;
});
tiles.on("tileerror", () => { el("tile-warning").hidden = false; });
refresh();
