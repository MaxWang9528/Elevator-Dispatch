// The Settings tab. Ordinary fields are generated from the schema the server sends
// (Settings.schema() in Python), so new settings appear here without touching this file.
"use strict";

class SettingsView {
  constructor() {
    this.form = document.getElementById("settingsForm");
    this.groupsEl = document.getElementById("settingsGroups");
    this.rowsEl = document.getElementById("sfRows");
    this.egRowsEl = document.getElementById("egRows");
    this.sections = new Map();     // schema group name -> {title, help} elements
    this.msgEl = document.getElementById("settingsMsg");
    this.schema = [];
    this.kinds = [];
    this.saved = null;
    this.dirty = false;
    this.presets = [];
    this.currentPreset = null;
    this.presetsEl = document.getElementById("presets");

    // Typing a preset name isn't a change to the settings.
    const edited = (e) => { if (!this.presetsEl.contains(e.target)) this.markDirty(); };
    this.form.addEventListener("input", edited);
    this.form.addEventListener("change", edited);
    this.form.addEventListener("submit", (e) => { e.preventDefault(); this.save(); });
    document.getElementById("discardChanges").addEventListener("click", () => this.discard());
    document.getElementById("restoreDefaults").addEventListener("click", () => this.restoreDefaults());
    document.getElementById("sfAdd").addEventListener("click", () => this.addRow());
    document.getElementById("egAdd").addEventListener("click", () => this.addGroup());
    window.addEventListener("beforeunload", (e) => { if (this.dirty) e.preventDefault(); });
    document.getElementById("presetSave").addEventListener("click", () => this.savePreset());
    document.getElementById("presetName").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.savePreset(); }   // not the settings form
    });
  }

  async load() {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error(`Server answered ${res.status}`);
    const data = await res.json();
    this.schema = data.schema;
    this.groupHelp = data.group_help || {};
    this.groupOffice = data.group_office || {};
    this.groupLimits = data.group_limits || { groups: 20, cars: 40, total: 100 };
    this.kinds = data.kinds;
    this.saved = data.settings;
    document.getElementById("settingsFile").textContent = data.file;
    document.getElementById("presetsFile").textContent = data.presets_file;
    this.renderFields();
    this.fill(this.saved);
    this.setPresets(data);
  }

  // ---- building the form ----------------------------------------------------------------
  renderFields() {
    const groups = new Map();
    for (const f of this.schema) {
      if (!groups.has(f.group)) groups.set(f.group, []);
      groups.get(f.group).push(f);
    }
    this.sections.clear();
    this.groupsEl.replaceChildren(...[...groups].map(([name, fields]) => {
      const section = el("section", { class: "settings-group" });
      const title = el("h2", {}, name);
      const help = el("p", { class: "help" }, this.groupHelp[name] || "");
      help.hidden = !this.groupHelp[name];
      section.append(title, help);
      // The elevator groups table leads the Elevators section.
      if (name === "Elevators") section.append(document.getElementById("egBlock"));
      section.append(...fields.map((f) => this.renderField(f)));
      this.sections.set(name, { title, help });
      return section;
    }));
    // Switches grey out the fields that depend on them.
    for (const f of this.schema) {
      if (f.type === "bool") this.form.elements[f.key].addEventListener("change", () => this.syncEnabled());
    }
    this.form.elements.office?.addEventListener("change", () => this.syncOffice());
  }

  /** An office building renames a few settings (residents become the people who work here). */
  syncOffice() {
    const office = Boolean(this.form.elements.office?.checked);
    for (const f of this.schema) {
      if (!f.office_label && !f.office_help) continue;
      const wrap = this.groupsEl.querySelector(`[data-key="${f.key}"]`);
      wrap.querySelector("label").textContent = office && f.office_label ? f.office_label : f.label;
      document.getElementById(`help-${f.key}`).textContent = office && f.office_help ? f.office_help : f.helpText;
    }
    for (const [name, { title, help }] of this.sections) {
      const [officeTitle, officeHelp] = this.groupOffice[name] || [];
      title.textContent = office && officeTitle ? officeTitle : name;
      const text = office && officeHelp ? officeHelp : this.groupHelp[name] || "";
      help.textContent = text;
      help.hidden = !text;
    }
  }

  renderField(f) {
    const id = `set-${f.key}`;
    const wrap = el("div", { class: "field", "data-key": f.key });
    const described = { "aria-describedby": `help-${f.key} err-${f.key}` };
    let input, help = f.help;
    if (f.type === "bool") {
      input = el("input", { type: "checkbox", id, name: f.key, ...described });
      help ||= `Default ${f.default ? "on" : "off"}.`;
    } else if (f.type === "choice") {
      input = el("select", { id, name: f.key, ...described }, ...f.options.map((o) => new Option(o.label, o.value)));
      help ||= "";
    } else if (f.type === "floors") {
      input = el("input", { type: "text", id, name: f.key, placeholder: f.placeholder || "", spellcheck: "false", ...described });
      help ||= "";
    } else if (f.type === "time") {
      input = el("input", { type: "time", id, name: f.key, required: "", ...described });
      help ||= `Default ${f.default}.`;
    } else {
      input = el("input", {
        type: "number", id, name: f.key, min: f.min, max: f.max, step: f.step,
        inputmode: f.type === "int" ? "numeric" : "decimal", required: "", ...described,
      });
      help ||= `Default ${f.default}.`;
    }
    f.helpText = help;
    const box = el("div", { class: "field-input" }, input);
    if (f.unit) box.append(el("span", { class: "unit" }, f.unit));
    wrap.append(
      el("label", { for: id }, f.label),
      box,
      el("p", { class: "help", id: `help-${f.key}` }, help),
      el("p", { class: "field-error", id: `err-${f.key}`, hidden: "" }),
    );
    return wrap;
  }

  /** Fields that belong to a switch are greyed out (but kept) while it is off. */
  syncEnabled() {
    for (const f of this.schema) {
      if (!f.enabled_by && !f.disabled_by) continue;
      const on = f.enabled_by ? this.form.elements[f.enabled_by].checked : !this.form.elements[f.disabled_by].checked;
      this.form.elements[f.key].disabled = !on;
      this.groupsEl.querySelector(`[data-key="${f.key}"]`).classList.toggle("is-off", !on);
    }
  }

  fill(settings) {
    for (const f of this.schema) {
      const input = this.form.elements[f.key];
      if (!input) continue;
      if (f.type === "bool") input.checked = Boolean(settings[f.key]);
      else input.value = settings[f.key];
    }
    this.syncEnabled();
    this.syncOffice();
    this.rowsEl.replaceChildren(...settings.special_floors.map((sf) => this.renderRow(sf)));
    this.egRowsEl.replaceChildren(...settings.elevator_groups.map((g) => this.renderGroupRow(g)));
    this.clearErrors();
    this.updateRows();
    this.updateGroups();
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

  // ---- elevator groups ----------------------------------------------------------------------
  renderGroupRow(g) {
    const tr = el("tr");
    const input = (attrs, value) => {
      const node = el("input", attrs);
      node.value = value;
      return node;
    };
    const remove = el("button", { type: "button", class: "btn btn-quiet btn-remove" }, "Remove");
    remove.addEventListener("click", () => { tr.remove(); this.updateGroups(); this.markDirty(); });
    tr.append(
      el("td", {}, input({ type: "text", maxlength: 40, "data-prop": "name", "aria-label": "Group name", placeholder: "e.g. High-rise" }, g.name)),
      el("td", {}, input({ type: "number", min: 1, max: this.groupLimits.cars, step: 1, inputmode: "numeric", "data-prop": "cars", "aria-label": "Cars", required: "" }, g.cars)),
      el("td", {}, input({ type: "text", "data-prop": "floors", "aria-label": "Floors served", placeholder: "all", spellcheck: "false" }, g.floors)),
      el("td", {}, input({ type: "number", min: 1, max: 40, step: 1, inputmode: "numeric", "data-prop": "capacity", "aria-label": "People per car", required: "" }, g.capacity)),
      el("td", {}, el("div", { class: "field-input" },
        input({ type: "number", min: 0.2, max: 5, step: 0.1, inputmode: "decimal", "data-prop": "speed", "aria-label": "Speed, floors per second", required: "" }, g.speed),
        el("span", { class: "unit" }, "floors/s"))),
      el("td", {}, remove),
    );
    tr.querySelector("[data-prop=cars]").addEventListener("input", () => this.updateGroups());
    tr.querySelector("[data-prop=name]").addEventListener("input", () => this.updateGroups());
    return tr;
  }

  addGroup() {
    const rows = [...this.egRowsEl.rows];
    const last = rows.length ? this.collect().elevator_groups.at(-1) : { capacity: 9, speed: 2.5 };
    const row = this.renderGroupRow({ name: `Group ${rows.length + 1}`, cars: 2, floors: "all",
                                      capacity: last.capacity, speed: last.speed });
    this.egRowsEl.append(row);
    this.updateGroups();
    this.markDirty();
    row.querySelector("[data-prop=name]").select();
  }

  updateGroups() {
    const rows = [...this.egRowsEl.rows];
    const total = rows.reduce((n, tr) => n + (Number(tr.querySelector("[data-prop=cars]").value) || 0), 0);
    for (const tr of rows) {
      const btn = tr.querySelector(".btn-remove");
      btn.disabled = rows.length === 1;     // a building needs at least one group
      btn.setAttribute("aria-label", `Remove ${tr.querySelector("[data-prop=name]").value || "this group"}`);
    }
    document.getElementById("egAdd").disabled = rows.length >= this.groupLimits.groups;
    const out = document.getElementById("egTotal");
    const cars = `${total.toLocaleString()} ${total === 1 ? "car" : "cars"}`;
    out.textContent = total > this.groupLimits.total
      ? `${cars} in all. The most is ${this.groupLimits.total}.`
      : rows.length > 1 ? `${cars} in ${rows.length} groups.` : "";
    out.classList.toggle("is-error", total > this.groupLimits.total);
  }

  updateTotal() {
    const total = this.collect().special_floors.reduce((sum, s) => sum + (Number(s.share) || 0), 0);
    const out = document.getElementById("sfTotal");
    const pct = (v) => `${Math.round(v * 10) / 10}%`;
    if (total > 100) {
      out.textContent = `Shares add up to ${pct(total)}. Lower them to 100% or less.`;
    } else if (this.rowsEl.rows.length) {
      out.textContent = `Special floors get ${pct(total)} of outings. The other ${pct(100 - total)} visit another resident's floor.`;
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
      if (input) data[f.key] = f.type === "bool" ? input.checked : input.value;
    }
    data.special_floors = [...this.rowsEl.rows].map((tr) => {
      const v = (p) => tr.querySelector(`[data-prop=${p}]`).value;
      return { floor: v("floor"), name: v("name"), kind: v("kind"), share: v("share") };
    });
    data.elevator_groups = [...this.egRowsEl.rows].map((tr) => {
      const v = (p) => tr.querySelector(`[data-prop=${p}]`).value;
      return { name: v("name"), cars: v("cars"), floors: v("floors"), capacity: v("capacity"), speed: v("speed") };
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
      this.setPresets(result);
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
      this.setPresets(result);
      this.setMessage("Defaults restored and saved. The simulation restarted.");
    }
  }

  async post(path, body, onError = (field, message) => this.showError(field, message)) {
    let res, data;
    try {
      res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      data = await res.json();
    } catch (err) {
      onError(null, "Couldn't reach the server. Check that it's running, then try again.", true);
      return null;
    }
    if (!res.ok) {
      onError(data.field, data.error || `Server answered ${res.status}`);
      return null;
    }
    return data;
  }

  // ---- presets ------------------------------------------------------------------------------
  setPresets(data) {
    if (!data.presets) return;
    this.presets = data.presets;
    this.currentPreset = data.current_preset;
    const list = document.getElementById("presetList");
    list.replaceChildren(...this.presets.map((p) => this.renderPreset(p)));
  }

  renderPreset(p) {
    const current = p.name === this.currentPreset;
    const s = p.settings;
    const algorithm = this.schema.find((f) => f.key === "dispatcher")?.options
      .find((o) => o.value === s.dispatcher)?.label ?? s.dispatcher;
    const plural = (n, one, many) => `${Number(n).toLocaleString()} ${Number(n) === 1 ? one : many}`;
    const groups = s.elevator_groups || [];
    const cars = groups.reduce((n, g) => n + Number(g.cars), 0);
    const elevators = groups.length > 1
      ? `${plural(cars, "elevator", "elevators")} in ${groups.length} groups`
      : `${plural(cars, "elevator", "elevators")}, ${groups[0]?.capacity} per car`;
    const meta = [plural(s.floors, "floor", "floors"), ...(s.office ? ["office"] : []), elevators,
      plural(s.residents, s.office ? "person" : "resident", s.office ? "people" : "residents"), algorithm].join(" · ");

    const name = el("span", { class: "preset-name" }, p.name);
    const text = el("div", { class: "preset-text" }, name);
    if (current) text.append(el("span", { class: "preset-badge" }, "In use"));
    text.append(el("span", { class: "preset-meta" }, meta));
    if (p.description) text.append(el("span", { class: "preset-desc" }, p.description));

    const load = el("button", { type: "button", class: "btn", "aria-label": `Load ${p.name}` }, "Load");
    load.addEventListener("click", () => this.loadPreset(p));
    const actions = el("div", { class: "inline" }, load);
    if (!p.builtin) {
      const del = el("button", { type: "button", class: "btn btn-quiet", "aria-label": `Delete ${p.name}` }, "Delete");
      del.addEventListener("click", () => this.deletePreset(p));
      actions.append(del);
    }
    return el("li", { class: `preset${current ? " is-current" : ""}` }, text, actions);
  }

  async loadPreset(p) {
    const losing = this.dirty ? "This discards your unsaved changes on this tab. "
      : this.currentPreset === null ? "The settings in use now aren't saved as a preset, so save them first to keep them. " : "";
    if (!confirm(`Load "${p.name}"? ${losing}This replaces the settings and restarts the simulation.`)) return;
    this.clearErrors();
    this.setPresetMessage(`Loading ${p.name}…`);
    const result = await this.post("/api/presets/load", { name: p.name }, (f, msg) => this.setPresetMessage(msg, "error"));
    if (!result) return;
    this.saved = result.settings;
    this.fill(this.saved);
    this.setPresets(result);
    this.setPresetMessage(`Loaded ${result.loaded}. The simulation restarted with it.`);
  }

  async savePreset() {
    const input = document.getElementById("presetName");
    const name = input.value.trim().replace(/\s+/g, " ");
    this.clearErrors();
    if (!name) return this.showPresetError("Give the preset a name.");
    const existing = this.presets.find((p) => p.name.toLowerCase() === name.toLowerCase());
    if (existing?.builtin) return this.showPresetError(`"${existing.name}" is a built-in preset. Pick another name.`);
    if (existing && !confirm(`Replace the preset "${existing.name}" with the settings on this tab?`)) return;
    this.setPresetMessage("Saving preset…");
    const result = await this.post("/api/presets/save", { name, settings: this.collect() }, (field, msg, network) => {
      if (network || field === "preset_name") return this.showPresetError(msg);
      this.setPresetMessage("Preset not saved: a setting below needs fixing.", "error");
      this.showError(field, msg);
    });
    if (!result) return;
    input.value = "";
    this.setPresets(result);
    this.setPresetMessage(this.dirty
      ? `Saved preset ${result.saved}. Your changes aren't in use until you save and restart.`
      : `Saved preset ${result.saved}.`);
  }

  async deletePreset(p) {
    if (!confirm(`Delete the preset "${p.name}"? This can't be undone.`)) return;
    const result = await this.post("/api/presets/delete", { name: p.name }, (f, msg) => this.setPresetMessage(msg, "error"));
    if (!result) return;
    this.setPresets(result);
    this.setPresetMessage(`Deleted ${p.name}.`);
  }

  showPresetError(message) {
    const input = document.getElementById("presetName");
    const err = document.getElementById("err-preset_name");
    err.textContent = message;
    err.hidden = false;
    input.classList.add("is-invalid");
    input.setAttribute("aria-invalid", "true");
    input.focus();
    this.setPresetMessage("");
  }

  setPresetMessage(text, kind = "") {
    const out = document.getElementById("presetMsg");
    out.textContent = text;
    out.className = `preset-msg${kind ? ` is-${kind}` : ""}`;
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
    const m = /^(special_floors|elevator_groups)(?:\.(\d+)\.(\w+))?/.exec(field);
    if (m) {
      errEl = document.getElementById(`err-${m[1]}`);
      const rows = m[1] === "special_floors" ? this.rowsEl.rows : this.egRowsEl.rows;
      if (m[2] !== undefined) input = rows[Number(m[2])]?.querySelector(`[data-prop=${m[3]}]`);
    } else {
      input = this.form.elements[field];
      errEl = document.getElementById(`err-${field}`);
    }
    if (errEl) { errEl.textContent = message; errEl.hidden = false; }
    if (input) {
      input.classList.add("is-invalid");
      input.setAttribute("aria-invalid", "true");
      input.scrollIntoView({ block: "center" });
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
