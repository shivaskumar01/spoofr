"use strict";
// Token from the URL the user opened (?t=...). API calls include it.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const api = (p, opts) => fetch(p + (p.includes("?") ? "&" : "?") + "t=" + TOKEN, opts);
const getj = (p) => api(p).then(r => r.json());
const post = (p, body) => api(p, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: body ? JSON.stringify(body) : null,
}).then(r => r.json());

const $ = (id) => document.getElementById(id);
const hint = (t) => { $("hint").textContent = t; };
const fmt = (lat, lng) => `${lat.toFixed(5)}, ${lng.toFixed(5)}`;

let mode = "teleport";
let pending = null;   // staged [lng, lat]
let route = [];       // waypoints [[lng, lat], ...]
let connected = false;
let live = null;      // [lng, lat] where the phone is, per the Mac
let routeActive = false;

// ---- map (MapLibre = native pinch/pan/zoom on the phone) ----
// Esri's street map, recoloured to match the Mac app's navy map. CARTO's dark_all
// now stamps "API KEY REQUIRED" across every tile while still answering 200, so
// it cannot be used without a key; Esri needs none. Saturation -1 plus swapped
// brightness-min/max inverts it to light-on-dark, and drawing it partly
// transparent over a navy background tints it and softens the contrast, which
// is as close as raster paint gets to the desktop's colour ramp (qtui/tilemap.py).
const style = {
  version: 8,
  sources: { c: {
    type: "raster",
    tiles: ["https://services.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"],
    tileSize: 256, attribution: "© Esri",
  } },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#0b1120" } },
    { id: "c", type: "raster", source: "c",
      paint: {
        "raster-saturation": -1,
        "raster-brightness-min": 1,
        "raster-brightness-max": 0,
        "raster-opacity": 0.55,
        "raster-fade-duration": 120,
      } },
  ],
};
const map = new maplibregl.Map({ container: "map", style, center: [0, 20], zoom: 2, attributionControl: false });
map.addControl(new maplibregl.AttributionControl({ compact: true }));

const mkEl = (cls, txt) => { const d = document.createElement("div"); d.className = "mk " + cls; if (txt) d.textContent = txt; return d; };
let liveMk = null, pinMk = null;
const wpMks = [];

function setLive(lng, lat) {
  live = [lng, lat];
  if (!liveMk) liveMk = new maplibregl.Marker({ element: mkEl("mk-live") }).setLngLat(live).addTo(map);
  else liveMk.setLngLat(live);
}
function setPin(lng, lat) {
  pending = [lng, lat];
  if (!pinMk) pinMk = new maplibregl.Marker({ element: mkEl("mk-pin") }).setLngLat(pending).addTo(map);
  else pinMk.setLngLat(pending);
  syncChrome();
}
function clearPin() {
  if (pinMk) { pinMk.remove(); pinMk = null; }
  pending = null;
  syncChrome();
}

// ---- route: one tap is a destination, the phone is the start ----
const R = 6371000, rad = (d) => d * Math.PI / 180;
function meters(a, b) {   // [lng, lat]
  const h = Math.sin(rad(b[1] - a[1]) / 2) ** 2 +
    Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(rad(b[0] - a[0]) / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
function plan() {         // what Start would walk, same rule as the Mac and server
  if (!route.length) return [];
  if (live) {
    const gap = meters(live, route[0]);
    if (gap >= 1 && gap <= 100000) return [live, ...route];
  }
  return route.slice();
}
const speed = () => parseFloat($("speed").value);
const mph = (mps) => { const v = mps * 2.2369363; return (v < 10 ? v.toFixed(1) : v.toFixed(0)) + " mph"; };
function estimate() {
  if (routeActive) return;
  const p = plan();
  if (p.length < 2) { $("eta").textContent = "Tap the map to set a destination"; return; }
  let m = 0; for (let i = 1; i < p.length; i++) m += meters(p[i - 1], p[i]);
  const mi = m / 1609.344, min = Math.round(m / speed() / 60);
  const dist = mi < 0.1 ? `${Math.round(m * 3.28084)} ft` : `${mi.toFixed(mi < 1 ? 2 : 1)} mi`;
  const t = min < 1 ? "<1 min" : min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min` : `${min} min`;
  $("eta").textContent = `${dist} · ${t}`;
}
function drawRoute() {
  wpMks.forEach(m => m.remove()); wpMks.length = 0;
  route.forEach((p, i) => wpMks.push(new maplibregl.Marker({
    element: mkEl("mk-wp" + (i === route.length - 1 ? " dest" : ""), String(i + 1)) }).setLngLat(p).addTo(map)));
  if (ensureRouteLayer())
    map.getSource("route").setData({ type: "Feature", geometry: { type: "LineString", coordinates: plan() } });
  estimate();
}

map.on("click", (e) => {
  const lng = e.lngLat.lng, lat = e.lngLat.lat;
  $("q").blur();
  if (mode === "teleport") {
    setPin(lng, lat);
    hint(`Pinned ${fmt(lat, lng)}. Tap “Set location here”.`);
  } else if (routeActive) {
    hint("A route is running. Stop it to change the route.");
  } else {
    route.push([lng, lat]); drawRoute();
    hint(route.length === 1 ? "Destination set. Press Start, or tap again to add a stop."
                            : `${route.length} stops, green is the destination.`);
  }
});

// ---- controls ----
function syncChrome() {
  $("route-ctl").classList.toggle("hidden", mode !== "route" && !routeActive);
  $("setbtn").classList.toggle("hidden", !(mode === "teleport" && pending));
  $("start").textContent = routeActive ? "Stop" : "Start";
  $("start").className = "btn " + (routeActive ? "danger" : "primary");
}
function setMode(m) {
  mode = m;
  document.querySelectorAll("#seg button").forEach(x => x.classList.toggle("on", x.dataset.mode === m));
  syncChrome();
  hint(mode === "route"
    ? "Tap where you want to end up. Your iPhone sets off from where it is."
    : "Tap the map or search to drop a pin, then “Set location here”.");
}
document.querySelectorAll("#seg button").forEach(b => b.onclick = () => { if (b.dataset.mode !== mode) setMode(b.dataset.mode); });
document.querySelectorAll("#pace button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#pace button").forEach(x => x.classList.toggle("on", x === b));
  $("speed").value = b.dataset.mps; $("speed").oninput();
});

let connecting = false, wizardOpen = false;
const RETRY_MIN = 5000, RETRY_MAX = 30000;
let retryDelay = RETRY_MIN, retryTimer = null;

const connectedText = (s) =>
  `${s.name} · iOS ${s.ios}` + (s.link ? ` · ${s.link}` : "");

async function doConnect(fromWizard) {
  if (connecting || connected) return;
  if (wizardOpen && !fromWizard) return;
  connecting = true;
  setStatus("Connecting…", "amber");
  try {
    const r = await post("/connect");
    if (r.connecting) return;                 // the Mac is already working on one
    if (r.dev_mode_needed) showWizard();
    else if (r.error) { setStatus("Not connected", "red"); hint(r.error); }
    else if (r.name) {
      connected = true;
      retryDelay = RETRY_MIN;
      setStatus(connectedText(r), "green");
      if (wizardOpen) wizardSucceeded();
    }
  } catch (e) {
    setStatus("Not connected", "red"); hint("Can’t reach the Mac. Is Spoofr still open there?");
  } finally {
    connecting = false;
  }
}

// Keep trying until the iPhone is reachable, but back off. Every attempt is a
// full connect on the Mac, and a flat 5s retry (plus the wizard's own 2.5s one)
// just stacked requests behind the connect that was already running.
function scheduleRetry() {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(async () => {
    if (!connected && !connecting) {
      await doConnect();
      if (!connected) retryDelay = Math.min(retryDelay * 1.6, RETRY_MAX);
    }
    scheduleRetry();
  }, retryDelay);
}

$("connect").onclick = () => doConnect();
$("restore").onclick = async () => {
  const r = await post("/restore");
  clearPin();
  hint(r && r.error ? "Restore failed: " + r.error
                    : "Real GPS restored. iOS reacquires in a few seconds.");
};

$("setbtn").onclick = async () => {
  if (!pending) return;
  const [lng, lat] = pending;
  hint(`Setting location to ${fmt(lat, lng)}…`);
  const r = await post("/set", { lat, lon: lng });
  if (r.error) { hint("Failed: " + r.error); return; }
  setLive(lng, lat); clearPin(); drawRoute();
  hint(`Location set to ${fmt(lat, lng)}`);
};

$("start").onclick = async () => {
  if (routeActive) { await post("/stop"); routeActive = false; syncChrome(); estimate(); hint("Route stopped."); return; }
  const p = plan();
  if (p.length < 2) {
    hint(route.length ? "Set your location first so the route has somewhere to start, or add a second stop."
                      : "Tap the map to set a destination first.");
    return;
  }
  const r = await post("/route", { points: p.map(q => [q[1], q[0]]), speed: speed() });
  if (r.error) { hint("Route failed: " + r.error); return; }
  routeActive = true; syncChrome();
  hint("On the way…");
};
$("clear").onclick = async () => {
  if (routeActive) { await post("/stop"); routeActive = false; }
  route = []; drawRoute(); syncChrome(); hint("Waypoints cleared.");
};
$("speed").oninput = () => { $("speed-val").textContent = mph(speed()); estimate(); };

$("q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); doSearch(); } });
async function doSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  $("q").blur();
  const m = q.match(/^\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/);
  let lat, lon;
  if (m && Math.abs(+m[1]) <= 90 && Math.abs(+m[2]) <= 180) { lat = +m[1]; lon = +m[2]; }
  else {
    hint(`Searching “${q}”…`);
    const r = await getj("/geocode?q=" + encodeURIComponent(q));
    if (r.error) { hint(r.error); return; }
    lat = r.lat; lon = r.lon;
  }
  map.flyTo({ center: [lon, lat], zoom: Math.max(15, map.getZoom()) });
  if (mode === "route") setMode("teleport");
  setPin(lon, lat);
  hint(`Found “${q}”. Tap “Set location here”.`);
}

// ---- status / live polling ----
function setStatus(text, color) {
  $("status").textContent = text;
  $("dot").style.background = `var(--${color})`;
  // it auto-connects, so the button only earns its space while it can help
  $("connect").classList.toggle("hidden", color === "green");
}
async function poll() {
  try {
    const s = await getj("/status");
    connected = s.connected;
    if (s.connected) { setStatus(connectedText(s), "green"); retryDelay = RETRY_MIN; }
    else if (s.reconnecting) setStatus("Reconnecting…", "amber");
    else if (s.connecting) setStatus("Connecting…", "amber");
    else if (s.lost) setStatus("Lost the iPhone", "red");
    if (s.live) setLive(s.live.lon, s.live.lat);
    if (routeActive !== !!s.route_active) {
      routeActive = !!s.route_active;
      syncChrome();
      if (!routeActive) { hint("Route finished."); drawRoute(); }
    }
  } catch (e) { /* server unreachable; keep last state */ }
}
setInterval(poll, 1200);
poll();
doConnect();          // auto-connect on load
scheduleRetry();      // and keep trying, with a growing gap

// initial: center on approximate location
getj("/locate").then(r => { if (r.lat) map.flyTo({ center: [r.lon, r.lat], zoom: 13 }); }).catch(() => {});
// The route line's layer is made as soon as the style is ready. Not on "load":
// that waits for every tile in view, so on a slow phone connection the line
// didn't exist yet and taps drew waypoints with nothing joining them.
function ensureRouteLayer() {
  if (map.getSource("route")) return true;
  try {
    map.addSource("route", { type: "geojson",
      data: { type: "Feature", geometry: { type: "LineString", coordinates: [] } } });
    map.addLayer({ id: "route", type: "line", source: "route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#3b82f6", "line-width": 5 } });
    return true;
  } catch (e) {
    return false;             // style not parsed yet; style.load below retries
  }
}
map.on("style.load", drawRoute);
hint("Tap the map to drop a pin, or search.");

// ---- Developer Mode wizard ----
let wizTimer = null;
function showWizard() {
  if (wizardOpen) return;
  wizardOpen = true;
  $("wizard").classList.remove("hidden");
  post("/devmode/reveal");
  // through the same guarded connect, so the wizard's poll can't stack full
  // connects on top of the retry loop's
  wizTimer = setInterval(() => doConnect(true), 2500);
}

function wizardSucceeded() {
  $("wiz-status").textContent = "✓  Developer Mode on, connecting…";
  $("wiz-status").style.color = "var(--green)";
  setTimeout(closeWizard, 900);
}
function closeWizard() { wizardOpen = false; clearInterval(wizTimer); $("wizard").classList.add("hidden"); }
$("wiz-cancel").onclick = closeWizard;
