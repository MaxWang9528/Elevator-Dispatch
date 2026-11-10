// The Import tab: open a saved report (JSON) or trips file (CSV) and see it laid out exactly
// like the Metrics tab. The server turns the file into a report (POST /api/import, see
// imports.py); recent imports are remembered in this browser so they can be reopened.
"use strict";

const IMPORT_STORE = "elevatorImports.v2";
const IMPORT_KEEP = 8;      // most recent imports remembered

class ImportTab {
  constructor() {
    const $ = (id) => document.getElementById(id);
    this.root = $("view-import");
    this.start = $("importStart");
    this.shown = $("importShown");
    this.input = $("importFile");
    this.msg = $("importMsg");
    this.recent = this.load();
    this.view = null;        // the Metrics layout, built the first time something is shown

    $("importPick").addEventListener("click", () => this.input.click());
    this.input.addEventListener("change", () => {
      if (this.input.files[0]) this.importFile(this.input.files[0]);
      this.input.value = "";
    });
    // Drop a file anywhere on the tab.
    this.root.addEventListener("dragover", (e) => {
      if (![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      this.root.classList.add("is-dropping");
    });
    this.root.addEventListener("dragleave", (e) => {
      if (!this.root.contains(e.relatedTarget)) this.root.classList.remove("is-dropping");
    });
    this.root.addEventListener("drop", (e) => {
      e.preventDefault();
      this.root.classList.remove("is-dropping");
      if (e.dataTransfer.files[0]) this.importFile(e.dataTransfer.files[0]);
    });
    this.renderRecent();
  }

  async importFile(file) {
    this.showStart();
    this.setMessage(`Reading ${file.name}…`);
    let res, data;
    try {
      res = await fetch(`/api/import?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
      data = await res.json();
    } catch {
      this.setMessage(`Couldn't send ${file.name} to the server. Check that it's running, then try again.`, true);
      return;
    }
    if (!res.ok) {
      this.setMessage(data.error || `${file.name}: the server answered ${res.status}.`, true);
      return;
    }
    const entry = { id: Date.now(), name: file.name, importedAt: new Date().toISOString(), report: data.report };
    this.recent = [entry, ...this.recent].slice(0, IMPORT_KEEP);
    this.save();
    this.setMessage("");
    this.show(entry);
  }

  show(entry) {
    if (!this.view) {
      const another = el("button", { type: "button", class: "btn" }, "Import another file");
      another.addEventListener("click", () => this.input.click());
      const back = el("button", { type: "button", class: "btn btn-quiet" }, "All imports");
      back.addEventListener("click", () => this.showStart());
      this.view = new MetricsView(this.shown, { prefix: "imp-", title: "", actions: [back, another] });
    }
    this.view.$("mTitle").textContent = entry.name;
    this.start.hidden = true;
    this.shown.hidden = false;
    this.view.render(entry.report);
    this.root.scrollTop = 0;
  }

  showStart() {
    this.shown.hidden = true;
    this.start.hidden = false;
    this.renderRecent();
  }

  remove(id) {
    this.recent = this.recent.filter((r) => r.id !== id);
    this.save();
    this.renderRecent();
  }

  // ---- the list of recent imports ----------------------------------------------------------------
  renderRecent() {
    document.getElementById("importRecent").hidden = this.recent.length === 0;
    document.getElementById("importList").replaceChildren(...this.recent.map((entry) => {
      const open = el("button", { type: "button", class: "import-open" },
        el("span", { class: "import-name" }, entry.name),
        el("span", { class: "import-meta" }, this.describe(entry)));
      open.addEventListener("click", () => this.show(entry));
      const remove = el("button", { type: "button", class: "btn btn-quiet", "aria-label": `Forget ${entry.name}` }, "Forget");
      remove.addEventListener("click", () => this.remove(entry.id));
      return el("li", {}, open, remove);
    }));
  }

  describe(entry) {
    const r = entry.report;
    const parts = [r.source && r.source.kind === "trips" ? "Trips file" : "Report"];
    const algorithm = r.run.dispatcher_label || r.run.dispatcher;
    if (algorithm) parts.push(algorithm);
    if (r.run.description) parts.push(r.run.description);
    parts.push(`${fmt.int(r.passengers.measured)} people`);
    const when = new Date(entry.importedAt);
    if (!Number.isNaN(when.getTime())) {
      parts.push(`imported ${when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`);
    }
    return parts.join(", ");
  }

  // ---- storage (this browser only) -----------------------------------------------------------------
  load() {
    try {
      const list = JSON.parse(localStorage.getItem(IMPORT_STORE) || "[]");
      return Array.isArray(list) ? list.filter((e) => e && e.report && e.report.passengers) : [];
    } catch {
      return [];
    }
  }

  save() {
    // Reports are fairly large; if the browser's storage is full, keep fewer.
    for (let keep = this.recent.length; keep >= 0; keep--) {
      try {
        localStorage.setItem(IMPORT_STORE, JSON.stringify(this.recent.slice(0, keep)));
        return;
      } catch { /* try again with fewer */ }
    }
  }

  setMessage(text, isError = false) {
    this.msg.textContent = text;
    this.msg.classList.toggle("is-error", isError);
  }
}

window.importTab = new ImportTab();
