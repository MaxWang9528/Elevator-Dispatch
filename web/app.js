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
  stream.addEventListener("profile", (e) => onProfile(JSON.parse(e.data)));
  stream.addEventListener("frame", (e) => onFrame(JSON.parse(e.data)));
  stream.onerror = () => setStatus("Lost the connection. Reconnecting…", true);
}

// ---- incoming ----------------------------------------------------------------------------
function onLayout(next) {
  layout = next;
  renderer.setLayout(layout);
  buildSpeedButtons(layout.speeds);
  fillFloorSelect(layout.floors, layout.lobby);
  showRun(layout.run, layout.rush);
  profile.setCurve(layout.run, layout.profile);
}

// The expected-traffic curve changes without a new run when outings per day change.
function onProfile(points) {
  if (!layout) return;
  layout.profile = points;
  profile.setCurve(layout.run, points);
  if (lastFrame) profile.setNow(lastFrame);
}

function onFrame(frame) {
  if (!layout) return;
  lastFrame = frame;
  renderer.setFrame(frame);

  // Timed runs show the time of day; steady runs show time since the run began.
  $("clock").textContent = formatClock(frame.clock ?? frame.t);
  $("playBtn").textContent = frame.finished ? "Finished" : frame.paused ? "Play" : "Pause";
  $("playBtn").disabled = frame.finished;
  for (const btn of $("speeds").children) {
    btn.setAttribute("aria-checked", String(btn.dataset.speed === String(frame.speed)));
  }
  if (!draggingRate) {
    $("rate").max = Math.max(10, frame.outings);
    $("rate").value = frame.outings;
    $("rateOut").textContent = frame.outings.toFixed(1);
  }
  $("rateHint").hidden = frame.clock !== null;
  $("rateHint").textContent = `About ${formatRate(frame.rate_now)} trips a minute. Steady traffic has no commutes.`;
  profile.setNow(frame);
  const r = frame.residents;
  $("residentsLine").textContent = `Residents: ${r.home.toLocaleString()} at home, `
    + `${r.out.toLocaleString()} out or in the lobby or garage, ${r.inside.toLocaleString()} elsewhere inside, `
    + `${r.traveling.toLocaleString()} on the move.`;

  const s = frame.stats;
  $("sWaiting").textContent = s.waiting.toLocaleString();
  $("sRiding").textContent = s.riding.toLocaleString();
  $("sArrived").textContent = s.arrived.toLocaleString();
  $("sMeanWait").textContent = formatDuration(s.mean_wait);
  $("sMaxWait").textContent = formatDuration(s.max_wait);
  $("sMeanRide").textContent = formatDuration(s.mean_ride);
  $("warmupHint").hidden = !s.warming_up;
  if (s.warming_up) {
    const start = layout.run.start === null ? s.warmup : (layout.run.start + s.warmup) % 86400;
    $("warmupHint").textContent = `Averages start at ${formatClock(start)}, after the warm-up.`;
  }

  if (frame.error) {
    setStatus("The simulation stopped with an error. Fix it, then press Restart.", true);
    $("banner").textContent = frame.error;
    $("banner").hidden = false;
  } else {
    $("banner").hidden = true;
    setStatus(frame.finished ? "Reached the stop time. Restart to run it again, or start another run."
      : frame.paused ? "Paused"
      : frame.speed === "max"
        ? (frame.actual_speed >= 1
          ? `Running as fast as possible, about ${Math.round(frame.actual_speed).toLocaleString()}× real time`
          : "Running as fast as possible")
        : `Running at ${frame.speed}× real time`);
  }
}

// ---- runs: rush hours, chosen hours, or steady traffic -----------------------------------
const RUSH_BUTTONS = { morning: $("runMorning"), evening: $("runEvening") };

function showRun(run, rushes) {
  const timed = run.start !== null;
  const names = { morning: "Morning rush", evening: "Evening rush", custom: "Chosen hours" };
  const seed = run.seed === null || run.seed === undefined ? ""
    : ` ${run.random_seed ? "Random seed" : "Seed"} ${run.seed}.`;
  $("runNote").textContent = (!timed ? "Time since the run began. Steady traffic."
    : `Time of day. ${names[run.mode]}, ${formatHours(run.start)} to ${formatHours(run.stop)}.`) + seed;
  $("clock").setAttribute("aria-label", timed ? "Simulated time of day" : "Simulated time since the run began");
  for (const btn of document.querySelectorAll(".run-mode")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.mode === run.mode));
  }

  for (const rush of rushes) {
    const btn = RUSH_BUTTONS[rush.name];
    btn.disabled = !rush.enabled;
    btn.querySelector("small").textContent = rush.enabled
      ? `${formatHours(rush.run_start)} to ${formatHours(rush.run_stop)}` : "Off in Settings";
  }
}

async function startRun(body) {
  try {
    await post("/api/run", body);
  } catch { /* post() already showed the reason */ }
}

for (const btn of document.querySelectorAll(".run-mode")) {
  btn.addEventListener("click", () => startRun({ mode: btn.dataset.mode }));
}

// The chosen hours are remembered in this browser.
for (const id of ["runStart", "runStop"]) {
  try {
    const saved = localStorage.getItem(id);
    if (/^\d\d:\d\d$/.test(saved || "")) $(id).value = saved;
  } catch { /* storage unavailable: keep the defaults */ }
}
$("runForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const start = $("runStart").value, stop = $("runStop").value;
  if (!start || !stop) {
    setStatus("Enter both a start and a stop time.", true);
    return;
  }
  try {
    localStorage.setItem("runStart", start);
    localStorage.setItem("runStop", stop);
  } catch { /* fine without */ }
  startRun({ mode: "custom", start, stop });
});

// Traffic over a timed run: the curve of people per minute, and where the run is now.
const profile = {
  curve: null, duration: 0, W: 272, H: 54,

  setCurve(run, points) {
    this.curve = points;
    this.duration = run.duration || 0;
    $("profile").hidden = !points;
    if (!points) return;
    $("profileStart").textContent = formatHours(run.start);
    $("profileStop").textContent = formatHours(run.stop);
    const top = Math.max(...points.map((p) => p[1]), 1) * 1.08;
    this.x = (t) => (t / this.duration) * this.W;
    this.y = (f) => 2 + (this.H - 3) * (1 - f / top);
    const busiest = points.reduce((a, b) => (b[1] > a[1] ? b : a));
    this.peakNote = `Busiest about ${formatHours(run.start + busiest[0])}, ${formatRate(busiest[1])} a minute.`;
    const line = points.map(([t, f], i) => `${i ? "L" : "M"}${this.x(t).toFixed(1)},${this.y(f).toFixed(1)}`).join("");
    const svg = $("profileSvg");
    svg.setAttribute("viewBox", `0 0 ${this.W} ${this.H}`);
    svg.innerHTML = `<path class="profile-area" d="${line}L${this.W},${this.H}L0,${this.H}Z"/>`
      + `<path class="profile-line" d="${line}"/>`
      + `<line class="profile-now" id="profileMark" y1="0" y2="${this.H}"/>`
      + `<circle class="profile-dot" id="profileDot" r="3.5"/>`;
  },

  setNow(frame) {
    if (!this.curve) return;
    const x = this.x(Math.min(frame.t, this.duration));
    const y = this.y(frame.rate_now);
    const mark = $("profileMark"), dot = $("profileDot");
    mark.setAttribute("x1", x); mark.setAttribute("x2", x);
    dot.setAttribute("cx", x); dot.setAttribute("cy", y);

    const now = `Expected trips a minute. About ${formatRate(frame.rate_now)} now`;
    const share = (v) => `${Math.round(v * 100)}%`;
    let mix = "no commuting right now";
    if (frame.leaving >= 0.05 && frame.leaving >= frame.returning) {
      mix = `${share(frame.leaving)} of them residents leaving for the day`;
    } else if (frame.returning >= 0.05) {
      mix = `${share(frame.returning)} of them residents coming home`;
    }
    $("profileNow").textContent = `${now}, ${mix}. ${this.peakNote}`;
  },
};

// ---- controls ----------------------------------------------------------------------------
function buildSpeedButtons(speeds) {
  const box = $("speeds");
  box.replaceChildren(...speeds.map((sp) => {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "radio";
    b.dataset.speed = sp;
    b.textContent = sp === "max" ? "Fastest" : `${sp}×`;
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
  $("rateOut").textContent = Number(e.target.value).toFixed(1);
  clearTimeout(rateTimer);
  rateTimer = setTimeout(() => post("/api/control", { outings: Number(e.target.value) }), 120);
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
const metricsView = new MetricsView($("view-metrics"), {
  prefix: "live-",
  title: "Metrics",
  actions: [
    el("a", { class: "btn", href: "/api/metrics?download=1", download: "" }, "Download report (JSON)"),
    el("a", { class: "btn", href: "/api/trips.csv", download: "" }, "Download trips (CSV)"),
  ],
});
const tabs = {
  sim: [$("tab-sim"), $("view-sim")],
  metrics: [$("tab-metrics"), $("view-metrics")],
  import: [$("tab-import"), $("view-import")],
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

/** Time of day without seconds: 6:30 */
function formatHours(t) {
  const m = Math.round(t / 60) % 1440;
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

function formatRate(v) {
  return v >= 10 || Number.isInteger(v) ? String(Math.round(v)) : v.toFixed(1);
}

function formatDuration(sec) {
  if (sec === null || sec === undefined) return "–";
  const s = Math.round(sec);
  if (s < 90) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

connect();
