// Connects the page to the simulation server: live frames in, controls out.
"use strict";

const $ = (id) => document.getElementById(id);
const renderer = new TowerRenderer($("tower"), $("towerScroll"));
let layout = null;
let lastFrame = null;
let draggingRate = false;

// ---- server communication -----------------------------------------------------------------
async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Server answered ${res.status}`);
    return data;
  } catch (err) {
    setStatus(`Couldn't reach the simulation: ${err.message}`, true);
    throw err;
  }
}

function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("layout", (e) => onLayout(JSON.parse(e.data)));
  stream.addEventListener("frame", (e) => onFrame(JSON.parse(e.data)));
  stream.onerror = () => setStatus("Lost the connection. Reconnecting…", true);
}

// ---- incoming ----------------------------------------------------------------------------
function onLayout(next) {
  layout = next;
  renderer.setLayout(layout);
  buildSpeedButtons(layout.speeds);
  fillFloorSelect(layout.floors, layout.lobby);
}

function onFrame(frame) {
  if (!layout) return;
  lastFrame = frame;
  renderer.setFrame(frame);

  $("clock").textContent = formatClock(frame.t);
  $("playBtn").textContent = frame.paused ? "Play" : "Pause";
  for (const btn of $("speeds").children) {
    btn.setAttribute("aria-checked", String(Number(btn.dataset.speed) === frame.speed));
  }
  if (!draggingRate) {
    $("rate").value = frame.rate;
    $("rateOut").textContent = Math.round(frame.rate);
  }

  const s = frame.stats;
  $("sWaiting").textContent = s.waiting.toLocaleString();
  $("sRiding").textContent = s.riding.toLocaleString();
  $("sArrived").textContent = s.arrived.toLocaleString();
  $("sMeanWait").textContent = formatDuration(s.mean_wait);
  $("sMaxWait").textContent = formatDuration(s.max_wait);
  $("sMeanRide").textContent = formatDuration(s.mean_ride);
  $("warmupHint").hidden = !s.warming_up;
  if (s.warming_up) {
    $("warmupHint").textContent = `Averages start at ${formatClock(s.warmup)}, after the warm-up.`;
  }

  if (frame.error) {
    setStatus("The simulation stopped with an error. Fix it, then press Restart.", true);
    $("banner").textContent = frame.error;
    $("banner").hidden = false;
  } else {
    $("banner").hidden = true;
    setStatus(frame.paused ? "Paused" : `Running at ${frame.speed}× real time`);
  }
}

// ---- controls ----------------------------------------------------------------------------
function buildSpeedButtons(speeds) {
  const box = $("speeds");
  box.replaceChildren(...speeds.map((sp) => {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "radio";
    b.dataset.speed = sp;
    b.textContent = `${sp}×`;
    b.setAttribute("aria-checked", "false");
    b.addEventListener("click", () => post("/api/control", { speed: sp }));
    return b;
  }));
}

function fillFloorSelect(floors, lobby) {
  const sel = $("addFloor");
  const keep = sel.value;
  sel.replaceChildren(...[...floors].reverse().map(
    (f) => new Option(f.name ? `${f.label} (${f.name})` : f.label, f.i)));
  sel.value = keep && floors[keep] ? keep : String(lobby);
}

$("playBtn").addEventListener("click", () => {
  if (lastFrame) post("/api/control", { paused: !lastFrame.paused });
});
$("restartBtn").addEventListener("click", () => post("/api/restart", {}));

let rateTimer = null;
$("rate").addEventListener("input", (e) => {
  draggingRate = true;
  $("rateOut").textContent = e.target.value;
  clearTimeout(rateTimer);
  rateTimer = setTimeout(() => post("/api/control", { rate: Number(e.target.value) }), 120);
});
$("rate").addEventListener("change", () => { draggingRate = false; });

$("addForm").addEventListener("submit", (e) => {
  e.preventDefault();
  post("/api/spawn", { origin: Number($("addFloor").value) });
});

$("tower").addEventListener("click", (e) => {
  const floor = renderer.floorAt(e);
  if (floor !== null) post("/api/spawn", { origin: floor });
});

// ---- tabs (the URL hash keeps the current tab across reloads) ------------------------------
const metricsView = new MetricsView();
const tabs = {
  sim: [$("tab-sim"), $("view-sim")],
  metrics: [$("tab-metrics"), $("view-metrics")],
  settings: [$("tab-settings"), $("view-settings")],
};

function showTab(name, focus = false) {
  if (!tabs[name]) name = "sim";
  for (const [key, [tab, view]] of Object.entries(tabs)) {
    const on = key === name;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    view.hidden = !on;
    if (on && focus) tab.focus();
  }
  metricsView.setActive(name === "metrics");
  const hash = name === "sim" ? "" : `#${name}`;
  if (location.hash !== hash) history.replaceState(null, "", hash || location.pathname);
}

for (const [key, [tab]] of Object.entries(tabs)) {
  tab.addEventListener("click", () => showTab(key));
  tab.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const names = Object.keys(tabs);
    const next = names[(names.indexOf(key) + (e.key === "ArrowRight" ? 1 : -1) + names.length) % names.length];
    showTab(next, true);
  });
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
showTab(location.hash.slice(1));

const settingsView = new SettingsView();
settingsView.load().catch((err) => {
  settingsView.setMessage(`Couldn't load settings: ${err.message}. Reload the page to try again.`, "error");
});

// ---- formatting --------------------------------------------------------------------------
function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text;
  el.classList.toggle("is-error", isError);
}

function formatClock(t) {
  const s = Math.floor(t);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function formatDuration(sec) {
  if (sec === null || sec === undefined) return "–";
  const s = Math.round(sec);
  if (s < 90) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

connect();
