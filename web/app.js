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

// ---- map (MapLibre = native pinch/pan/zoom on the phone) ----
const style = {
  version: 8,
  sources: { c: {
    type: "raster",
    tiles: ["a", "b", "c"].map(s => `https://${s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png`),
    tileSize: 256, attribution: "© OpenStreetMap © CARTO",
  } },
  layers: [{ id: "c", type: "raster", source: "c" }],
};
const map = new maplibregl.Map({ container: "map", style, center: [0, 20], zoom: 2, attributionControl: false });
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
map.addControl(new maplibregl.AttributionControl({ compact: true }));

const mkEl = (cls, txt) => { const d = document.createElement("div"); d.className = "mk " + cls; if (txt) d.textContent = txt; return d; };
let liveMk = null, pinMk = null;
const wpMks = [];

function setLive(lng, lat) {
  if (!liveMk) liveMk = new maplibregl.Marker({ element: mkEl("mk-live") }).setLngLat([lng, lat]).addTo(map);
  else liveMk.setLngLat([lng, lat]);
}
function setPin(lng, lat) {
  pending = [lng, lat];
  if (!pinMk) pinMk = new maplibregl.Marker({ element: mkEl("mk-pin") }).setLngLat([lng, lat]).addTo(map);
  else pinMk.setLngLat([lng, lat]);
  if (mode === "teleport") $("setbtn").classList.remove("hidden");
}
function clearPin() {
  if (pinMk) { pinMk.remove(); pinMk = null; }
  pending = null;
  $("setbtn").classList.add("hidden");
}
function drawRoute() {
  wpMks.forEach(m => m.remove()); wpMks.length = 0;
  route.forEach((p, i) => wpMks.push(new maplibregl.Marker({ element: mkEl("mk-wp", String(i + 1)) }).setLngLat(p).addTo(map)));
  const data = { type: "Feature", geometry: { type: "LineString", coordinates: route } };
  if (map.getSource("route")) map.getSource("route").setData(data);
  else {
    map.addSource("route", { type: "geojson", data });
    map.addLayer({ id: "route", type: "line", source: "route",
      paint: { "line-color": "#3b82f6", "line-width": 4 } });
  }
}

map.on("click", (e) => {
  const lng = e.lngLat.lng, lat = e.lngLat.lat;
  if (mode === "teleport") {
    setPin(lng, lat);
    hint(`Pinned ${fmt(lat, lng)}, tap “Set location here”.`);
  } else {
    route.push([lng, lat]); drawRoute();
    hint(`${route.length} waypoint(s). Press Start to walk the route.`);
  }
});

// ---- controls ----
document.querySelectorAll("#seg button").forEach(b => b.onclick = () => {
  mode = b.dataset.mode;
  document.querySelectorAll("#seg button").forEach(x => x.classList.toggle("on", x === b));
  $("route-ctl").classList.toggle("hidden", mode !== "route");
  $("setbtn").classList.toggle("hidden", !(mode === "teleport" && pending));
  hint(mode === "route"
    ? "Tap the map to drop waypoints, then press Start."
    : "Tap the map or search to drop a pin, then “Set location here”.");
});

let connecting = false, wizardOpen = false;
async function doConnect() {
  if (connecting || connected || wizardOpen) return;
  connecting = true;
  setStatus("Connecting…", "amber");
  try {
    const r = await post("/connect");
    if (r.dev_mode_needed) showWizard();
    else if (r.error) { setStatus("Not connected", "red"); hint(r.error); }
    else if (r.name) { connected = true; setStatus(`Connected · ${r.name} · iOS ${r.ios}`, "green"); }
  } catch (e) {
    setStatus("Not connected", "red"); hint("Can’t reach the Mac, is the server still running?");
  } finally {
    connecting = false;
  }
}
$("connect").onclick = doConnect;
$("restore").onclick = async () => { await post("/restore"); clearPin(); hint("Real GPS restored. iOS reacquires in a few seconds."); };

$("setbtn").onclick = async () => {
  if (!pending) return;
  const [lng, lat] = pending;
  hint(`Setting location to ${fmt(lat, lng)}…`);
  const r = await post("/set", { lat, lon: lng });
  if (r.error) { hint("Failed: " + r.error); return; }
  setLive(lng, lat); clearPin();
  hint(`Location set to ${fmt(lat, lng)}`);
};

$("start").onclick = async () => {
  if (route.length < 2) { hint("Drop at least two waypoints first."); return; }
  const r = await post("/route", { points: route.map(p => [p[1], p[0]]), speed: parseFloat($("speed").value) });
  if (r.error) hint("Route failed: " + r.error); else hint("Walking the route…");
};
$("stop").onclick = () => post("/stop");
$("clear").onclick = () => { route = []; drawRoute(); hint("Waypoints cleared."); };
$("speed").oninput = () => { $("speed-val").textContent = parseFloat($("speed").value).toFixed(1) + " m/s"; };

$("go").onclick = doSearch;
$("q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); doSearch(); } });
async function doSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  hint(`Searching “${q}”…`);
  const r = await getj("/geocode?q=" + encodeURIComponent(q));
  if (r.error) { hint(r.error); return; }
  map.flyTo({ center: [r.lon, r.lat], zoom: 15 });
  if (mode === "route") document.querySelector('#seg button[data-mode="teleport"]').click();
  setPin(r.lon, r.lat);
  hint(`Found “${q}”, tap “Set location here”.`);
  $("q").blur();
}

// ---- status / live polling ----
function setStatus(text, color) {
  $("status").textContent = text;
  $("dot").style.color = `var(--${color})`;
}
async function poll() {
  try {
    const s = await getj("/status");
    connected = s.connected;
    if (s.connected) setStatus(`Connected · ${s.name} · iOS ${s.ios}`, "green");
    if (s.live) setLive(s.live.lon, s.live.lat);
  } catch (e) { /* server unreachable; keep last state */ }
}
setInterval(poll, 1200);
poll();
doConnect();                                                              // auto-connect on load
setInterval(() => { if (!connected && !connecting) doConnect(); }, 5000);  // keep trying until the iPhone is reachable

// initial: center on approximate location
getj("/locate").then(r => { if (r.lat) map.flyTo({ center: [r.lon, r.lat], zoom: 12 }); }).catch(() => {});
hint("Connect your iPhone, then tap the map to drop a pin.");

// ---- Developer Mode wizard ----
let wizTimer = null;
function showWizard() {
  if (wizardOpen) return;
  wizardOpen = true;
  $("wizard").classList.remove("hidden");
  post("/devmode/reveal");
  wizTimer = setInterval(async () => {
    const r = await post("/connect");           // succeeds once Developer Mode is on
    if (r && r.name) {
      connected = true;
      $("wiz-status").textContent = "✓  Developer Mode on, connecting…";
      $("wiz-status").style.color = "var(--green)";
      setTimeout(closeWizard, 900);
    }
  }, 2500);
}
function closeWizard() { wizardOpen = false; clearInterval(wizTimer); $("wizard").classList.add("hidden"); }
$("wiz-cancel").onclick = closeWizard;
