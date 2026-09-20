/* SB Scheduler Card — v0.4.0 (edit-only)
 *
 * Edits existing sb_scheduler schedules: name, day-set, and time pattern.
 * Creating schedules and editing actions are deliberately out of v1 — they are
 * most of the work, and `sb_scheduler.create_schedule` already covers creation.
 *
 * Needs no websocket API: schedules are read from their switch entities'
 * attributes and written back through `sb_scheduler.edit_schedule`.
 */

const CARD = "sb-scheduler-card";
const VERSION = "0.4.0";

// "sunset", "sunset+00:15:00", "sunrise-01:30" — must survive a round-trip
// through the editor, which is why these get a text field and not <input type=time>.
const SUN = /^(sunrise|sunset)(\s*[+-]\s*\d{1,2}:\d{2}(:\d{2})?)?$/i;
const isSun = (v) => SUN.test(String(v ?? "").trim());

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// "2026-09-21T06:30:00-05:00" -> "Mon 21 Sep, 06:30"
const prettyTrigger = (iso) => {
  if (!iso) return "not scheduled";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit",
  });
};

class SbSchedulerCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(`${CARD}-editor`);
  }
  static getStubConfig() {
    return { title: "Schedules" };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._open = null;   // schedule_id being edited
    this._draft = null;  // local edit state; NEVER overwritten from hass
    this._sig = null;
  }

  setConfig(config) {
    this._config = { title: "Schedules", ...(config || {}) };
    this._sig = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    // While the editor is open, re-rendering would discard half-typed input.
    if (this._open) return;
    const sig = this._signature();
    if (sig !== this._sig) {
      this._sig = sig;
      this._render();
    }
  }

  getCardSize() {
    return 3 + this._schedules().length;
  }

  // --- data ---------------------------------------------------------------
  _schedules() {
    const states = this._hass?.states || {};
    return Object.keys(states)
      .filter((id) => id.startsWith("switch.") && states[id].attributes?.schedule_id)
      .map((id) => ({ entity_id: id, ...states[id].attributes, state: states[id].state }))
      .sort((a, b) => String(a.friendly_name).localeCompare(String(b.friendly_name)));
  }

  _daySets() {
    const states = this._hass?.states || {};
    return Object.keys(states)
      .filter((id) => id.startsWith("calendar.") && states[id].attributes?.day_set_id)
      .map((id) => ({
        id: states[id].attributes.day_set_id,
        name: states[id].attributes.friendly_name || states[id].attributes.day_set_id,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  _signature() {
    return this._schedules()
      .map((s) => `${s.schedule_id}|${s.state}|${s.day_set}|${s.next_trigger}|${s.last_triggered}|${JSON.stringify(s.pattern)}|${s.friendly_name}`)
      .join("~");
  }

  // --- editing ------------------------------------------------------------
  _beginEdit(scheduleId) {
    const s = this._schedules().find((x) => x.schedule_id === scheduleId);
    if (!s) return;
    const pattern = s.pattern || {};
    this._open = scheduleId;
    this._draft = {
      entity_id: s.entity_id,
      name: s.friendly_name || "",
      day_set: s.day_set || "daily",
      type: pattern.type === "interval" ? "interval" : "occurrences",
      occurrences: (pattern.occurrences || []).map((t) =>
        isSun(t) ? String(t).trim() : String(t).slice(0, 5)),
      start: String(pattern.start || "09:00").slice(0, 5),
      stop: String(pattern.stop || "17:00").slice(0, 5),
      every_minutes: Number(pattern.every_minutes || 15),
      error: null,
    };
    if (!this._draft.occurrences.length) this._draft.occurrences = ["06:30"];
    this._render();
  }

  _cancel() {
    this._open = null;
    this._draft = null;
    this._sig = null;
    this._render();
  }

  _validate(d) {
    const timeOk = (t) => /^\d{1,2}:\d{2}$/.test(t) &&
      Number(t.split(":")[0]) < 24 && Number(t.split(":")[1]) < 60;
    if (!d.name.trim()) return "Name cannot be empty.";
    if (d.type === "occurrences") {
      const times = d.occurrences.map((t) => t.trim()).filter(Boolean);
      if (!times.length) return "Add at least one time.";
      const bad = times.filter((t) => !timeOk(t) && !isSun(t));
      if (bad.length) {
        return `Not a valid time: ${bad.join(", ")}. Use HH:MM, or sunrise/sunset ` +
               `with an optional offset such as sunset+00:15.`;
      }
    } else {
      if (!timeOk(d.start) || !timeOk(d.stop)) return "Start and stop must be times.";
      if (d.stop <= d.start) return "Stop must be after start.";
      if (!(d.every_minutes >= 1)) return "Repeat every … must be at least 1 minute.";
    }
    return null;
  }

  async _save() {
    const d = this._draft;
    const error = this._validate(d);
    if (error) {
      d.error = error;
      this._render();
      return;
    }
    const pattern = d.type === "interval"
      ? { type: "interval", start: d.start, stop: d.stop, every_minutes: Number(d.every_minutes) }
      : { type: "occurrences", occurrences: d.occurrences.map((t) => t.trim()).filter(Boolean) };

    try {
      await this._hass.callService("sb_scheduler", "edit_schedule", {
        schedule_id: this._open,
        name: d.name.trim(),
        day_set: d.day_set,
        pattern,
      });
      this._cancel();
    } catch (err) {
      d.error = `Save failed: ${err?.message || err}`;
      this._render();
    }
  }

  // --- rendering ----------------------------------------------------------
  _render() {
    if (!this.shadowRoot) return;
    if (!this._hass) {
      this.shadowRoot.innerHTML = "";
      return;
    }
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>${
      this._config.title ? `<h1 class="card-header">${esc(this._config.title)}</h1>` : ""
    }<div class="body">${this._open ? this._editorHtml() : this._listHtml()}</div></ha-card>`;
    this._wire();
  }

  _listHtml() {
    const rows = this._schedules();
    if (!rows.length) {
      return `<div class="empty">No schedules yet.<br>
        Create one with the <code>sb_scheduler.create_schedule</code> action —
        this card edits existing schedules.</div>`;
    }
    return rows.map((s) => {
      const pattern = s.pattern || {};
      const summary = pattern.type === "interval"
        ? `every ${pattern.every_minutes} min, ${String(pattern.start).slice(0, 5)}–${String(pattern.stop).slice(0, 5)}`
        : (s.times || []).map((t) => String(t).slice(0, 5)).join(", ");
      const on = s.state !== "off";
      return `<div class="row ${on ? "" : "disabled"}">
        <div class="info">
          <div class="name">${esc(s.friendly_name)}</div>
          <div class="meta">
            <span class="chip">${esc(s.day_set)}</span>
            <span>${esc(summary)}</span>
          </div>
          <div class="last">Last: ${esc(s.last_triggered ? prettyTrigger(s.last_triggered) : "never")}</div>
          <div class="next">Next: ${on ? esc(prettyTrigger(s.next_trigger)) : "\u2014"}</div>
        </div>
        <div class="controls">
          <label class="toggle" title="${on ? "Disable" : "Enable"} this schedule">
            <input type="checkbox" class="enable" data-entity="${esc(s.entity_id)}" ${on ? "checked" : ""}>
            <span></span>
          </label>
          <button class="run" data-entity="${esc(s.entity_id)}"
                  title="Run the actions now, ignoring the day-set">Run now</button>
          <button class="edit" data-id="${esc(s.schedule_id)}">Edit</button>
        </div>
      </div>`;
    }).join("");
  }

  _editorHtml() {
    const d = this._draft;
    const daySets = this._daySets();
    const known = daySets.some((x) => x.id === d.day_set);
    return `
      ${d.error ? `<div class="error">${esc(d.error)}</div>` : ""}
      <label class="field"><span>Name</span>
        <input id="name" type="text" value="${esc(d.name)}"></label>

      <label class="field"><span>Runs on</span>
        <select id="day_set">
          ${daySets.map((x) => `<option value="${esc(x.id)}" ${x.id === d.day_set ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
          ${known ? "" : `<option value="${esc(d.day_set)}" selected>${esc(d.day_set)} (missing)</option>`}
        </select></label>
      ${known ? "" : `<div class="warn">This schedule points at a day-set that no longer exists, so it cannot run.</div>`}

      <div class="field"><span>Times</span>
        <div class="radios">
          <label><input type="radio" name="ptype" value="occurrences" ${d.type === "occurrences" ? "checked" : ""}> At set times</label>
          <label><input type="radio" name="ptype" value="interval" ${d.type === "interval" ? "checked" : ""}> Every N minutes</label>
        </div>
      </div>

      ${d.type === "occurrences" ? `
        <div class="times">
          ${d.occurrences.map((t, i) => `
            <div class="timerow">
              <input class="occ" data-i="${i}" type="${isSun(t) ? "text" : "time"}"
                     value="${esc(t)}" ${isSun(t) ? 'title="Relative to the sun"' : ""}>
              ${d.occurrences.length > 1 ? `<button class="drop" data-i="${i}" title="Remove">✕</button>` : ""}
            </div>`).join("")}
          <button class="add">+ Add a time</button>
          <div class="hint">Times are HH:MM, or sunrise/sunset with an offset —
            e.g. <code>sunset+00:15</code>.</div>
        </div>` : `
        <div class="interval">
          <label class="field inline"><span>From</span><input id="start" type="time" value="${esc(d.start)}"></label>
          <label class="field inline"><span>Until</span><input id="stop" type="time" value="${esc(d.stop)}"></label>
          <label class="field inline"><span>Every (min)</span><input id="every" type="number" min="1" max="720" value="${esc(d.every_minutes)}"></label>
          <div class="count">${this._intervalCount(d)}</div>
        </div>`}

      <div class="actions-note">Actions are not editable here in v1 — use
        <code>sb_scheduler.edit_schedule</code>.</div>

      <div class="buttons">
        <button class="cancel">Cancel</button>
        <button class="save">Save</button>
      </div>`;
  }

  _intervalCount(d) {
    const toMin = (t) => {
      const [h, m] = String(t).split(":").map(Number);
      return h * 60 + m;
    };
    const span = toMin(d.stop) - toMin(d.start);
    const step = Number(d.every_minutes);
    if (!(span >= 0) || !(step >= 1)) return "";
    const n = Math.floor(span / step) + 1;
    return `${n} firing${n === 1 ? "" : "s"} per day`;
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("button.edit").forEach((b) =>
      b.addEventListener("click", () => this._beginEdit(b.dataset.id)));

    // Run now fires the actions immediately, ignoring the day-set. No confirm
    // step: the button is explicit and the consequence is one run of something
    // the schedule does anyway. The Last: line updating is the receipt.
    root.querySelectorAll("button.run").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        b.textContent = "Running\u2026";
        try {
          await this._hass.callService("sb_scheduler", "run_now", {
            entity_id: b.dataset.entity,
          });
        } catch (err) {
          b.textContent = "Failed";
          b.title = String(err?.message || err);
          return;
        }
        // last_triggered changes, which re-renders the list and restores this
        // button. Restore by hand too, in case the service was a no-op.
        setTimeout(() => {
          b.disabled = false;
          b.textContent = "Run now";
        }, 1500);
      }));

    root.querySelectorAll("input.enable").forEach((box) =>
      box.addEventListener("change", () => {
        this._hass.callService("switch", box.checked ? "turn_on" : "turn_off", {
          entity_id: box.dataset.entity,
        });
      }));

    if (!this._open) return;
    const d = this._draft;

    const bind = (sel, key, transform = (v) => v) => {
      const el = root.querySelector(sel);
      if (el) el.addEventListener("input", () => { d[key] = transform(el.value); });
    };
    bind("#name", "name");
    bind("#start", "start");
    bind("#stop", "stop");
    bind("#every", "every_minutes", Number);

    const daySet = root.querySelector("#day_set");
    if (daySet) daySet.addEventListener("change", () => { d.day_set = daySet.value; });

    root.querySelectorAll('input[name="ptype"]').forEach((r) =>
      r.addEventListener("change", () => {
        if (!r.checked) return;
        d.type = r.value;
        d.error = null;
        this._render();
      }));

    root.querySelectorAll("input.occ").forEach((inp) =>
      inp.addEventListener("input", () => { d.occurrences[Number(inp.dataset.i)] = inp.value; }));

    root.querySelectorAll("button.drop").forEach((b) =>
      b.addEventListener("click", () => {
        d.occurrences.splice(Number(b.dataset.i), 1);
        this._render();
      }));

    const add = root.querySelector("button.add");
    if (add) add.addEventListener("click", () => { d.occurrences.push("12:00"); this._render(); });

    // Live firing count while the interval is being edited.
    ["#start", "#stop", "#every"].forEach((sel) => {
      const el = root.querySelector(sel);
      if (el) el.addEventListener("input", () => {
        const out = root.querySelector(".count");
        if (out) out.textContent = this._intervalCount(d);
      });
    });

    root.querySelector("button.cancel")?.addEventListener("click", () => this._cancel());
    root.querySelector("button.save")?.addEventListener("click", () => this._save());
  }
}

const STYLE = `
:host { display: block; }
/* Native select popups ignore the page theme unless told otherwise. */
:host { color-scheme: light dark; }
.card-header { font-size: 1.25rem; padding: 12px 16px 0; margin: 0; }
.body { padding: 8px 16px 16px; }
.empty { color: var(--secondary-text-color); padding: 12px 0; line-height: 1.5; }
.row { display: flex; align-items: center; gap: 12px; padding: 10px 0;
       border-bottom: 1px solid var(--divider-color); flex-wrap: wrap; }
.row:last-of-type { border-bottom: none; }
.row.disabled .name, .row.disabled .meta { opacity: .55; }
/* min-width keeps the controls from crushing the text before the row wraps */
.info { flex: 1 1 180px; min-width: 180px; }
.name { font-weight: 500; }
.meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
        color: var(--secondary-text-color); font-size: .9em; margin-top: 2px; }
.chip { background: var(--primary-color); color: var(--text-primary-color);
        border-radius: 10px; padding: 1px 8px; font-size: .85em; }
.last { color: var(--secondary-text-color); font-size: .85em; margin-top: 2px; }
.next { color: var(--secondary-text-color); font-size: .85em; }
.controls { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; }
.controls button { white-space: nowrap; }
.controls button[disabled] { opacity: .6; cursor: default; }
.toggle { position: relative; display: inline-block; width: 40px; height: 22px; flex: 0 0 auto; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle span { position: absolute; inset: 0; cursor: pointer; border-radius: 22px;
               background: var(--disabled-text-color, #9e9e9e); transition: background .2s; }
.toggle span::before { content: ""; position: absolute; width: 16px; height: 16px;
                       left: 3px; top: 3px; border-radius: 50%; background: #fff;
                       transition: transform .2s; }
.toggle input:checked + span { background: var(--primary-color); }
.toggle input:checked + span::before { transform: translateX(18px); }
.toggle input:focus-visible + span { outline: 2px solid var(--primary-color); outline-offset: 2px; }
button { cursor: pointer; border-radius: 6px; border: 1px solid var(--divider-color);
         background: var(--card-background-color); color: var(--primary-text-color);
         padding: 6px 12px; font: inherit; }
button.save { background: var(--primary-color); color: var(--text-primary-color);
              border-color: transparent; }
button.drop { border: none; background: none; color: var(--error-color); padding: 4px 8px; }
.field { display: flex; flex-direction: column; gap: 4px; margin: 12px 0; }
.field > span { color: var(--secondary-text-color); font-size: .85em; }
.field.inline { flex: 1; }
input, select { font: inherit; padding: 8px; border-radius: 6px;
                border: 1px solid var(--divider-color);
                background: var(--card-background-color); color: var(--primary-text-color); }
option { background: var(--card-background-color); color: var(--primary-text-color); }
.radios { display: flex; gap: 16px; flex-wrap: wrap; }
.radios label { display: flex; align-items: center; gap: 6px; }
.times { display: flex; flex-direction: column; gap: 6px; }
.timerow { display: flex; align-items: center; gap: 6px; }
.interval { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }
.count { color: var(--secondary-text-color); font-size: .85em; padding-bottom: 10px; }
.hint { color: var(--secondary-text-color); font-size: .8em; margin-top: 4px; }
.buttons { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.error { background: var(--error-color); color: var(--text-primary-color);
         padding: 8px 12px; border-radius: 6px; margin-bottom: 8px; }
.warn { color: var(--warning-color); font-size: .85em; margin: -6px 0 8px; }
.actions-note { color: var(--secondary-text-color); font-size: .8em; margin-top: 16px; }
code { background: var(--secondary-background-color); padding: 1px 4px; border-radius: 3px; }
`;

// --- config editor ---------------------------------------------------------
class SbSchedulerCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._render();
  }
  set hass(hass) {
    this._hass = hass;
  }
  _render() {
    if (this._built) return;
    this._built = true;
    this.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:4px;padding:8px 0">
        <span style="font-size:.85em;color:var(--secondary-text-color)">Title</span>
        <input id="t" type="text" style="font:inherit;padding:8px;border-radius:6px;
          border:1px solid var(--divider-color);background:var(--card-background-color);
          color:var(--primary-text-color)" value="${esc(this._config.title || "")}">
      </div>`;
    this.querySelector("#t").addEventListener("input", (e) => {
      this._config = { ...this._config, title: e.target.value };
      this.dispatchEvent(new CustomEvent("config-changed", {
        detail: { config: this._config }, bubbles: true, composed: true,
      }));
    });
  }
}

customElements.define(CARD, SbSchedulerCard);
customElements.define(`${CARD}-editor`, SbSchedulerCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: CARD,
  name: "SB Scheduler Card",
  description: "Edit sb_scheduler schedules: day-set and time pattern.",
  preview: false,
  documentationURL: "https://github.com/snadboy/sb-scheduler",
});

console.info(`%c ${CARD.toUpperCase()} %c v${VERSION} `,
  "color:white;background:#3f51b5;font-weight:700",
  "color:#3f51b5;background:white;font-weight:700");
