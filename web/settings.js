// The Settings tab. Ordinary fields are generated from the schema the server sends
// (Settings.schema() in Python), so new settings appear here without touching this file.
"use strict";

class SettingsView {
  constructor() {
    this.form = document.getElementById("settingsForm");
    this.groupsEl = document.getElementById("settingsGroups");
    this.rowsEl = document.getElementById("sfRows");
    this.msgEl = document.getElementById("settingsMsg");
    this.schema = [];
    this.kinds = [];
    this.saved = null;
    this.dirty = false;

    this.form.addEventListener("input", () => this.markDirty());
    this.form.addEventListener("change", () => this.markDirty());
    this.form.addEventListener("submit", (e) => { e.preventDefault(); this.save(); });
    document.getElementById("discardChanges").addEventListener("click", () => this.discard());
    document.getElementById("restoreDefaults").addEventListener("click", () => this.restoreDefaults());
    document.getElementById("sfAdd").addEventListener("click", () => this.addRow());
    window.addEventListener("beforeunload", (e) => { if (this.dirty) e.preventDefault(); });
  }

  async load() {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error(`Server answered ${res.status}`);
    const data = await res.json();
    this.schema = data.schema;
    this.kinds = data.kinds;
    this.saved = data.settings;
    document.getElementById("settingsFile").textContent = data.file;
    this.renderFields();
    this.fill(this.saved);
  }

  // ---- building the form ----------------------------------------------------------------
  renderFields() {
    const groups = new Map();
    for (const f of this.schema) {
      if (!groups.has(f.group)) groups.set(f.group, []);
      groups.get(f.group).push(f);
    }
    this.groupsEl.replaceChildren(...[...groups].map(([name, fields]) => {
      const section = el("section", { class: "settings-group" });
      section.append(el("h2", {}, name), ...fields.map((f) => this.renderField(f)));
      return section;
    }));
  }

  renderField(f) {
    const id = `set-${f.key}`;
    const wrap = el("div", { class: "field" });
    const input = el("input", {
      type: "number", id, name: f.key, min: f.min, max: f.max, step: f.step,
      inputmode: f.type === "int" ? "numeric" : "decimal", required: "",
      "aria-describedby": `help-${f.key} err-${f.key}`,
    });
    const box = el("div", { class: "field-input" }, input);
    if (f.unit) box.append(el("span", { class: "unit" }, f.unit));
    wrap.append(
      el("label", { for: id }, f.label),
      box,
      el("p", { class: "help", id: `help-${f.key}` }, f.help || `Default ${f.default}.`),
      el("p", { class: "field-error", id: `err-${f.key}`, hidden: "" }),
    );
    return wrap;
  }

  fill(settings) {
    for (const f of this.schema) {
      const input = this.form.elements[f.key];
      if (input) input.value = settings[f.key];
    }
    this.rowsEl.replaceChildren(...settings.special_floors.map((sf) => this.renderRow(sf)));
    this.clearErrors();
    this.updateRows();
    this.dirty = false;
    this.setMessage("");
  }

  renderRow(sf) {
    const tr = el("tr");
    const floor = el("input", { type: "number", min: 1, step: 1, "data-prop": "floor", "aria-label": "Floor number", required: "" });
    floor.value = sf.floor;
    const name = el("input", { type: "text", maxlength: 60, "data-prop": "name", "aria-label": "Name", placeholder: "e.g. Fitness center" });
    name.value = sf.name;
    const kind = el("select", { "data-prop": "kind", "aria-label": "Type" },
      ...this.kinds.map((k) => new Option(k.label, k.value)));
    kind.value = sf.kind;
    const swatch = el("span", { class: "sf-swatch", "aria-hidden": "true" });
    const share = el("input", { type: "number", min: 0, max: 100, step: 1, "data-prop": "share", "aria-label": "Share of trips, percent", required: "" });
    share.value = sf.share;
    const remove = el("button", { type: "button", class: "btn btn-quiet btn-remove" }, "Remove");
    remove.addEventListener("click", () => { tr.remove(); this.updateRows(); this.markDirty(); });
    kind.addEventListener("change", () => this.updateRows());
    floor.addEventListener("input", () => this.updateRows());

    tr.append(
      el("td", {}, floor),
      el("td", {}, name),
      el("td", {}, swatch, kind),
      el("td", {}, el("div", { class: "field-input" }, share, el("span", { class: "unit" }, "%"))),
      el("td", {}, remove),
    );
    return tr;
  }

  addRow() {
    const used = new Set(this.collect().special_floors.map((s) => Number(s.floor)));
    const floors = Number(this.form.elements.floors?.value) || 1;
    let free = 1;
    while (used.has(free) && free < floors) free++;
    const row = this.renderRow({ floor: free, name: "", kind: "amenity", share: 0 });
    this.rowsEl.append(row);
    this.updateRows();
    this.markDirty();
    row.querySelector("[data-prop=floor]").focus();
  }

  updateRows() {
    const rows = [...this.rowsEl.rows];
    document.getElementById("sfEmpty").hidden = rows.length > 0;
    for (const tr of rows) {
      const kind = tr.querySelector("[data-prop=kind]").value;
      tr.querySelector(".sf-swatch").style.background = `var(--${kind})`;
      const floor = tr.querySelector("[data-prop=floor]").value;
      tr.querySelector(".btn-remove").setAttribute("aria-label", `Remove floor ${floor}`);
    }
    this.updateTotal();
  }

  updateTotal() {
    const total = this.collect().special_floors.reduce((sum, s) => sum + (Number(s.share) || 0), 0);
    const out = document.getElementById("sfTotal");
    const pct = (v) => `${Math.round(v * 10) / 10}%`;
    if (total > 100) {
      out.textContent = `Shares add up to ${pct(total)}. Lower them to 100% or less.`;
    } else if (this.rowsEl.rows.length) {
      out.textContent = `Special floors take ${pct(total)} of trips. The other ${pct(100 - total)} go between residents' floors.`;
    } else {
      out.textContent = "";
    }
    out.classList.toggle("is-error", total > 100);
  }

  // ---- reading, saving --------------------------------------------------------------------
  collect() {
    const data = {};
    for (const f of this.schema) {
      const input = this.form.elements[f.key];
      if (input) data[f.key] = input.value;
    }
    data.special_floors = [...this.rowsEl.rows].map((tr) => {
      const v = (p) => tr.querySelector(`[data-prop=${p}]`).value;
      return { floor: v("floor"), name: v("name"), kind: v("kind"), share: v("share") };
    });
    return data;
  }

  async save() {
    this.clearErrors();
    this.setMessage("Saving…");
    const result = await this.post("/api/settings", this.collect());
    if (result) {
      this.saved = result.settings;
      this.fill(this.saved);
      this.setMessage("Saved. The simulation restarted with these settings.");
    }
  }

  discard() {
    if (this.saved) this.fill(this.saved);
    this.setMessage("Changes discarded.");
  }

  async restoreDefaults() {
    if (!confirm("Replace all settings with the defaults? This also restarts the simulation.")) return;
    this.clearErrors();
    const result = await this.post("/api/settings/defaults", {});
    if (result) {
      this.saved = result.settings;
      this.fill(this.saved);
      this.setMessage("Defaults restored and saved. The simulation restarted.");
    }
  }

  async post(path, body) {
    let res, data;
    try {
      res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      data = await res.json();
    } catch (err) {
      this.setMessage("Couldn't reach the server. Check that it's running, then save again.", "error");
      return null;
    }
    if (!res.ok) {
      this.showError(data.field, data.error || `Server answered ${res.status}`);
      return null;
    }
    return data;
  }

  // ---- messages and errors ------------------------------------------------------------------
  markDirty() {
    this.dirty = true;
    this.updateTotal();
    this.setMessage("Unsaved changes", "dirty");
  }

  setMessage(text, kind = "") {
    this.msgEl.textContent = text;
    this.msgEl.className = `settings-msg${kind ? ` is-${kind}` : ""}`;
  }

  clearErrors() {
    this.form.querySelectorAll(".is-invalid").forEach((n) => {
      n.classList.remove("is-invalid");
      n.removeAttribute("aria-invalid");
    });
    this.form.querySelectorAll(".field-error").forEach((n) => { n.hidden = true; n.textContent = ""; });
  }

  showError(field, message) {
    this.setMessage(`Not saved. ${message}`, "error");
    if (!field) return;
    let input = null, errEl = null;
    const m = /^special_floors(?:\.(\d+)\.(\w+))?/.exec(field);
    if (m) {
      errEl = document.getElementById("err-special_floors");
      if (m[1] !== undefined) input = this.rowsEl.rows[Number(m[1])]?.querySelector(`[data-prop=${m[2]}]`);
    } else {
      input = this.form.elements[field];
      errEl = document.getElementById(`err-${field}`);
    }
    if (errEl) { errEl.textContent = message; errEl.hidden = false; }
    if (input) {
      input.classList.add("is-invalid");
      input.setAttribute("aria-invalid", "true");
      input.focus();
    }
  }
}

/** Tiny DOM helper: el("input", {type: "number"}, ...children) */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
}

window.SettingsView = SettingsView;
