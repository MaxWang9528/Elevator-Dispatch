// The Metrics tab: polls /api/metrics while visible and draws tiles, tables and four charts.
// Charts are plain SVG. Each has a hover/keyboard readout and a "Show as table" twin.
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
}

// ---- formatting ----------------------------------------------------------------------------
const fmt = {
  none: "–",
  int: (v) => (v === null || v === undefined ? fmt.none : Math.round(v).toLocaleString()),
  dec: (v, digits = 1) => (v === null || v === undefined ? fmt.none
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })),
  pct: (v, digits = 0) => (v === null || v === undefined ? fmt.none : `${(v * 100).toFixed(digits)}%`),
  signed: (v) => (v > 0 ? `+${v.toLocaleString()}` : v.toLocaleString()),
  /** Compact duration for tiles and tooltips: 45s, 2m 05s */
  secs(v) {
    if (v === null || v === undefined) return fmt.none;
    const s = Math.round(v);
    return s < 90 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  },
  /** Clock without seconds: 1:05 */
  clock(t) {
    const m = Math.round(t / 60);
    return `${Math.floor(m / 60) % 24}:${String(m % 60).padStart(2, "0")}`;
  },
  clockSecs(t) {
    const s = Math.round(t);
    return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  },
  span(seconds) {
    const m = seconds / 60;
    if (m < 1) return `${Math.round(seconds)} s`;
    if (m < 90) return `${Math.round(m)} min`;
    return `${(m / 60).toFixed(1)} h`;
  },
};

// ---- axis ticks ------------------------------------------------------------------------------
const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];

function axisTicks(max, kind) {
  let step;
  if (kind === "time") {
    step = TIME_STEPS.find((s) => max / s <= 4) || TIME_STEPS.at(-1);
  } else {
    const pow = 10 ** Math.floor(Math.log10(Math.max(max, 1) / 4 || 1));
    step = [1, 2, 5, 10].map((m) => m * pow).find((s) => max / s <= 4);
    if (kind === "count") step = Math.max(1, step);
  }
  const top = Math.max(step, Math.ceil(max / step - 1e-9) * step);
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  const label = kind === "time"
    ? (v) => (v === 0 ? "0" : step >= 60 ? `${v / 60}m` : `${v}s`)
    : (v) => v.toLocaleString();
  return { ticks, top, label };
}

// ---- chart -----------------------------------------------------------------------------------
class Chart {
  /**
   * opts: { type: "line" | "bar", label, series: [{key, name, color}], yKind: "time" | "count",
   *         area, endLabels, padRight (px, to line up with a chart that has end labels), xTick(row), tipTitle(row), tipValue(v), tipExtra(row) -> [[name, value]],
   *         columns: [{title, get(row), num}], empty }
   */
  constructor(figure, opts) {
    this.opts = opts;
    this.rows = [];
    this.hover = null;
    this.height = 230;

    if (opts.series.length > 1) {
      figure.append(el("ul", { class: "chart-legend" }, ...opts.series.map((s) => {
        const key = el("span", { class: "chart-key" });
        key.style.background = `var(${s.color})`;
        return el("li", {}, key, s.name);
      })));
    }
    this.svg = svgEl("svg", { class: "chart-svg", role: "img", tabindex: 0, "aria-label": opts.label });
    this.tip = el("div", { class: "chart-tip", hidden: "" });
    this.emptyEl = el("p", { class: "chart-empty", hidden: "" }, opts.empty);
    this.plot = el("div", { class: "chart-plot" }, this.svg, this.tip, this.emptyEl);
    this.tbody = el("tbody");
    const head = el("tr", {}, ...opts.columns.map((c) => el("th", { scope: "col", class: c.num ? "num" : null }, c.title)));
    this.details = el("details", { class: "chart-table" },
      el("summary", {}, "Show as table"),
      el("div", { class: "table-scroll" }, el("table", { class: "data-table" }, el("thead", {}, head), this.tbody)));
    figure.append(this.plot, this.details);

    this.svg.addEventListener("pointermove", (e) => this.setHover(this.indexAt(e)));
    this.svg.addEventListener("pointerleave", () => this.setHover(null));
    this.svg.addEventListener("blur", () => this.setHover(null));
    this.svg.addEventListener("keydown", (e) => this.onKey(e));
    this.details.addEventListener("toggle", () => this.renderTable());
    new ResizeObserver(() => this.render()).observe(this.plot);
  }

  setData(rows) {
    this.rows = rows;
    if (this.hover !== null && this.hover >= rows.length) this.hover = null;
    this.render();
    this.renderTable();
  }

  hasValue(row) {
    return this.opts.series.some((s) => row[s.key] !== null && row[s.key] !== undefined);
  }

  // -- geometry --
  layout() {
    const width = this.plot.clientWidth;
    const m = { l: 46, r: this.opts.endLabels ? 112 : this.opts.padRight ?? 14, t: 12, b: 28 };
    const plotW = Math.max(40, width - m.l - m.r);
    const plotH = this.height - m.t - m.b;
    const n = this.rows.length;
    const max = Math.max(0, ...this.rows.flatMap((r) => this.opts.series.map((s) => r[s.key] ?? 0)));
    const axis = axisTicks(max || 1, this.opts.yKind);
    const y = (v) => m.t + plotH * (1 - v / axis.top);
    let x, slot;
    if (this.opts.type === "bar") {
      slot = plotW / Math.max(n, 1);
      x = (i) => m.l + slot * (i + 0.5);
    } else {
      const t0 = n ? this.rows[0].t0 : 0;
      const t1 = n ? this.rows[n - 1].t1 : 1;
      const tx = (t) => m.l + plotW * ((t - t0) / (t1 - t0));
      x = (i) => tx((this.rows[i].t0 + this.rows[i].t1) / 2);
      x.time = tx;
      slot = plotW / Math.max(n, 1);
    }
    return { width, m, plotW, plotH, axis, x, y, slot, n };
  }

  indexAt(evt) {
    if (!this.g || !this.g.n) return null;
    const px = evt.clientX - this.svg.getBoundingClientRect().left;
    const { m, plotW, slot, n, x } = this.g;
    if (px < m.l - 8 || px > m.l + plotW + 8) return null;
    if (this.opts.type === "line") {
      // The nearest point, so the crosshair lands on the point under the pointer.
      let best = 0;
      for (let i = 1; i < n; i++) if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i;
      return best;
    }
    return Math.max(0, Math.min(n - 1, Math.floor((px - m.l) / slot)));
  }

  onKey(e) {
    const n = this.rows.length;
    if (!n) return;
    if (e.key === "Escape") return this.setHover(null);
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const from = this.hover ?? (e.key === "ArrowLeft" ? n : -1);
    this.setHover(Math.max(0, Math.min(n - 1, from + (e.key === "ArrowRight" ? 1 : -1))));
  }

  setHover(index) {
    if (index === this.hover) return;
    this.hover = index;
    this.render();
  }

  // -- drawing --
  render() {
    const g = (this.g = this.layout());
    const { m, plotW, plotH, axis, x, y, n } = g;
    const { opts, rows } = this;
    const anyData = rows.some((r) => this.hasValue(r));
    this.emptyEl.hidden = anyData;
    this.svg.setAttribute("viewBox", `0 0 ${g.width} ${this.height}`);
    this.svg.setAttribute("width", g.width);
    this.svg.setAttribute("height", this.height);
    const parts = [];

    // gridlines and y labels
    for (const v of axis.ticks) {
      const gy = Math.round(y(v)) + 0.5;
      parts.push(svgEl("line", { x1: m.l, x2: m.l + plotW, y1: gy, y2: gy, class: v === 0 ? "chart-axis" : "chart-grid" }));
      parts.push(svgEl("text", { x: m.l - 8, y: gy + 4, class: "chart-tick", "text-anchor": "end" }, axis.label(v)));
    }

    // x labels, thinned so they never collide. `xTickSteps` (in rows) keeps labels on round
    // numbers; `minorTicks` marks every row on the axis, when they are far enough apart.
    if (n) {
      let every = Math.max(1, Math.ceil((opts.type === "bar" ? 22 : 52) / g.slot));
      if (opts.xTickSteps) every = opts.xTickSteps.find((s) => s * g.slot >= 36) ?? opts.xTickSteps.at(-1);
      const minorEvery = g.slot >= 3 ? 1 : 5;
      const base = Math.round(y(0)) + 0.5;
      rows.forEach((row, i) => {
        const tx = opts.type === "bar" ? x(i) : x.time(row.t0);
        if (opts.minorTicks && i % minorEvery === 0) {
          const px = Math.round(tx) + 0.5;
          parts.push(svgEl("line", { x1: px, x2: px, y1: base, y2: base + (i % every ? 3 : 6), class: "chart-axis" }));
        }
        if (i % every) return;
        parts.push(svgEl("text", { x: tx, y: this.height - 8, class: "chart-tick", "text-anchor": "middle" }, opts.xTick(row)));
      });
    }

    if (anyData && opts.type === "bar") this.drawBars(parts, g);
    if (anyData && opts.type === "line") this.drawLines(parts, g);

    // hover readout: crosshair for lines; bars lift themselves in drawBars
    if (this.hover !== null && rows[this.hover] && opts.type === "line") {
      const hx = Math.round(x(this.hover)) + 0.5;
      parts.push(svgEl("line", { x1: hx, x2: hx, y1: m.t, y2: m.t + plotH, class: "chart-cross" }));
      for (const s of opts.series) {
        const v = rows[this.hover][s.key];
        if (v === null || v === undefined) continue;
        parts.push(svgEl("circle", { cx: x(this.hover), cy: y(v), r: 4, class: "chart-dot", style: `fill: var(${s.color})` }));
      }
    }
    this.svg.replaceChildren(...parts);
    this.renderTip(g);
  }

  drawBars(parts, g) {
    const { x, y, slot } = g;
    const s = this.opts.series[0];
    const w = Math.max(1, Math.min(24, slot - 2));
    const base = y(0);
    this.rows.forEach((row, i) => {
      const v = row[s.key];
      if (v === null || v === undefined) return;
      const top = Math.min(y(v), base - 1);
      const r = Math.min(4, w / 2, base - top);
      const left = x(i) - w / 2;
      // rounded at the data end, square at the baseline
      const d = `M${left},${base} V${top + r} Q${left},${top} ${left + r},${top} H${left + w - r} `
        + `Q${left + w},${top} ${left + w},${top + r} V${base} Z`;
      parts.push(svgEl("path", { d, class: i === this.hover ? "chart-bar is-hover" : "chart-bar", style: `fill: var(${s.color})` }));
    });
  }

  drawLines(parts, g) {
    const { x, y, m, plotW } = g;
    const ends = [];
    for (const s of this.opts.series) {
      const runs = [];     // a missing value breaks the line
      let run = [];
      this.rows.forEach((row, i) => {
        const v = row[s.key];
        if (v === null || v === undefined) {
          if (run.length) runs.push(run);
          run = [];
        } else {
          run.push([x(i), y(v)]);
        }
      });
      if (run.length) runs.push(run);
      for (const pts of runs) {
        const d = pts.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
        if (this.opts.area && pts.length > 1) {
          const area = `${d} L${pts.at(-1)[0].toFixed(1)},${y(0)} L${pts[0][0].toFixed(1)},${y(0)} Z`;
          parts.push(svgEl("path", { d: area, class: "chart-area", style: `fill: var(${s.color})` }));
        }
        if (pts.length > 1) parts.push(svgEl("path", { d, class: "chart-line", style: `stroke: var(${s.color})` }));
      }
      const last = runs.at(-1)?.at(-1);
      if (last) {
        parts.push(svgEl("circle", { cx: last[0], cy: last[1], r: 4, class: "chart-dot", style: `fill: var(${s.color})` }));
        ends.push({ name: s.name, x: last[0], y: last[1] });
      }
    }
    // End labels only when they have room; otherwise the legend carries identity.
    if (this.opts.endLabels && ends.length) {
      const apart = ends.every((a, i) => ends.every((b, j) => i === j || Math.abs(a.y - b.y) >= 15));
      const fits = ends.every((e) => e.x + 10 < m.l + plotW + 14);
      if (apart && fits) {
        for (const e of ends) {
          parts.push(svgEl("text", { x: e.x + 10, y: e.y + 4, class: "chart-label" }, e.name));
        }
      }
    }
  }

  renderTip(g) {
    const row = this.hover === null ? null : this.rows[this.hover];
    if (!row) {
      this.tip.hidden = true;
      return;
    }
    const { opts } = this;
    const lines = [el("p", { class: "tip-title" }, opts.tipTitle(row))];
    for (const s of opts.series) {
      const key = el("span", { class: "chart-key" });
      key.style.background = `var(${s.color})`;
      lines.push(el("p", { class: "tip-row" }, key, el("strong", {}, opts.tipValue(row[s.key])), s.name));
    }
    for (const [name, value] of opts.tipExtra ? opts.tipExtra(row) : []) {
      lines.push(el("p", { class: "tip-row" }, el("span", { class: "chart-key is-blank" }), el("strong", {}, value), name));
    }
    this.tip.replaceChildren(...lines);
    this.tip.hidden = false;
    const hx = g.x(this.hover);
    const w = this.tip.offsetWidth;
    const left = hx + 14 + w > g.width ? hx - 14 - w : hx + 14;
    this.tip.style.left = `${Math.max(0, left)}px`;
    this.tip.style.top = `${g.m.t}px`;
  }

  renderTable() {
    if (!this.details.open) return;
    this.tbody.replaceChildren(...this.rows.map((row) => el("tr", {},
      ...this.opts.columns.map((c) => el("td", { class: c.num ? "num" : null }, String(c.get(row)))))));
  }
}

// ---- the view ------------------------------------------------------------------------------
/**
 * Builds the Metrics layout (the #metricsLayout template) into `root` and fills it from a report.
 * The Metrics tab shows the live run; the Import tab shows a saved one. `prefix` keeps the ids of
 * each copy apart; `title` and `actions` (elements) fill the head.
 */
class MetricsView {
  constructor(root, { prefix, title, actions = [] }) {
    this.timer = null;
    this.active = false;
    this.prefix = prefix;
    // Seconds added to simulated time before it is shown: the run's starting time of day in a
    // timed run (so charts read 7:30, not 1:00 into the run), 0 in a steady run.
    this.dayStart = 0;

    const layout = document.getElementById("metricsLayout").content.cloneNode(true);
    for (const node of layout.querySelectorAll("[id]")) node.id = prefix + node.id;
    for (const node of layout.querySelectorAll("[aria-labelledby]")) {
      node.setAttribute("aria-labelledby", prefix + node.getAttribute("aria-labelledby"));
    }
    root.replaceChildren(layout);
    this.root = root;
    this.$("mTitle").textContent = title;
    this.$("mActions").replaceChildren(...actions);
    this.scope = this.$("mScope");
    this.notice = this.$("mNotice");
    this.source = this.$("mSource");
    this.body = this.$("mBody");

    const secs = (v) => fmt.dec(v);
    const bucketTitle = (b) => `${this.clock(b.t0)} to ${this.clock(b.t1)}`;
    const timeColumns = [
      { title: "Interval starts", get: (b) => this.clock(b.t0) },
      { title: "People arriving", get: (b) => fmt.int(b.arrived), num: true },
      { title: "Delivered", get: (b) => fmt.int(b.delivered), num: true },
      { title: "Average wait (s)", get: (b) => secs(b.wait_mean), num: true },
      { title: "95th percentile wait (s)", get: (b) => secs(b.wait_p95), num: true },
      { title: "Waiting, average", get: (b) => fmt.dec(b.avg_waiting), num: true },
      { title: "Waiting, peak", get: (b) => fmt.int(b.peak_waiting), num: true },
    ];
    const ordinal = (q) => {
      const tens = q % 100, ones = q % 10;
      const suffix = tens >= 11 && tens <= 13 ? "th" : ones === 1 ? "st" : ones === 2 ? "nd" : ones === 3 ? "rd" : "th";
      return `${q}${suffix}`;
    };
    this.tripChart = new Chart(this.$("mTripChart"), {
      type: "line", yKind: "time", minorTicks: true, xTickSteps: [10, 20, 25, 50, 100],
      label: "Line chart of whole-trip time at each percentile, from fastest to slowest",
      series: [{ key: "trip", name: "whole trip", color: "--series-1" }],
      xTick: (row) => String(row.q),
      tipTitle: (row) => `${ordinal(row.q)} percentile${row.q === 0 ? ", fastest" : row.q === 100 ? ", slowest" : ""}`,
      tipValue: fmt.secs,
      tipExtra: (row) => (row.q < 100 ? [["of people took longer", `${100 - row.q}%`]] : []),
      columns: [
        { title: "Percentile", get: (row) => ordinal(row.q) },
        { title: "Whole trip (s)", get: (row) => fmt.dec(row.trip), num: true },
      ],
      empty: "No one has finished a trip yet.",
    });
    this.waitChart = new Chart(this.$("mWaitChart"), {
      type: "line", yKind: "time", endLabels: true,
      label: "Line chart of average and 95th percentile wait, by when people arrived",
      series: [
        { key: "wait_mean", name: "Average", color: "--series-1" },
        { key: "wait_p95", name: "95th percentile", color: "--series-2" },
      ],
      xTick: (b) => this.clock(b.t0),
      tipTitle: bucketTitle,
      tipValue: fmt.secs,
      tipExtra: (b) => [["people arriving", fmt.int(b.arrived)]],
      columns: timeColumns,
      empty: "The first interval hasn't settled yet.",
    });
    this.queueChart = new Chart(this.$("mQueueChart"), {
      type: "line", yKind: "count", area: true, padRight: 112,
      label: "Line chart of the average number of people waiting over time",
      series: [{ key: "avg_waiting", name: "waiting on average", color: "--series-1" }],
      xTick: (b) => this.clock(b.t0),
      tipTitle: bucketTitle,
      tipValue: (v) => fmt.dec(v),
      tipExtra: (b) => [["at the busiest moment", fmt.int(b.peak_waiting)], ["delivered", fmt.int(b.delivered)]],
      columns: timeColumns,
      empty: "The first interval hasn't settled yet.",
    });
    this.floorChart = new Chart(this.$("mFloorChart"), {
      type: "bar", yKind: "time",
      label: "Bar chart of average wait for each starting floor",
      series: [{ key: "wait_mean", name: "average wait", color: "--series-1" }],
      xTick: (f) => f.label,
      tipTitle: (f) => (f.name ? `Floor ${f.label}, ${f.name}` : `Floor ${f.label}`),
      tipValue: fmt.secs,
      tipExtra: (f) => [["95th percentile", fmt.secs(f.wait_p95)], ["longest", fmt.secs(f.wait_max)],
                        ["trips from here", fmt.int(f.trips_from)]],
      columns: [
        { title: "Floor", get: (f) => f.label },
        { title: "Name", get: (f) => f.name || "" },
        { title: "Trips from here", get: (f) => fmt.int(f.trips_from), num: true },
        { title: "Trips to here", get: (f) => fmt.int(f.trips_to), num: true },
        { title: "Average wait (s)", get: (f) => secs(f.wait_mean), num: true },
        { title: "95th percentile wait (s)", get: (f) => secs(f.wait_p95), num: true },
        { title: "Longest wait (s)", get: (f) => secs(f.wait_max), num: true },
      ],
      empty: "No waits measured yet.",
    });
  }

  $(id) {
    return document.getElementById(this.prefix + id);
  }

  clock(t) { return fmt.clock(t + this.dayStart); }
  clockSecs(t) { return fmt.clockSecs(t + this.dayStart); }

  /** Poll the live run, only while the tab is showing. */
  setActive(on) {
    if (on === this.active) return;
    this.active = on;
    clearInterval(this.timer);
    if (on) {
      this.refresh();
      this.timer = setInterval(() => this.refresh(), 2000);
    }
  }

  async refresh() {
    try {
      const res = await fetch("/api/metrics");
      if (!res.ok) throw new Error(`Server answered ${res.status}`);
      this.render(await res.json());
    } catch (err) {
      this.scope.textContent = `Couldn't load metrics: ${err.message}. Trying again shortly.`;
    }
  }

  render(r) {
    const p = r.passengers, s = r.system, f = r.fleet, hc = r.hall_calls;
    const interval = `${r.bucket_seconds / 60}-minute`;
    const timed = r.run.day_start !== null && r.run.day_start !== undefined;
    this.dayStart = timed ? r.run.day_start : 0;
    const clockKind = timed ? "" : " of simulated time";
    const runName = (r.run.description || "steady traffic").replace(/^./, (c) => c.toUpperCase());
    const algorithm = r.run.dispatcher_label || r.run.dispatcher;
    const who = [algorithm ? `Algorithm: ${algorithm}` : "Algorithm not recorded",
                 r.run.seed !== null && r.run.seed !== undefined ? `seed ${r.run.seed}` : null]
      .filter(Boolean).join(", ");

    if (r.warming_up) {
      this.scope.textContent = `${runName}. Warming up: measuring starts at ${this.clockSecs(r.warmup)}${clockKind}, `
        + `so the start from an empty building doesn't skew the numbers. It is now ${this.clockSecs(r.time)}. `
        + "Change the warm-up period in Settings.";
    } else {
      this.scope.textContent = `${runName}. Measuring since ${this.clockSecs(r.warmup)}${clockKind}: `
        + `${fmt.int(p.measured)} people over ${fmt.span(r.measured_seconds)}. ${who}.`;
    }
    const missing = r.source && r.source.missing && r.source.missing.length;
    this.source.hidden = !missing;
    if (missing) this.source.textContent = r.source.note;
    this.body.classList.toggle("is-warming", r.warming_up);

    this.notice.hidden = !s.overloaded;
    if (s.overloaded) {
      this.notice.textContent = `The elevators aren't keeping up. ${fmt.int(s.backlog_change)} more people are waiting `
        + "or riding than when measuring began, and waits are still growing, so the averages below understate them.";
    }

    this.tiles([
      ["Average wait", fmt.secs(p.wait.mean),
        Math.round(p.waiting || 0) ? `includes ${fmt.int(p.waiting)} still waiting` : `${fmt.int(p.measured)} people`],
      ["95th percentile wait", fmt.secs(p.wait.p95), "1 person in 20 waits longer"],
      [`Waits over ${r.long_wait} s`, fmt.pct(p.long_wait_share), `${fmt.int(p.long_waits)} of ${fmt.int(p.measured)} people`],
      ["Average trip", fmt.secs(p.trip.mean), "button press to arrival"],
      ["Delivered per hour", fmt.int(s.delivered_per_hour), `${fmt.int(s.arrivals_per_hour)} arriving per hour`],
      ["Floors traveled per person", fmt.dec(f.floors_per_delivery),
        f.empty_share === null || f.empty_share === undefined ? "not in this file" : `${fmt.pct(f.empty_share)} of travel is empty`],
    ]);

    const distRow = (name, d) => [name, fmt.dec(d.mean), fmt.dec(d.p50), fmt.dec(d.p90), fmt.dec(d.p95),
                                  fmt.dec(d.p99), fmt.dec(d.max), fmt.int(d.n)];
    this.table("mPassengers", [distRow("Wait", p.wait), distRow("Ride", p.ride), distRow("Whole trip", p.trip)]);
    // Reports saved before this chart existed don't have the percentiles.
    const tripPercentiles = p.trip_percentiles;
    this.tripChart.emptyEl.textContent = tripPercentiles || !p.delivered
      ? "No one has finished a trip yet."
      : "This report was saved before trip percentiles were recorded. Import its trips file (CSV) to see this chart.";
    this.tripChart.setData((tripPercentiles || []).map((trip, q) => ({ t0: q, t1: q, q, trip })));
    this.table("mDirections", Object.entries(r.by_direction).map(([name, d]) =>
      [name === "up" ? "Going up" : "Going down", fmt.int(d.people), fmt.dec(d.wait_mean), fmt.dec(d.wait_p95),
       fmt.dec(d.trip_mean), fmt.pct(d.straight_shot_share)]));
    this.facts("mPassengerFacts", [
      ["Still waiting", fmt.int(p.waiting)],
      ["Riding now", fmt.int(p.riding)],
      ...(p.changing ? [["Changing cars now", fmt.int(p.changing)]] : []),
      ["Delivered", fmt.int(p.delivered)],
      ["Left behind by a full car", `${fmt.int(p.left_behind_people)} people, ${fmt.int(p.left_behind_events)} times`],
      ["Other stops on the way, average", fmt.dec(p.stops_per_ride, 2)],
      ["Straight shots", p.delivered
        ? `${fmt.pct(p.straight_shot_share)}, ${fmt.int(p.straight_shots)} of ${fmt.int(p.delivered)} delivered`
        : fmt.none],
      ...(p.changes ? [
        ["Changes of car", `${fmt.int(p.changes)}, waiting ${fmt.secs(p.change_wait.mean)} on average for the next car`
          + ` (95th percentile ${fmt.secs(p.change_wait.p95)}). Waits above add these in.`],
        ["Whole journey when changing cars", p.journeys_with_change
          ? `${fmt.secs(p.journey_with_change.mean)} on average, 95th percentile ${fmt.secs(p.journey_with_change.p95)}`
            + `, ${fmt.int(p.journeys_with_change)} journeys`
          : fmt.none],
      ] : []),
    ]);
    this.facts("mCallFacts", [
      ["Hall calls answered", fmt.int(hc.answered)],
      ["Average response", fmt.secs(hc.response.mean)],
      ["95th percentile response", fmt.secs(hc.response.p95)],
      ["Longest response", fmt.secs(hc.response.max)],
      ["Buttons lit now", fmt.int(hc.open)],
      ["Oldest lit button", fmt.secs(hc.oldest_open)],
    ]);
    this.facts("mQueueFacts", [
      ["People waiting, average", fmt.dec(s.avg_waiting)],
      ["People waiting, peak", fmt.int(s.peak_waiting)],
      ["People riding, average", fmt.dec(s.avg_riding)],
      [`Most delivered in one ${interval} interval`, fmt.int(s.peak_delivered_per_interval)],
      ["Change in people waiting or riding", fmt.signed(s.backlog_change)],
    ]);

    const carRow = (name, c, capacity) => [name, fmt.int(c.distance), fmt.pct(c.empty_share), fmt.int(c.starts),
      fmt.int(c.door_cycles), fmt.pct(c.share_moving), fmt.pct(c.share_doors), fmt.pct(c.share_idle),
      fmt.dec(c.avg_load_departing), fmt.pct(c.full_departure_share),
      c.peak_load === null || c.peak_load === undefined ? fmt.none : `${c.peak_load} of ${capacity}`, fmt.int(c.delivered)];
    const capacity = r.cars.length ? Math.max(...r.cars.map((c) => c.capacity)) : 0;
    this.table("mCars", [
      ...r.cars.map((c) => carRow(carName(c, r.settings?.elevator_groups), c, c.capacity)),
      carRow("All cars", f, capacity),
    ], true);

    for (const node of this.root.querySelectorAll("[data-interval]")) node.textContent = interval;
    // Plot settled intervals only. While an interval's latest arrivals are still waiting, their
    // waits-so-far are short, which would draw a misleading dive at the right edge.
    const finished = r.timeline.filter((b) => b.settled);
    this.waitChart.setData(finished);
    this.queueChart.setData(finished);
    this.floorChart.setData(r.by_floor);
  }

  tiles(items) {
    this.$("mTiles").replaceChildren(...items.map(([label, value, note]) =>
      el("div", { class: "tile" }, el("p", { class: "tile-label" }, label),
        el("p", { class: "tile-value" }, value), el("p", { class: "tile-note" }, note))));
  }

  /** Fill a table body; the first cell of each row is its header. */
  table(id, rows, lastIsTotal = false) {
    this.$(id).replaceChildren(...rows.map((cells, i) => {
      const tr = el("tr", { class: lastIsTotal && i === rows.length - 1 ? "is-total" : null },
        el("th", { scope: "row" }, cells[0]), ...cells.slice(1).map((c) => el("td", { class: "num" }, c)));
      return tr;
    }));
  }

  facts(id, pairs) {
    this.$(id).replaceChildren(...pairs.map(([label, value]) =>
      el("div", {}, el("dt", {}, label), el("dd", {}, value))));
  }
}

window.MetricsView = MetricsView;

/** "Car 3", or with several elevator groups "High zone, car 2" (numbered within the group). */
function carName(car, groups) {
  if (!groups || groups.length < 2 || car.group === undefined || !groups[car.group]) return `Car ${car.id + 1}`;
  const first = groups.slice(0, car.group).reduce((n, g) => n + g.cars, 0);
  return `${groups[car.group].name}, car ${car.id - first + 1}`;
}
