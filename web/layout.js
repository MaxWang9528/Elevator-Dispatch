// The Simulation tab's divider: drag (or arrow keys) to resize the side panel, double-click to
// reset, and a button to collapse it. Width and collapsed state are remembered in this browser.
(() => {
  const app = document.getElementById("view-sim");
  const splitter = document.getElementById("splitter");
  const toggle = document.getElementById("panelToggle");
  const MIN = 260, MIN_BUILDING = 240, KEY_WIDTH = "elevator.panelWidth", KEY_COLLAPSED = "elevator.panelCollapsed";
  const load = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  const save = (k, v) => { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* not kept */ } };

  const clamp = (w) => Math.round(Math.max(MIN, Math.min(w, app.clientWidth - MIN_BUILDING - splitter.offsetWidth)));
  const setWidth = (w, keep = true) => {
    if (w === null) {
      app.style.removeProperty("--panel-width");
      if (keep) save(KEY_WIDTH, null);
      return;
    }
    const px = clamp(w);
    app.style.setProperty("--panel-width", `${px}px`);
    splitter.setAttribute("aria-valuenow", String(px));
    if (keep) save(KEY_WIDTH, String(px));
  };
  const panelWidth = () => document.getElementById("sidePanel").getBoundingClientRect().width;
  const setCollapsed = (collapsed) => {
    app.classList.toggle("panel-collapsed", collapsed);
    toggle.textContent = collapsed ? "‹" : "›";
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Expand the side panel" : "Collapse the side panel");
    toggle.title = collapsed ? "Expand panel" : "Collapse panel";
    save(KEY_COLLAPSED, collapsed ? "1" : null);
  };

  const saved = Number(load(KEY_WIDTH));
  if (saved) setWidth(saved, false);
  setCollapsed(load(KEY_COLLAPSED) === "1");

  toggle.addEventListener("click", (e) => { e.stopPropagation(); setCollapsed(!app.classList.contains("panel-collapsed")); });
  toggle.addEventListener("pointerdown", (e) => e.stopPropagation());
  toggle.addEventListener("dblclick", (e) => e.stopPropagation());

  splitter.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    splitter.setPointerCapture(e.pointerId);
    app.classList.add("resizing");
    const right = app.getBoundingClientRect().right;
    const move = (ev) => {
      const w = right - ev.clientX - splitter.offsetWidth / 2;
      if (app.classList.contains("panel-collapsed")) {
        if (w < MIN) return;
        setCollapsed(false);
      }
      setWidth(w);
    };
    const up = () => {
      app.classList.remove("resizing");
      splitter.removeEventListener("pointermove", move);
      splitter.removeEventListener("pointerup", up);
      splitter.removeEventListener("pointercancel", up);
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", up);
    splitter.addEventListener("pointercancel", up);
  });
  splitter.addEventListener("dblclick", () => { setCollapsed(false); setWidth(null); });
  splitter.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 80 : 20;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      if (app.classList.contains("panel-collapsed")) setCollapsed(false);
      setWidth(panelWidth() + (e.key === "ArrowLeft" ? step : -step));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setCollapsed(!app.classList.contains("panel-collapsed"));
    }
  });
  // Keep a saved width inside the window when it gets smaller.
  window.addEventListener("resize", () => {
    if (!app.style.getPropertyValue("--panel-width") || !app.clientWidth || app.classList.contains("panel-collapsed")) return;
    setWidth(panelWidth(), false);
  });
})();
