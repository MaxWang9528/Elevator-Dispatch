// Draws the building cross-section on a <canvas>.
//
// The server sends frames ~15 times a second. Between frames this interpolates car positions,
// and people are "sprites" that glide when they change container (queue -> car -> out the door).
"use strict";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const lerp = (a, b, t) => a + (b - a) * t;
const hexToRgb = (hex) => {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? [...h].map((ch) => ch + ch).join("") : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
};
const easeInOut = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

class TowerRenderer {
  constructor(canvas, scroller) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.scroller = scroller;
    this.layout = null;
    this.prev = null;          // previous frame
    this.cur = null;           // latest frame
    this.curAt = 0;            // performance.now() when `cur` arrived
    this.frameGap = 1000 / 15; // smoothed gap between frames, ms
    this.sprites = new Map();  // person id -> sprite
    this.hoverFloor = null;
    this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    this.readColors();
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.readColors());
    new ResizeObserver(() => this.resize()).observe(scroller);
    canvas.addEventListener("mousemove", (e) => { this.hoverFloor = this.floorAt(e); });
    canvas.addEventListener("mouseleave", () => { this.hoverFloor = null; });
    requestAnimationFrame((t) => this.loop(t));
  }

  readColors() {
    const css = getComputedStyle(document.documentElement);
    const names = ["bg", "ink", "muted", "shaft", "car", "car-open", "up", "down", "amber",
                   "floor-line", "parking", "lobby", "amenity", "retail", "lantern-off", "hover",
                   "speed-0", "speed-mid", "speed-1"];
    this.c = Object.fromEntries(names.map((n) => [n, css.getPropertyValue(`--${n}`).trim()]));
    this.speedRamp = ["speed-0", "speed-mid", "speed-1"].map((n) => hexToRgb(this.c[n]));
    this.fontNum = css.getPropertyValue("--font-num").trim();
    this.fontUi = css.getPropertyValue("--font-ui").trim();
  }

  setLayout(layout) {
    this.layout = layout;
    this.prev = this.cur = null;
    this.sprites.clear();   // person ids restart after a reset
    this.resize();
  }

  setFrame(frame) {
    const now = performance.now();
    if (this.cur) this.frameGap = lerp(this.frameGap, clamp(now - this.curAt, 20, 500), 0.2);
    this.prev = this.cur && this.cur.cars.length === frame.cars.length ? this.cur : frame;
    this.cur = frame;
    this.curAt = now;
  }

  // ---- geometry ------------------------------------------------------------------------
  resize() {
    if (!this.layout) return;
    const w = this.scroller.clientWidth;
    const h = this.scroller.clientHeight;
    const n = this.layout.floors.length;
    const cars = this.layout.cars.length;
    const g = { pad: 16 };
    g.floorH = clamp((h - 2 * g.pad) / n, 15, 30);
    g.height = Math.ceil(n * g.floorH + 2 * g.pad);
    g.width = w;
    // Compact drawing, centred: [labels][corridor with queues][lanterns][shafts]
    const labelW = 36, lanternW = 28, side = 16;
    g.gap = cars > 8 ? 6 : 10;
    g.shaftW = clamp((w - 2 * side - labelW - lanternW - 160) / cars - g.gap, 18, 60);
    g.shaftsW = cars * g.shaftW + (cars - 1) * g.gap;
    const corridorW = clamp(w - 2 * side - labelW - lanternW - g.shaftsW, 100, 340);
    const total = labelW + corridorW + lanternW + g.shaftsW;
    g.left = Math.max(side, (w - total) / 2);
    g.labelX = g.left + labelW;              // right edge of the floor labels
    g.queueLeft = g.labelX + 8;
    g.queueRight = g.labelX + corridorW;
    g.lanternX = g.queueRight + lanternW / 2;
    g.shaftsX = g.queueRight + lanternW;
    g.dotR = clamp(g.floorH * 0.13, 1.8, 3.2);
    g.dotStep = 2 * g.dotR + 2.2;
    g.maxQueueDots = Math.max(1, Math.floor((g.queueRight - g.queueLeft - 26) / g.dotStep));
    this.g = g;

    const dpr = window.devicePixelRatio || 1;
    this.canvas.style.width = `${w}px`;
    this.canvas.style.height = `${g.height}px`;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(g.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /** y of the top edge of floor `i` (may be fractional, for cars between floors). */
  floorTop(i) {
    const n = this.layout.floors.length;
    return this.g.pad + (n - 1 - i) * this.g.floorH;
  }

  shaftX(carIndex) {
    return this.g.shaftsX + carIndex * (this.g.shaftW + this.g.gap);
  }

  floorAt(evt) {
    if (!this.layout) return null;
    const rect = this.canvas.getBoundingClientRect();
    const y = evt.clientY - rect.top;
    const n = this.layout.floors.length;
    const i = n - 1 - Math.floor((y - this.g.pad) / this.g.floorH);
    return i >= 0 && i < n ? i : null;
  }

  carBox(car, pos) {
    const x = this.shaftX(car.id) + 2;
    const w = this.g.shaftW - 4;
    const y = this.floorTop(pos) + 2;
    const h = this.g.floorH - 4;
    return { x, y, w, h };
  }

  riderSlot(box, index, capacity) {
    const innerX = box.x + 5, innerW = box.w - 8;
    const innerY = box.y + 2, innerH = box.h - 4;
    // Grid with roughly square cells; dots shrink to fit a full car.
    const rows = clamp(Math.round(Math.sqrt((capacity * innerH) / innerW)), 1, capacity);
    const cols = Math.ceil(capacity / rows);
    const sx = innerW / cols, sy = innerH / rows;
    const col = index % cols, row = Math.floor(index / cols) % rows;
    const r = Math.max(0.9, Math.min(this.g.dotR, sx * 0.42, sy * 0.42));
    return { x: innerX + (col + 0.5) * sx, y: innerY + (row + 0.5) * sy, r };
  }

  queueSlot(floor, dir, index) {
    const top = this.floorTop(floor);
    const y = top + this.g.floorH * (dir === "up" ? 0.33 : 0.69);
    return { x: this.g.queueRight - (index + 0.5) * this.g.dotStep, y, r: this.g.dotR };
  }

  // ---- animation loop -------------------------------------------------------------------
  loop(now) {
    if (this.layout && this.cur) this.draw(now);
    requestAnimationFrame((t) => this.loop(t));
  }

  interpolatedCars(now) {
    const t = clamp((now - this.curAt) / this.frameGap, 0, 1);
    return this.cur.cars.map((car, i) => {
      const before = this.prev.cars[i] || car;
      const jump = Math.abs(car.pos - before.pos) > 3;   // e.g. after a reset: don't glide
      return {
        ...car,
        pos: jump ? car.pos : lerp(before.pos, car.pos, t),
        vel: jump ? car.vel : lerp(before.vel ?? 0, car.vel ?? 0, t),
      };
    });
  }

  draw(now) {
    const { ctx, g, c } = this;
    const floors = this.layout.floors;
    const cars = this.interpolatedCars(now);
    const frame = this.cur;

    ctx.fillStyle = c.bg;
    ctx.fillRect(0, 0, g.width, g.height);

    // floors: bands, labels, names, lines
    ctx.textBaseline = "middle";
    const labelFont = `600 ${Math.round(clamp(g.floorH * 0.62, 11, 15))}px ${this.fontNum}`;
    const nameFont = `500 ${Math.round(clamp(g.floorH * 0.55, 11, 13))}px ${this.fontUi}`;
    for (const f of floors) {
      const top = this.floorTop(f.i);
      const band = f.kind === "residential" ? null : c[f.kind];
      const bandW = g.shaftsX - 4 - g.left;
      if (band) {
        ctx.fillStyle = band;
        ctx.fillRect(g.left, top, bandW, g.floorH);
      }
      if (this.hoverFloor === f.i) {
        ctx.fillStyle = c.hover;
        ctx.fillRect(g.left, top, bandW, g.floorH);
      }
      ctx.textAlign = "right";
      ctx.font = labelFont;
      ctx.fillStyle = f.kind === "residential" ? c.muted : c.ink;
      ctx.fillText(f.label, g.labelX - 6, top + g.floorH / 2 + 0.5);
      if (f.name) {
        ctx.textAlign = "left";
        ctx.font = nameFont;
        ctx.fillStyle = c.muted;
        ctx.fillText(f.name, g.queueLeft, top + g.floorH / 2 + 0.5, g.queueRight - g.queueLeft - 8);
      }
      ctx.fillStyle = c["floor-line"];
      ctx.fillRect(g.labelX, top + g.floorH - 1, g.shaftsX - g.labelX - 4, 1);
    }

    // shafts
    const shaftTop = this.floorTop(floors.length - 1);
    const shaftH = floors.length * g.floorH;
    ctx.fillStyle = c.shaft;
    for (const car of cars) ctx.fillRect(this.shaftX(car.id), shaftTop, g.shaftW, shaftH);

    // assigned pickups (notch on the shaft's left edge) and car calls (amber dot, right edge)
    for (const car of cars) {
      const x = this.shaftX(car.id);
      for (const [floor, dir] of car.hall_stops) {
        ctx.fillStyle = dir === "up" ? c.up : c.down;
        const top = this.floorTop(floor);
        ctx.fillRect(x, top + (dir === "up" ? 2 : g.floorH / 2), 3, g.floorH / 2 - 2);
      }
      ctx.fillStyle = c.amber;
      for (const floor of car.car_calls) {
        ctx.beginPath();
        ctx.arc(x + g.shaftW - 3.5, this.floorTop(floor) + g.floorH / 2, 2, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // hall lanterns: lit when the button is pressed
    const lit = new Set(frame.hall_calls.map((h) => `${h.floor}:${h.dir}`));
    const top0 = this.layout.floors.length - 1;
    for (const f of floors) {
      const top = this.floorTop(f.i);
      const mid = top + g.floorH / 2;
      const s = clamp(g.floorH * 0.2, 2.5, 4.5);
      if (f.i < top0) this.triangle(g.lanternX, mid - s * 0.35 - 1, s, true, lit.has(`${f.i}:up`) ? c.up : c["lantern-off"]);
      if (f.i > 0) this.triangle(g.lanternX, mid + s * 0.35 + 1, s, false, lit.has(`${f.i}:down`) ? c.down : c["lantern-off"]);
    }

    // cars
    for (const car of cars) this.drawCar(car);

    // people
    this.drawPeople(now, cars);
  }

  /** 0 = stopped, 1 = top speed -> color along the speed ramp. */
  speedColor(t) {
    const ramp = this.speedRamp;
    const x = clamp(t, 0, 1) * (ramp.length - 1);
    const i = Math.min(Math.floor(x), ramp.length - 2);
    const f = x - i;
    const [a, b] = [ramp[i], ramp[i + 1]];
    return `rgb(${[0, 1, 2].map((k) => Math.round(lerp(a[k], b[k], f))).join(",")})`;
  }

  triangle(cx, cy, s, up, color) {
    const { ctx } = this;
    ctx.fillStyle = color;
    ctx.beginPath();
    if (up) {
      ctx.moveTo(cx, cy - s); ctx.lineTo(cx + s, cy + s * 0.6); ctx.lineTo(cx - s, cy + s * 0.6);
    } else {
      ctx.moveTo(cx, cy + s); ctx.lineTo(cx + s, cy - s * 0.6); ctx.lineTo(cx - s, cy - s * 0.6);
    }
    ctx.closePath();
    ctx.fill();
  }

  drawCar(car) {
    const { ctx, c } = this;
    const b = this.carBox(car, car.pos);
    const maxSpeed = this.layout.cars[car.id]?.max_speed || 1;
    ctx.fillStyle = car.door > 0 ? c["car-open"] : this.speedColor(Math.abs(car.vel || 0) / maxSpeed);
    ctx.fillRect(b.x, b.y, b.w, b.h);
    // door: a bar on the corridor side that shrinks toward the middle as it opens
    const doorH = b.h * (1 - car.door);
    if (doorH > 0.5) {
      ctx.fillStyle = "#fff";      // black in both themes, to stand out against any car color
      ctx.fillRect(b.x, b.y + (b.h - doorH) / 2, 3, doorH);
    }
    // direction arrow, top-right corner
    if (car.dir !== "idle") {
      const s = clamp(b.h * 0.16, 2, 3.5);
      this.triangle(b.x + b.w - s - 2, b.y + s + 2, s, car.dir === "up", car.dir === "up" ? c.up : c.down);
    }
  }

  drawPeople(now, cars) {
    const { ctx, g, c } = this;
    const speed = this.cur.speed || 1;
    const dur = this.reducedMotion.matches ? 1 : clamp(650 / speed, 60, 450);
    const targets = new Map();

    for (const f of this.cur.floors) {
      for (const dir of ["up", "down"]) {
        const q = f[dir];
        const shown = Math.min(q.length, g.maxQueueDots);
        for (let i = 0; i < shown; i++) {
          targets.set(q[i], { ...this.queueSlot(f.i, dir, i), key: `f${f.i}${dir}`, dir });
        }
        if (q.length > shown) {
          const last = this.queueSlot(f.i, dir, shown);
          ctx.fillStyle = dir === "up" ? c.up : c.down;
          ctx.textAlign = "right";
          ctx.textBaseline = "middle";
          ctx.font = `600 ${Math.round(clamp(g.floorH * 0.45, 10, 13))}px ${this.fontNum}`;
          ctx.fillText(`+${q.length - shown}`, last.x + g.dotR + 2, last.y + 0.5);
        }
      }
    }
    for (const car of cars) {
      const box = this.carBox(car, car.pos);
      const cap = this.layout.cars[car.id].capacity;
      car.riders.forEach((id, j) => {
        targets.set(id, { ...this.riderSlot(box, j, cap), key: `c${car.id}`, dir: car.dir });
      });
    }

    // move sprites toward their targets; start a glide whenever someone changes container
    for (const [id, t] of targets) {
      let s = this.sprites.get(id);
      if (!s) {
        s = { key: t.key, x: t.x, y: t.y, r: t.r, fromX: t.x, fromY: t.y, fromR: t.r, t0: now, born: now,
              dir: t.dir === "idle" ? null : t.dir };
        this.sprites.set(id, s);
      } else if (s.leaving) {
        s.leaving = false;
      }
      if (s.key !== t.key) {
        s.fromX = s.x; s.fromY = s.y; s.fromR = s.r; s.t0 = now; s.key = t.key;
      }
      const k = easeInOut(clamp((now - s.t0) / dur, 0, 1));
      s.x = lerp(s.fromX, t.x, k);
      s.y = lerp(s.fromY, t.y, k);
      s.r = lerp(s.fromR, t.r, k);
      s.alpha = clamp((now - s.born) / dur, 0, 1);
    }
    // people who left (arrived): step out of the car's door side and fade
    for (const [id, s] of this.sprites) {
      if (targets.has(id)) continue;
      if (!s.leaving) {
        s.leaving = true; s.fromX = s.x; s.fromY = s.y; s.t0 = now;
      }
      const k = clamp((now - s.t0) / (dur * 1.4), 0, 1);
      if (k >= 1) { this.sprites.delete(id); continue; }
      s.x = lerp(s.fromX, s.fromX - 8, easeInOut(k));
      s.alpha = 1 - k;
    }

    ctx.lineWidth = 1;
    ctx.strokeStyle = c.bg;
    for (const s of this.sprites.values()) {
      ctx.globalAlpha = s.alpha;
      ctx.fillStyle = s.dir === "up" ? c.up : s.dir === "down" ? c.down : c.ink;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
      // riders sit on a car whose color changes with speed; a ring keeps them legible on all of it
      if (s.key[0] === "c" && !s.leaving) ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
}

window.TowerRenderer = TowerRenderer;
