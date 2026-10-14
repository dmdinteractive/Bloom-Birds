/* Kinetic Motion Control — editor front end (vanilla JS, no build step).
 *
 * Model
 *   S.show      the active show (clips + playlist), edited in place and auto-saved
 *   S.tick      latest 20 Hz snapshot from the engine (positions, nodes, mode)
 *   S.render    sampled motion of the selected clip, computed by the Pi (same math the motors get)
 */
"use strict";

const S = {
  config: null, show: null, shows: [], patterns: {}, common: [], eases: [], spm: 0, simulate: false,
  tick: null, ws: null, wsOk: false,
  selClip: null, selMotor: 0, selKey: null, render: null, renderFor: null, highlight: -1,
  events: [], lastWarn: null, layoutMotors: null, layoutDrag: null,
};
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
const newId = () => Math.random().toString(16).slice(2, 10);
const fmtTime = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`; };
const fmtTimeTenths = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`; };
const led = (state, title = "") => `<span class="led ${state || ""}" ${title ? `title="${esc(title)}"` : ""}></span>`;

// Scope palette: neutral traces, one highlight, alarms in red.
const SCOPE = { bg: "#121619", grid: "#232a31", gridMajor: "#313a43", axis: "#8b949c", trace: "rgba(170,182,194,0.55)",
  traceDim: "rgba(170,182,194,0.28)", hi: "#f2c200", key: "#ffffff", keySel: "#f2c200", play: "#39b54a", limit: "#e53935" };

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, warn = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (warn ? " warn" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), 2600);
}
function send(cmd, extra = {}) {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ cmd, ...extra }));
  else toast("Not connected to the controller", true);
}

// ------------------------------------------------------------ boot
async function loadState() {
  const st = await api("GET", "/api/state");
  S.config = st.config; S.show = st.show; S.shows = st.shows; S.patterns = st.patterns;
  S.common = st.common_params; S.eases = st.eases; S.spm = st.steps_per_mm; S.simulate = st.simulate;
  S.kinds = st.kinds || {};
  S.modParams = st.mod_params || []; S.blends = st.blends || {};
  $$(".sim-only").forEach((b) => (b.hidden = !S.simulate));
  const mem = st.config.mechanics.home_mode === "memory";
  $('[data-all="set_zero"]').hidden = mem;          // in memory mode the top is taught in Motor Setup
  $('[data-all="home"]').textContent = mem ? "All To Top" : "Home All";
  S.layoutMotors = JSON.parse(JSON.stringify(S.config.motors));
  $("#motorCount").textContent = S.config.motors.length;
  if (!S.selClip || !clipById(S.selClip)) S.selClip = S.show.clips[0]?.id ?? null;
  S.selKey = null;
  if (S.highlight >= nMotors()) S.highlight = -1;
  renderShowSelect(); renderPlaylist(); renderClipList(); renderEditor(); renderTraceSel(); requestRender();
  renderSettings();
}

function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { S.wsOk = true; };
  ws.onclose = () => { S.wsOk = false; setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "tick") {
      const prevClip = S.tick?.clip, prevMode = S.tick?.mode, prevEntry = S.tick?.entry;
      S.tick = m;
      if (m.events) addEvents(m.events);
      // motors added or removed (here or on another screen): reload the motor list once
      if (S.config && m.travel && m.travel.length !== S.config.motors.length && !S.reloading) {
        S.reloading = true; loadState().finally(() => { S.reloading = false; });
      }
      if (m.clip !== prevClip || m.mode !== prevMode || m.entry !== prevEntry) markPlaying();
    } else if (m.type === "events") addEvents(m.events);
    else if (m.type === "error") toast(m.msg, true);
  };
}

function addEvents(evs) {
  for (const e of evs) {
    S.events.push(e);
    if (e.level === "warn") {
      S.lastWarn = e;
      if (Date.now() / 1000 - e.t < 5) toast(e.msg, true);
    }
  }
  S.events = S.events.slice(-120);
  $("#events").innerHTML = S.events.slice().reverse().map((e) =>
    `<tr class="${e.level}"><td>${new Date(e.t * 1000).toLocaleTimeString()}</td><td>${e.level === "warn" ? "Warning" : "Info"}</td><td>${esc(e.msg)}</td></tr>`).join("");
}

// ------------------------------------------------------------ show helpers
const clipById = (id) => S.show?.clips.find((c) => c.id === id);
const nMotors = () => S.config.motors.length;
const typeName = (c) => (c?.type === "timeline" ? "Timeline" : c?.type === "layers" ? "Layered" : "Pattern");
const saveShowSoon = debounce(async () => {
  try { await api("PUT", "/api/show", S.show); } catch (e) { toast("Save failed: " + e.message, true); }
}, 250);
function showChanged({ structure = false } = {}) {
  saveShowSoon();
  requestRender();
  if (structure) { renderPlaylist(); renderClipList(); }
}

function patternDefaults(name) {
  const p = {};
  for (const d of S.common) p[d.key] = d.default;
  for (const d of S.patterns[name]?.params || []) p[d.key] = d.default;
  if (name === "pose") p.values = Array.from({ length: nMotors() }, () => 0.5);
  for (const d of S.patterns[name]?.params || []) if (d.type === "list") p[d.key] = [...d.default];
  return p;
}
function addClip(type) {
  if (type === "layers") {
    const subs = S.show.clips.filter((c) => c.type !== "layers").slice(0, 2);
    const clip = { id: newId(), name: "New layered", type: "layers", duration: 60, fade: 4, base: 0,
      layers: subs.map((c, k) => ({ clip: c.id, target: "", blend: k ? "add" : "over", mix: 1, offset: 0, enabled: true })) };
    S.show.clips.push(clip); S.selClip = clip.id; S.selKey = null;
    showChanged({ structure: true }); renderEditor(); return;
  }
  const clip = type === "pattern"
    ? { id: newId(), name: "New pattern", type: "pattern", pattern: "wave", duration: 60, fade: 4, params: patternDefaults("wave") }
    : { id: newId(), name: "New timeline", type: "timeline", duration: 20, fade: 3, mode: "shared", stagger: 0,
        stagger_by: "index", stagger_reverse: false, wrap: true,
        tracks: { all: [[0, 0.2, "smooth"], [10, 0.8, "smooth"]] } };
  S.show.clips.push(clip);
  S.selClip = clip.id; S.selKey = null;
  showChanged({ structure: true }); renderEditor();
}

// ------------------------------------------------------------ left column
function renderShowSelect() {
  $("#showSelect").innerHTML = S.shows.map((n) => `<option ${n === S.show.name ? "selected" : ""}>${esc(n)}</option>`).join("");
  $("#showName").textContent = S.show.name;
}
function clipLength(e) { const c = clipById(e.clip); return (c?.duration || 0) * Math.max(1, e.repeat || 1); }

function renderPlaylist() {
  const ol = $("#playlist");
  const pl = S.show.playlist;
  $("#playlistTotal").textContent = pl.length ? `cycle ${fmtTime(pl.reduce((a, e) => a + clipLength(e), 0))}` : "";
  if (!pl.length) { ol.innerHTML = `<li class="empty">Empty. Select a clip and choose Add to Playlist.</li>`; return; }
  ol.innerHTML = pl.map((e, i) => {
    const c = clipById(e.clip);
    const off = e.enabled === false;
    return `<li data-i="${i}" class="${e.clip === S.selClip ? "sel" : ""} ${off ? "off" : ""}">
      <span class="dim">${i + 1}</span>
      <span class="name" title="${off ? "Switched off on the operator panel" : esc(typeName(c))}">${esc(c?.name || "?")}${off ? " (off)" : ""}</span>
      <input class="rep" type="number" min="1" max="99" value="${e.repeat || 1}" data-rep="${i}" aria-label="Repeat count" title="Repeat count">
      <span class="dim">${fmtTime(clipLength(e))}</span>
      <span class="ctl">
        <button class="btn icon" data-up="${i}" aria-label="Move up" title="Move up">▲</button>
        <button class="btn icon" data-down="${i}" aria-label="Move down" title="Move down">▼</button>
        <button class="btn icon" data-del="${i}" aria-label="Remove from playlist" title="Remove">✕</button>
      </span></li>`;
  }).join("");
  markPlaying();
}
$("#playlist").addEventListener("click", (ev) => {
  const t = ev.target, pl = S.show.playlist;
  const idx = (k) => parseInt(t.dataset[k], 10);
  if (t.dataset.up !== undefined) { const i = idx("up"); if (i > 0) [pl[i - 1], pl[i]] = [pl[i], pl[i - 1]]; }
  else if (t.dataset.down !== undefined) { const i = idx("down"); if (i < pl.length - 1) [pl[i + 1], pl[i]] = [pl[i], pl[i + 1]]; }
  else if (t.dataset.del !== undefined) pl.splice(idx("del"), 1);
  else if (t.closest("li[data-i]") && t.dataset.rep === undefined) { selectClip(pl[+t.closest("li").dataset.i].clip); return; }
  else return;
  showChanged({ structure: true });
});
$("#playlist").addEventListener("change", (ev) => {
  if (ev.target.dataset.rep === undefined) return;
  S.show.playlist[+ev.target.dataset.rep].repeat = clamp(parseInt(ev.target.value, 10) || 1, 1, 99);
  showChanged({ structure: true });
});

function renderClipList() {
  const ul = $("#clipList");
  if (!S.show.clips.length) { ul.innerHTML = `<li class="empty">No clips. Create a pattern or timeline.</li>`; return; }
  ul.innerHTML = S.show.clips.map((c) => `<li data-id="${c.id}" class="${c.id === S.selClip ? "sel" : ""}">
      <span class="name">${esc(c.name)}</span><span class="dim">${typeName(c)}</span><span class="dim">${fmtTime(c.duration)}</span></li>`).join("");
  markPlaying();
}
function selectClip(id) {
  S.selClip = id; S.selKey = null; S.selMotor = 0;
  renderClipList(); renderPlaylist(); renderEditor(); requestRender();
}
$("#clipList").addEventListener("click", (ev) => {
  const li = ev.target.closest("li[data-id]"); if (li) selectClip(li.dataset.id);
});
function markPlaying() {
  const playingClip = S.tick?.mode === "playlist" || S.tick?.mode === "solo" ? S.tick.clip : null;
  $$("#clipList li[data-id]").forEach((li) => li.classList.toggle("playing", li.dataset.id === playingClip));
  $$("#playlist li[data-i]").forEach((li) => li.classList.toggle("playing",
    S.tick?.mode === "playlist" && +li.dataset.i === (S.tick.entry % Math.max(1, S.show.playlist.length))));
}

$("#btnAddPattern").onclick = () => addClip("pattern");
$("#btnAddTimeline").onclick = () => addClip("timeline");
$("#btnAddLayers").onclick = () => addClip("layers");
$("#btnSaveAs").onclick = async () => {
  const name = prompt("Save show as:", S.show.name); if (!name) return;
  const r = await api("POST", "/api/show/saveas", { name });
  S.shows = r.shows; S.show = r.show; renderShowSelect(); toast(`Saved "${name}"`);
};
$("#showSelect").onchange = async (ev) => {
  const r = await api("POST", "/api/show/load", { name: ev.target.value });
  S.show = r.show; S.selClip = S.show.clips[0]?.id ?? null; S.selKey = null;
  renderShowSelect(); renderPlaylist(); renderClipList(); renderEditor(); requestRender(); toast(`Loaded "${S.show.name}"`);
};

// ------------------------------------------------------------ property grid helpers
const UNITS = { period: "s", direction: "°", duration: "s", fade: "s", stagger: "s" };
function fmtNum(v, step) { const dec = step >= 1 ? 0 : step >= 0.1 ? 1 : 2; return Number(v).toFixed(dec); }
const sec = (title) => `<div class="p-sec">${esc(title)}</div>`;
const prop = (label, inner, help = "") =>
  `<div class="p-lab">${label}</div><div class="p-val">${inner}</div>${help ? `<div class="p-help">${esc(help)}</div>` : ""}`;
/** Slider + numeric box bound to the same value. scope: p (pattern param), pose, c (clip field). */
function sliderProp(key, d, value, scope = "p") {
  const v = value ?? d.default;
  const unit = d.unit ?? UNITS[key] ?? "";
  return prop(`<label for="f_${scope}_${key}">${esc(d.label.replace(/ \((s|deg)\)$/, ""))}</label>`,
    `<input type="range" id="f_${scope}_${key}" data-${scope}="${key}" min="${d.min}" max="${d.max}" step="${d.step}" value="${v}">
     <input type="number" class="num" id="n_${scope}_${key}" data-${scope}="${key}" min="${d.min}" max="${d.max}" step="${d.step}" value="${fmtNum(v, d.step)}" aria-label="${esc(d.label)}">
     <span class="unit">${unit}</span>`, d.help);
}
function syncPair(t) {
  const other = t.type === "range" ? $(`#${t.id.replace(/^f_/, "n_")}`) : $(`#${t.id.replace(/^n_/, "f_")}`);
  if (other && other !== document.activeElement) other.value = t.type === "range" ? fmtNum(t.value, parseFloat(t.step)) : t.value;
}

// ------------------------------------------------------------ clip editor
function renderEditor() {
  const ed = $("#editor");
  const c = clipById(S.selClip);
  $("#editorTitle").textContent = c ? `Clip Properties — ${typeName(c)}` : "Clip Properties";
  if (!c) { ed.innerHTML = `<div class="editor-empty">No clip selected. Create a pattern or timeline in the Clip Library.</div>`; return; }
  let h = `<div class="props">${sec("General")}
    ${prop(`<label for="c_name">Name</label>`, `<input type="text" id="c_name" data-c="name" value="${esc(c.name)}">`)}
    ${prop(`<label for="c_dur">Length</label>`, `<input type="number" class="num" id="c_dur" data-c="duration" min="1" max="3600" step="1" value="${c.duration}"><span class="unit">s</span>`)}
    ${prop(`<label for="c_fade">Fade in</label>`, `<input type="number" class="num" id="c_fade" data-c="fade" min="0" max="60" step="0.5" value="${c.fade ?? 3}"><span class="unit">s</span>`)}`;
  if (c.type === "pattern") {
    h += sec("Pattern") + prop(`<label for="c_pat">Generator</label>`, `<select id="c_pat" data-c="pattern">${Object.entries(S.patterns).map(([k, p]) =>
      `<option value="${k}" ${k === c.pattern ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</select>`);
    if (c.pattern === "pose") {
      h += sec("Axis positions (0 = home, 1 = far end; continuous: speed)");
      const vals = c.params.values || [];
      S.config.motors.forEach((m, i) => { h += sliderProp(i, { label: m.name, min: 0, max: 1, step: 0.01, default: 0.5, unit: "" }, vals[i], "pose"); });
    } else {
      h += sec("Motion");
      const common = S.patterns[c.pattern]?.common;
      for (const d of S.common) if (!common || common.includes(d.key)) h += sliderProp(d.key, d, c.params[d.key]);
      const extra = S.patterns[c.pattern]?.params || [];
      if (extra.length) h += sec("Shape");
      for (const d of extra) {
        if (d.type === "bool") h += prop(`<label for="pb_${d.key}">${esc(d.label)}</label>`, `<input type="checkbox" id="pb_${d.key}" data-pb="${d.key}" ${c.params[d.key] ? "checked" : ""}>`);
        else if (d.type === "list") {
          const cnt = Math.round(c.params[d.count_key] ?? 4), vals = c.params[d.key] || d.default;
          for (let k = 0; k < cnt; k++) h += sliderProp(k, { label: `${d.label} ${k + 1}`, min: 0, max: 1, step: 0.01, default: 0.5, unit: "" }, vals[k] ?? 0.5, "lv");
        } else h += sliderProp(d.key, d, c.params[d.key]);
      }
    }
  } else if (c.type === "layers") {
    h += layersHtml(c);
  } else {
    const own = (i) => (c.tracks[String(i)] || []).length > 0;
    h += sec("Curves") + prop(`<label for="c_mode">Mode</label>`, `<select id="c_mode" data-c="mode">
        <option value="shared" ${c.mode !== "per_motor" ? "selected" : ""}>Shared curve (all axes)</option>
        <option value="per_motor" ${c.mode === "per_motor" ? "selected" : ""}>Individual curve per axis</option></select>`);
    if (c.mode === "per_motor") {
      h += `<div class="p-full"><div class="axis-pick">` + S.config.motors.map((m, i) =>
        `<button class="btn ${i === S.selMotor ? "sel" : ""} ${own(i) ? "own" : ""}" data-motor="${i}" title="${own(i) ? "Individual curve" : "Uses shared curve"}">${esc(m.name)}</button>`).join("") + `</div></div>`;
      h += `<div class="p-full">` + (own(S.selMotor)
        ? `<button class="btn small" data-act="copyToAll">Copy to All Axes</button><button class="btn small" data-act="useShared">Revert to Shared</button>`
        : `<button class="btn small" data-act="makeOwn">Create Curve for ${esc(S.config.motors[S.selMotor]?.name)}</button>`) + `</div>`;
    }
    h += sec("Stagger") + sliderProp("stagger", { label: "Spread", min: 0, max: Math.max(1, c.duration), step: 0.1, default: 0,
      help: "Total delay spread across axes. One curve becomes a traveling wave." }, c.stagger, "c");
    h += prop(`<label for="c_sby">Order</label>`, `<select id="c_sby" data-c="stagger_by">
        <option value="index" ${c.stagger_by === "index" ? "selected" : ""}>Axis number</option>
        <option value="x" ${c.stagger_by === "x" ? "selected" : ""}>Left to right</option>
        <option value="center" ${c.stagger_by === "center" ? "selected" : ""}>Center outward</option></select>`);
    h += prop(`<label for="c_rev">Reverse order</label>`, `<input type="checkbox" id="c_rev" data-cb="stagger_reverse" ${c.stagger_reverse ? "checked" : ""}>`);
    h += prop(`<label for="c_wrap">Seamless loop</label>`, `<input type="checkbox" id="c_wrap" data-cb="wrap" ${c.wrap !== false ? "checked" : ""}>`,
      "Last keyframe flows back into the first.");
    h += `</div><div class="props" id="keybox"></div><p class="note">Double-click the profile to add a keyframe. Drag to move (snaps to 0.25 s; hold Alt for free placement). Delete removes the selected key.</p><div>`;
  }
  if (c.type !== "layers") h += `</div><div class="props">` + shapingHtml(c);
  h += `</div><div class="editor-actions">
      <button class="btn default" data-act="solo">Preview Clip</button>
      <button class="btn" data-act="toPlaylist">Add to Playlist</button>
      <button class="btn" data-act="duplicate">Duplicate</button>
      <button class="btn" data-act="delete">Delete</button></div>
      <p class="note" id="soloHint"></p>`;
  ed.innerHTML = h;
  updateKeyBox();
  updateSoloHint();
}
// ---- shaping (clip["mods"]) and targets: shared by pattern and timeline clips
const groupNames = () => (S.config.groups || []).map((g) => g.name).filter(Boolean);
const targetSelect = (val, attrs) => `<select ${attrs}><option value="">All motors</option>${groupNames().map((g) =>
  `<option value="${esc(g)}" ${g === val ? "selected" : ""}>${esc(g)}</option>`).join("")}</select>`;
function restOf(c) {
  if (c.mods?.rest !== undefined && c.mods?.rest !== null) return c.mods.rest;
  if (c.type === "pattern") return c.pattern === "steps" ? 0.5 : (c.params?.center ?? 0.5);
  return 0;
}
function shapingHtml(c) {
  const m = c.mods || {};
  let h = sec("Target & shaping");
  h += prop(`<label for="c_target">Target</label>`, targetSelect(c.target || "", `id="c_target" data-c="target"`),
    groupNames().length ? "Motors outside the target wait at the rest position." : "Make groups under Setup → Motor Groups.");
  for (const d of S.modParams || []) {
    if (d.key === "stagger" && c.type !== "pattern") continue;          // timelines have their own stagger
    const v = d.key === "rest" ? restOf(c) : m[d.key];
    h += sliderProp(d.key, { ...d, default: d.default ?? 0.5 }, v, "m");
  }
  if (c.type === "pattern") {
    h += prop(`<label for="m_sby">Stagger order</label>`, `<select id="m_sby" data-mc="stagger_by">
      <option value="index" ${m.stagger_by !== "x" && m.stagger_by !== "center" ? "selected" : ""}>Axis number</option>
      <option value="x" ${m.stagger_by === "x" ? "selected" : ""}>Left to right</option>
      <option value="center" ${m.stagger_by === "center" ? "selected" : ""}>Center outward</option></select>`);
  }
  h += prop(`<label for="m_inv">Invert</label>`, `<input type="checkbox" id="m_inv" data-mb="invert" ${m.invert ? "checked" : ""}>`,
    "Swap home and far end: up becomes down");
  h += prop(`<label for="m_mir">Mirror left/right</label>`, `<input type="checkbox" id="m_mir" data-mb="mirror" ${m.mirror ? "checked" : ""}>`,
    "Spatial patterns run symmetrically from the center line, like a pair of wings");
  return h;
}
function layersHtml(c) {
  const subs = S.show.clips.filter((x) => x.type !== "layers");
  let h = sec("Layers") + sliderProp("base", { label: "Base position", min: 0, max: 1, step: 0.01, default: 0,
    help: "Where motors sit before any layer moves them (0 = home)" }, c.base ?? 0, "c");
  h += `<div class="p-full"><div class="layers">` + (c.layers || []).map((L, i) => `
    <div class="layer ${L.enabled === false ? "off" : ""}">
      <div class="layer-head"><input type="checkbox" data-ly="enabled" data-li="${i}" ${L.enabled === false ? "" : "checked"} title="Layer on/off">
        <b>Layer ${i + 1}</b>
        <select data-ly="clip" data-li="${i}" aria-label="Clip">${subs.map((x) => `<option value="${x.id}" ${x.id === L.clip ? "selected" : ""}>${esc(x.name)}</option>`).join("")}
          ${subs.some((x) => x.id === L.clip) ? "" : `<option value="" selected>(missing clip)</option>`}</select>
        <span class="spacer"></span>
        <button class="btn small" data-lact="up" data-li="${i}" ${i === 0 ? "disabled" : ""} aria-label="Move up">▲</button>
        <button class="btn small" data-lact="down" data-li="${i}" ${i === c.layers.length - 1 ? "disabled" : ""} aria-label="Move down">▼</button>
        <button class="btn small" data-lact="del" data-li="${i}" aria-label="Remove layer">✕</button></div>
      <div class="layer-row">
        <label>Target ${targetSelect(L.target || "", `data-ly="target" data-li="${i}"`)}</label>
        <label>Blend <select data-ly="blend" data-li="${i}">${Object.entries(S.blends || {}).map(([k, v]) =>
          `<option value="${k}" ${k === (L.blend || "over") ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label>Mix <input type="range" min="0" max="1" step="0.01" value="${L.mix ?? 1}" data-ly="mix" data-li="${i}"><span class="mono">${Math.round((L.mix ?? 1) * 100)}%</span></label>
        <label>Offset <input type="number" class="num" step="0.1" value="${L.offset ?? 0}" data-ly="offset" data-li="${i}"><span class="unit">s</span></label>
      </div></div>`).join("") + `</div>
    <button class="btn small" data-lact="add">Add Layer</button></div>`;
  h += `<p class="note p-full">Layers apply top to bottom. <b>Over</b> replaces what's below, <b>Add</b> puts a clip's movement on top
    (e.g. a fast flutter over a slow rise), <b>Scale</b> uses a clip as a volume control toward home, <b>Highest/Lowest</b> pick
    per motor. Each layer can drive just one group. Layered clips use clips from the library; edit those to change a layer.</p>`;
  return h;
}

function updateSoloHint() {
  const el = $("#soloHint"); if (!el) return;
  el.textContent = S.tick?.armed ? "Axes enabled: preview drives the sculpture." : "Axes disabled: preview is on screen only.";
}

$("#editor").addEventListener("input", (ev) => {
  const t = ev.target, c = clipById(S.selClip); if (!c) return;
  const num = (lo, hi) => clamp(parseFloat(t.value), lo, hi);
  if (t.dataset.p !== undefined) {
    if (t.value === "" || isNaN(parseFloat(t.value))) return;
    c.params[t.dataset.p] = num(parseFloat(t.min), parseFloat(t.max)); syncPair(t); showChanged();
  } else if (t.dataset.pose !== undefined) {
    if (t.value === "") return;
    const i = +t.dataset.pose; c.params.values = c.params.values || [];
    while (c.params.values.length < nMotors()) c.params.values.push(0.5);
    c.params.values[i] = num(0, 1); syncPair(t); showChanged();
  } else if (t.dataset.c === "stagger") {
    if (t.value === "") return;
    c.stagger = num(0, 3600); syncPair(t); showChanged();
  } else if (t.dataset.c === "base") {
    if (t.value === "") return;
    c.base = num(0, 1); syncPair(t); showChanged();
  } else if (t.dataset.m !== undefined) {
    if (t.value === "" || isNaN(parseFloat(t.value))) return;
    c.mods = c.mods || {}; c.mods[t.dataset.m] = num(parseFloat(t.min), parseFloat(t.max)); syncPair(t); showChanged();
  } else if (t.dataset.lv !== undefined) {
    if (t.value === "") return;
    const k = +t.dataset.lv; c.params.levels = [...(c.params.levels || S.patterns.steps.params.find((d) => d.key === "levels").default)];
    c.params.levels[k] = num(0, 1); syncPair(t); showChanged();
  } else if (t.dataset.ly === "mix") {
    const L = c.layers[+t.dataset.li]; L.mix = num(0, 1);
    t.nextElementSibling.textContent = `${Math.round(L.mix * 100)}%`; showChanged();
  } else if (t.dataset.c === "name") {
    c.name = t.value; $("#editorTitle").textContent = `Clip Properties — ${typeName(c)}`; showChanged({ structure: true });
  } else if (t.dataset.k !== undefined && t.dataset.k !== "ease") {
    editKeyField(t);
  }
});
$("#editor").addEventListener("change", (ev) => {
  const t = ev.target, c = clipById(S.selClip); if (!c) return;
  const k = t.dataset.c;
  if (k === "duration") { c.duration = clamp(parseFloat(t.value) || 1, 1, 3600); showChanged({ structure: true }); renderEditor(); }
  else if (k === "fade") { c.fade = clamp(parseFloat(t.value) || 0, 0, 60); showChanged(); }
  else if (k === "pattern") { c.pattern = t.value; c.params = { ...patternDefaults(t.value), ...c.params }; if (t.value === "pose" && !c.params.values) c.params.values = patternDefaults("pose").values; showChanged(); renderEditor(); }
  else if (k === "mode" || k === "stagger_by") { c[k] = t.value; showChanged(); renderEditor(); }
  else if (t.dataset.pb) { c.params[t.dataset.pb] = t.checked; showChanged(); }
  else if (k === "target") { c.target = t.value || undefined; showChanged(); }
  else if (t.dataset.mb) { c.mods = c.mods || {}; c.mods[t.dataset.mb] = t.checked; showChanged(); }
  else if (t.dataset.mc) { c.mods = c.mods || {}; c.mods[t.dataset.mc] = t.value; showChanged(); }
  else if (t.dataset.ly && t.dataset.ly !== "mix") {
    const L = c.layers[+t.dataset.li], f = t.dataset.ly;
    if (f === "enabled") L.enabled = t.checked;
    else if (f === "offset") L.offset = parseFloat(t.value) || 0;
    else if (f === "target") L.target = t.value || undefined;
    else L[f] = t.value;
    showChanged(); if (f === "enabled") renderEditor();
  }
  else if (t.dataset.cb) { c[t.dataset.cb] = t.checked; showChanged(); }
  else if (t.dataset.k === "ease") { if (S.selKey) { S.selKey[2] = t.value; showChanged(); } }
  else if (t.dataset.p === "count") { renderEditor(); }
  else if (t.type === "number" && (t.dataset.p !== undefined || t.dataset.pose !== undefined || t.dataset.c === "stagger")) {
    t.value = fmtNum(clamp(parseFloat(t.value) || 0, parseFloat(t.min), parseFloat(t.max)), parseFloat(t.step));   // tidy on commit
  }
});
$("#editor").addEventListener("click", (ev) => {
  const t = ev.target.closest("button"), c = clipById(S.selClip); if (!c || !t) return;
  if (t.dataset.motor !== undefined) { S.selMotor = +t.dataset.motor; S.selKey = null; renderEditor(); return; }
  if (t.dataset.lact) {
    const i = +t.dataset.li, a = t.dataset.lact, Ls = c.layers;
    if (a === "add") {
      const first = S.show.clips.find((x) => x.type !== "layers");
      if (!first) { toast("Make a pattern or timeline clip first", true); return; }
      Ls.push({ clip: first.id, target: "", blend: Ls.length ? "add" : "over", mix: 1, offset: 0, enabled: true });
    } else if (a === "del") Ls.splice(i, 1);
    else if (a === "up" && i > 0) [Ls[i - 1], Ls[i]] = [Ls[i], Ls[i - 1]];
    else if (a === "down" && i < Ls.length - 1) [Ls[i + 1], Ls[i]] = [Ls[i], Ls[i + 1]];
    showChanged(); renderEditor(); return;
  }
  const act = t.dataset.act; if (!act) return;
  if (act === "solo") { send("solo", { clip: c.id }); toast(`Previewing "${c.name}"` + (S.tick?.armed ? " on the sculpture" : "")); }
  else if (act === "toPlaylist") { S.show.playlist.push({ clip: c.id, repeat: 1 }); showChanged({ structure: true }); toast("Added to playlist"); }
  else if (act === "duplicate") {
    const d = JSON.parse(JSON.stringify(c)); d.id = newId(); d.name = c.name + " copy";
    S.show.clips.splice(S.show.clips.indexOf(c) + 1, 0, d); S.selClip = d.id; showChanged({ structure: true }); renderEditor();
  } else if (act === "delete") {
    const usedBy = S.show.clips.filter((x) => x.type === "layers" && (x.layers || []).some((L) => L.clip === c.id)).map((x) => x.name);
    if (!confirm(`Delete "${c.name}"? It will also be removed from the playlist.` + (usedBy.length ? `\n\nIt is a layer in: ${usedBy.join(", ")}.` : ""))) return;
    S.show.clips = S.show.clips.filter((x) => x.id !== c.id);
    S.show.playlist = S.show.playlist.filter((e) => e.clip !== c.id);
    S.selClip = S.show.clips[0]?.id ?? null; showChanged({ structure: true }); renderEditor();
  } else if (act === "makeOwn") { c.tracks[String(S.selMotor)] = JSON.parse(JSON.stringify(c.tracks.all || [])); showChanged(); renderEditor(); }
  else if (act === "useShared") { delete c.tracks[String(S.selMotor)]; S.selKey = null; showChanged(); renderEditor(); }
  else if (act === "copyToAll") {
    const src = c.tracks[String(S.selMotor)];
    c.tracks.all = JSON.parse(JSON.stringify(src));
    for (let i = 0; i < nMotors(); i++) if (i !== S.selMotor) delete c.tracks[String(i)];
    delete c.tracks[String(S.selMotor)]; c.mode = "shared"; S.selKey = null; showChanged(); renderEditor();
  } else if (act === "delKey") deleteSelectedKey();
});

// ---- keyframe helpers (mirror of motion.py) ----
function ease(kind, u) {
  u = clamp(u);
  if (kind === "linear") return u;
  if (kind === "ease_in") return u * u * u;
  if (kind === "ease_out") return 1 - (1 - u) ** 3;
  if (kind === "hold") return 0;
  return u * u * u * (u * (u * 6 - 15) + 10);
}
function evalTrack(keys, t, dur, wrap) {
  if (!keys?.length) return 0;
  let ks = [...keys].sort((a, b) => a[0] - b[0]);
  if (wrap && dur > 0) {
    t = ((t % dur) + dur) % dur;
    const f = ks[0], l = ks[ks.length - 1];
    ks = [[l[0] - dur, l[1], l[2] || "smooth"], ...ks, [f[0] + dur, f[1], f[2] || "smooth"]];
  } else {
    if (t <= ks[0][0]) return ks[0][1];
    if (t >= ks[ks.length - 1][0]) return ks[ks.length - 1][1];
  }
  for (let i = 0; i < ks.length - 1; i++) {
    const a = ks[i], b = ks[i + 1];
    if (a[0] <= t && t <= b[0]) { const span = b[0] - a[0]; const u = span <= 0 ? 0 : (t - a[0]) / span; return a[1] + (b[1] - a[1]) * ease(a[2] || "smooth", u); }
  }
  return ks[ks.length - 1][1];
}
function activeTrack(c) {
  if (c.mode === "per_motor" && (c.tracks[String(S.selMotor)] || []).length) return c.tracks[String(S.selMotor)];
  c.tracks.all = c.tracks.all || [[0, 0.5, "smooth"]];
  return c.tracks.all;
}
const EASE_LABEL = { smooth: "S-curve", linear: "Linear", ease_in: "Ease in", ease_out: "Ease out", hold: "Hold (step)" };
function updateKeyBox() {
  const box = $("#keybox"); if (!box) return;
  const c = clipById(S.selClip);
  const tr = activeTrack(c);
  if (!S.selKey || !tr.includes(S.selKey)) {
    S.selKey = null;
    box.innerHTML = sec("Keyframe") + `<div class="p-full"><span class="note" style="margin:0">None selected · ${tr.length} key${tr.length === 1 ? "" : "s"} on this curve</span></div>`;
    return;
  }
  const k = S.selKey;
  box.innerHTML = sec(`Keyframe ${tr.indexOf(k) + 1} of ${tr.length}`) +
    prop(`<label for="k_t">Time</label>`, `<input type="number" class="num" id="k_t" data-k="t" step="0.05" min="0" max="${c.duration}" value="${k[0].toFixed(2)}"><span class="unit">s</span>`) +
    prop(`<label for="k_v">Height</label>`, `<input type="number" class="num" id="k_v" data-k="v" step="0.01" min="0" max="1" value="${k[1].toFixed(2)}"><span class="unit">0–1</span>`) +
    prop(`<label for="k_e">Transition</label>`, `<select id="k_e" data-k="ease">${S.eases.map((e) => `<option value="${e}" ${e === (k[2] || "smooth") ? "selected" : ""}>${EASE_LABEL[e] || e}</option>`).join("")}</select>`) +
    `<div class="p-full"><button class="btn small" data-act="delKey">Delete Keyframe</button></div>`;
}
function editKeyField(t) {
  const c = clipById(S.selClip), k = S.selKey; if (!k || t.value === "") return;
  if (t.dataset.k === "t") k[0] = clamp(parseFloat(t.value) || 0, 0, c.duration);
  if (t.dataset.k === "v") k[1] = clamp(parseFloat(t.value) || 0);
  activeTrack(c).sort((a, b) => a[0] - b[0]);
  showChanged();
}
function deleteSelectedKey() {
  const c = clipById(S.selClip); const tr = activeTrack(c);
  if (!S.selKey || tr.length <= 1) { toast("A curve needs at least one keyframe", true); return; }
  tr.splice(tr.indexOf(S.selKey), 1); S.selKey = null; updateKeyBox(); showChanged();
}

// ------------------------------------------------------------ render (sampled motion from the Pi)
const requestRender = debounce(async () => {
  const c = clipById(S.selClip);
  if (!c) { S.render = null; return; }
  try {
    const r = await api("POST", "/api/render", { clip: c, clips: S.show.clips });
    S.render = r; S.renderFor = c.id;
    const bad = r.limited.filter(Boolean).length;
    const w = $("#limitWarning");
    w.hidden = !bad;
    if (bad) w.textContent = "Rate limit: segments marked in red exceed the configured max speed or acceleration and will lag. Increase period/length, reduce amplitude, or raise limits in Setup.";
  } catch (e) { S.render = null; }
}, 180);

function renderTraceSel() {
  $("#traceSel").innerHTML = `<option value="-1">None</option>` +
    S.config.motors.map((m, i) => `<option value="${i}" ${i === S.highlight ? "selected" : ""}>${esc(m.name)}</option>`).join("");
}
$("#traceSel").onchange = (ev) => { S.highlight = +ev.target.value; };

// ------------------------------------------------------------ canvases
function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(cv.clientWidth * dpr), h = Math.round(cv.clientHeight * dpr);
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, cv.clientWidth, cv.clientHeight];
}
const FONT = "11px 'Segoe UI', -apple-system, Roboto, Arial, sans-serif";
const MONO = "11px Consolas, Menlo, 'DejaVu Sans Mono', monospace";

/** Axis monitor: engineering-style elevation view. One ruler in real units when every motor uses
 *  the same units; otherwise each axis is drawn as a share of its own travel (0-100%). */
function drawStage() {
  const cv = $("#stage"); if (!cv.clientWidth || !S.config) return;
  const [ctx, W, H] = fitCanvas(cv);
  ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);
  const motors = S.config.motors, n = motors.length, tk = S.tick;
  const L = 58, R = 16, T = 26, B = 40;
  const units = motors.map((m, i) => unitOf(i));
  const uniform = units.every((u) => u === units[0]);
  const travelMax = Math.max(...motors.map((m, i) => travelOf(i)));
  // y for motor i at value v (continuous rotation: |speed|)
  const yv = (i, v) => T + (Math.abs(v) / (uniform ? travelMax : travelOf(i) || 1)) * (H - T - B);
  const ymm = (mm) => T + (mm / travelMax) * (H - T - B);
  // ruler + grid
  ctx.font = MONO; ctx.textAlign = "right"; ctx.lineWidth = 1;
  if (uniform) {
    const step = niceStep(travelMax);
    for (let mm = 0; mm <= travelMax + 1e-6; mm += step) {
      const y = Math.round(ymm(mm)) + 0.5;
      ctx.strokeStyle = Math.round(mm / step) % 2 === 0 ? "#dfe2e5" : "#eff1f3";
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
      ctx.fillStyle = "#6d757e"; ctx.fillText(`${+mm.toFixed(2)}`, L - 8, y + 4);
    }
  } else {
    for (let k = 0; k <= 4; k++) {
      const y = Math.round(T + (k / 4) * (H - T - B)) + 0.5;
      ctx.strokeStyle = k % 2 === 0 ? "#dfe2e5" : "#eff1f3";
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
      ctx.fillStyle = "#6d757e"; ctx.fillText(`${k * 25}%`, L - 8, y + 4);
    }
  }
  ctx.save(); ctx.translate(12, (T + H - B) / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = "center"; ctx.font = FONT;
  ctx.fillText(uniform ? `From home (${units[0]})` : "From home (% of each axis' travel)", 0, 0); ctx.restore();
  // beam
  ctx.fillStyle = "#4a525b"; ctx.fillRect(L, T - 8, W - L - R, 3);
  const colW = (W - L - R) / n;
  for (let i = 0; i < n; i++) {
    const m = motors[i];
    const cx = Math.round(L + colW * (i + 0.5)) + 0.5;
    const travel = travelOf(i);
    const out = tk?.out_mm?.[i] ?? 0, act = tk?.actual_mm?.[i], lim = tk?.limited?.[i];
    // travel envelope
    ctx.strokeStyle = "#c9cdd2"; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(cx, yv(i, 0)); ctx.lineTo(cx, yv(i, travel)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#8a929b"; ctx.fillRect(cx - 4, yv(i, travel) - 0.5, 8, 1);
    // drive + link
    ctx.fillStyle = "#6d757e"; ctx.fillRect(cx - 6, T - 12, 12, 7);
    ctx.strokeStyle = "#2a3036"; ctx.beginPath(); ctx.moveTo(cx, T - 5); ctx.lineTo(cx, yv(i, out)); ctx.stroke();
    // commanded position (continuous rotation: speed)
    ctx.fillStyle = lim ? "#c62828" : "#2a3036";
    ctx.fillRect(cx - 7, yv(i, out) - 4, 14, 8);
    // reported position
    if (act !== null && act !== undefined) {
      ctx.strokeStyle = "#2f63a4"; ctx.lineWidth = 1.5;
      ctx.strokeRect(cx - 10.5, yv(i, act) - 6.5, 21, 13); ctx.lineWidth = 1;
    }
    // labels
    ctx.textAlign = "center"; ctx.fillStyle = "#1b1f23"; ctx.font = FONT;
    ctx.fillText(m.name, cx, H - 22);
    ctx.fillStyle = "#4a525b"; ctx.font = MONO;
    ctx.fillText(fmtVal(out, units[i]), cx, H - 8);
  }
  ctx.textAlign = "left"; ctx.font = FONT; ctx.fillStyle = "#6d757e";
  ctx.fillText(tk?.armed ? "OUTPUT: ENABLED" : "OUTPUT: PREVIEW", L, 12);
}

function scoreGeom(W, H, dur) {
  const L = 48, R = 12, T = 18, B = 26;
  return { L, R, T, B, tx: (t) => L + (t / dur) * (W - L - R), vy: (v) => T + v * (H - T - B),
    xt: (x) => ((x - L) / (W - L - R)) * dur, yv: (y) => (y - T) / (H - T - B) };
}

/** Motion profile: oscilloscope-style plot. */
function drawScore() {
  const cv = $("#score"); if (!cv.clientWidth) return;
  const [ctx, W, H] = fitCanvas(cv);
  ctx.fillStyle = SCOPE.bg; ctx.fillRect(0, 0, W, H);
  const c = clipById(S.selClip);
  if (!c) return;
  const dur = c.duration, g = scoreGeom(W, H, dur), n = nMotors();
  cv.classList.toggle("editable", c.type === "timeline");
  $("#scoreTitle").textContent = `Motion Profile — ${c.name}`;
  $("#scoreHint").textContent = c.type === "timeline" ? "Yellow: editable curve · Gray: all axes after stagger" : "One trace per axis over one clip length";
  // grid: 10 x-divisions, 4 y-divisions
  ctx.lineWidth = 1; ctx.font = MONO;
  for (let k = 0; k <= 4; k++) {
    const y = Math.round(g.vy(k / 4)) + 0.5;
    ctx.strokeStyle = k % 2 === 0 ? SCOPE.gridMajor : SCOPE.grid;
    ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(W - g.R, y); ctx.stroke();
    ctx.fillStyle = SCOPE.axis; ctx.textAlign = "right"; ctx.fillText(k === 0 ? "HOME" : k === 4 ? "FAR" : (k / 4).toFixed(2), g.L - 6, y + 4);
  }
  const stepT = dur > 600 ? 60 : dur > 240 ? 30 : dur > 90 ? 10 : dur > 20 ? 5 : dur > 8 ? 2 : 1;
  ctx.textAlign = "center";
  for (let t = 0; t <= dur + 1e-6; t += stepT) {
    const x = Math.round(g.tx(t)) + 0.5;
    ctx.strokeStyle = t % (stepT * 5) === 0 ? SCOPE.gridMajor : SCOPE.grid;
    ctx.beginPath(); ctx.moveTo(x, g.T); ctx.lineTo(x, H - g.B); ctx.stroke();
    ctx.fillStyle = SCOPE.axis; ctx.textAlign = x > W - g.R - 16 ? "right" : "center"; ctx.fillText(fmtTime(t), Math.min(x, W - 4), H - 9);
  }
  // server-rendered motion of every axis
  const r = S.renderFor === c.id ? S.render : null;
  if (r) {
    ctx.fillStyle = SCOPE.limit;
    r.limited.forEach((bad, k) => { if (bad) ctx.fillRect(g.tx(r.t[k]) - 1.5, g.T - 10, 3, 5); });
    const hi = c.type === "timeline" && c.mode === "per_motor" ? S.selMotor : S.highlight;
    for (let i = 0; i < n; i++) {
      if (i === hi) continue;
      ctx.strokeStyle = c.type === "timeline" ? SCOPE.traceDim : SCOPE.trace; ctx.lineWidth = 1;
      ctx.beginPath();
      r.t.forEach((t, k) => { const x = g.tx(t), y = g.vy(r.values[k][i]); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
    }
    if (hi >= 0 && hi < n && c.type !== "timeline") {
      ctx.strokeStyle = SCOPE.hi; ctx.lineWidth = 1.8; ctx.beginPath();
      r.t.forEach((t, k) => { const x = g.tx(t), y = g.vy(r.values[k][hi]); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
    }
  }
  // editable curve + keyframes
  if (c.type === "timeline") {
    const tr = activeTrack(c), wrap = c.wrap !== false;
    ctx.strokeStyle = SCOPE.hi; ctx.lineWidth = 1.8; ctx.beginPath();
    for (let px = 0; px <= 400; px++) { const t = (px / 400) * dur; const x = g.tx(t), y = g.vy(clamp(evalTrack(tr, t, dur, wrap))); px ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    ctx.stroke();
    for (const k of tr) {
      const x = Math.round(g.tx(k[0])), y = Math.round(g.vy(k[1])), sel = k === S.selKey, s = sel ? 5 : 4;
      ctx.fillStyle = sel ? SCOPE.keySel : SCOPE.bg; ctx.strokeStyle = sel ? SCOPE.keySel : SCOPE.key; ctx.lineWidth = 1.5;
      ctx.fillRect(x - s, y - s, s * 2, s * 2); ctx.strokeRect(x - s + 0.5, y - s + 0.5, s * 2 - 1, s * 2 - 1);
      if (sel) { ctx.fillStyle = SCOPE.key; ctx.font = MONO; ctx.textAlign = "left"; ctx.fillText(`${k[0].toFixed(2)} s, ${k[1].toFixed(2)}`, x + 9, y - 8); }
    }
  }
  // playhead
  if (S.tick && S.tick.clip === c.id && (S.tick.mode === "solo" || S.tick.mode === "playlist")) {
    const x = Math.round(g.tx(S.tick.clip_t % dur)) + 0.5;
    ctx.strokeStyle = SCOPE.play; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, g.T); ctx.lineTo(x, H - g.B); ctx.stroke();
  }
}

// profile interactions (timeline clips)
(function scoreInput() {
  const cv = $("#score"); let drag = null;
  const pos = (ev) => { const r = cv.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
  const ctxInfo = () => { const c = clipById(S.selClip); return c && c.type === "timeline" ? [c, scoreGeom(cv.clientWidth, cv.clientHeight, c.duration)] : [null, null]; };
  cv.addEventListener("pointerdown", (ev) => {
    const [c, g] = ctxInfo(); if (!c) return;
    const [x, y] = pos(ev);
    const hit = activeTrack(c).find((k) => Math.hypot(g.tx(k[0]) - x, g.vy(k[1]) - y) < 10);
    S.selKey = hit || null; updateKeyBox();
    if (hit) { drag = hit; cv.setPointerCapture(ev.pointerId); }
    cv.focus();
  });
  cv.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const [c, g] = ctxInfo(); const [x, y] = pos(ev);
    let t = clamp(g.xt(x), 0, c.duration); if (!ev.altKey) t = Math.round(t * 4) / 4;
    drag[0] = t; drag[1] = clamp(Math.round(g.yv(y) * 100) / 100);
    updateKeyBox();
  });
  cv.addEventListener("pointerup", () => {
    if (!drag) return;
    const [c] = ctxInfo(); activeTrack(c).sort((a, b) => a[0] - b[0]); drag = null; updateKeyBox(); showChanged();
  });
  cv.addEventListener("dblclick", (ev) => {
    const [c, g] = ctxInfo(); if (!c) return;
    const [x, y] = pos(ev);
    const k = [clamp(Math.round(g.xt(x) * 4) / 4, 0, c.duration), clamp(Math.round(g.yv(y) * 100) / 100), "smooth"];
    const tr = activeTrack(c); tr.push(k); tr.sort((a, b) => a[0] - b[0]);
    S.selKey = k; updateKeyBox(); showChanged();
  });
  cv.addEventListener("keydown", (ev) => { if ((ev.key === "Delete" || ev.key === "Backspace") && S.selKey) { ev.preventDefault(); deleteSelectedKey(); } });
})();

// ------------------------------------------------------------ title bar, toolbar, status bar
const MODE_LABEL = { idle: "IDLE", playlist: "SHOW", solo: "PREVIEW", hold: "HOLD", park: "PARK" };
function renderStatus() {
  const tk = S.tick;
  const ind = [];
  ind.push(`<span class="ind">${led(S.wsOk ? "on" : "bad")}Controller</span>`);
  if (tk) {
    ind.push(`<span class="ind">${led(tk.link ? "on" : "bad")}Bridge</span>`);
    const byId = new Map();
    for (const nd of tk.nodes) if (nd.online && nd.id !== null && nd.id < nMotors() && !byId.has(nd.id)) byId.set(nd.id, nd);
    const assigned = [...byId.values()];
    const homed = assigned.filter((nd) => nd.flags.includes("homed")).length;
    ind.push(`<span class="ind">${led(assigned.length === nMotors() ? "on" : assigned.length ? "warn" : "bad")}Axes <b>${assigned.length}/${nMotors()}</b></span>`);
    ind.push(`<span class="ind">${led(homed === nMotors() ? "on" : homed ? "warn" : "")}Homed <b>${homed}/${nMotors()}</b></span>`);
    const faults = tk.nodes.filter((nd) => nd.online && nodeFault(nd)).length;
    ind.push(`<span class="ind">${led(faults ? "bad" : "")}Faults <b>${faults}</b></span>`);
    if (S.simulate) ind.push(`<span class="ind">${led("warn")}Simulation</span>`);

    // toolbar readouts
    const c = clipById(tk.clip);
    $("#roMode").textContent = MODE_LABEL[tk.mode] || tk.mode.toUpperCase();
    $("#roClip").textContent = c && (tk.mode === "playlist" || tk.mode === "solo") ? c.name + (tk.fading ? " (fade)" : "") : "—";
    $("#roTime").textContent = c && (tk.mode === "playlist" || tk.mode === "solo") ? `${fmtTimeTenths(tk.clip_t % c.duration)}` : "—";

    // enable button
    const b = $("#btnEnable");
    b.setAttribute("aria-pressed", String(tk.armed));
    $("#enableLed").className = "led" + (tk.armed ? " on" : "");
    $("#enableText").textContent = tk.armed ? "ENABLED" : "DISABLED";

    // status bar
    $("#sbState").textContent = tk.startup === "running" ? (tk.armed ? "Running" : "Ready") : `Startup: ${tk.startup}`;
    $("#sbPort").textContent = `Bridge: ${tk.link ? tk.port : "not connected"}${tk.bridge ? ` · ch ${tk.bridge.channel}` : ""}`;
    $("#sbRate").textContent = `${S.config.frame_rate || 50} Hz`;
    $("#sbTx").textContent = tk.bridge ? `TX ${tk.bridge.tx_ok}${tk.bridge.tx_fail ? ` / fail ${tk.bridge.tx_fail}` : ""}` : "TX —";
    const exc = $("#sbException");
    const activeFault = tk.nodes.find((nd) => nd.online && nodeFault(nd));
    if (activeFault) { exc.className = "sb-cell sb-exception bad"; exc.textContent = `${axisName(activeFault)}: ${nodeFault(activeFault)}`; }
    else if (S.lastWarn && Date.now() / 1000 - S.lastWarn.t < 120) { exc.className = "sb-cell sb-exception warn"; exc.textContent = S.lastWarn.msg; }
    else { exc.className = "sb-cell sb-exception"; exc.textContent = "No exceptions"; }
  }
  $("#indicators").innerHTML = ind.join("");
  updateSoloHint();
}

$("#btnPlay").onclick = () => {
  // The show only drives the motors while ENABLED. After Motor Setup (or Stop All) it's disabled,
  // and Run Show alone would just preview on screen.
  if (!S.tick?.armed && confirm("The motors are DISABLED, so the show would only play on screen.\n\n" +
      "OK: enable the motors and run the show.\nCancel: preview on screen only.")) send("arm", { on: true });
  send("play");
};
$("#btnNext").onclick = () => send("next");
$("#btnHold").onclick = () => send("hold");
$("#btnPark").onclick = () => send("park");
$("#btnStopAll").onclick = () => send("stop_all");
$("#btnEnable").onclick = () => {
  const on = !S.tick?.armed;
  if (on && !confirm("Enable axes? The motors will begin following the show.")) return;
  send("arm", { on });
};

// ------------------------------------------------------------ axes tab
const axisName = (nd) => (nd.id !== null && S.config.motors[nd.id] ? S.config.motors[nd.id].name : nd.mac);
function duplicateIds() {
  const seen = {}, dup = new Set();
  for (const nd of S.tick?.nodes || []) if (nd.online && nd.id !== null) { if (seen[nd.id]) dup.add(nd.id); seen[nd.id] = true; }
  return dup;
}
function nodeFault(nd) {
  const f = nd.flags;
  if (nd.online && nd.id !== null && duplicateIds().has(nd.id)) return `Duplicate axis number #${nd.id}`;
  if (!f.includes("power_good")) return "No USB-PD power";
  if (!f.includes("tmc_ok")) return "Driver not responding";
  if (f.includes("overtemp")) return "Driver over temperature";
  if (f.includes("home_failed")) return "Homing failed";
  if (f.includes("slip")) return "Position error (encoder mismatch)";
  if (f.includes("link_lost") && nd.id !== null) return "Command stream lost";
  if (memoryMode() && nd.id !== null) {
    const setupMac = S.tick?.setup_macs?.[nd.id];
    if (S.tick?.commissioned?.[nd.id] && setupMac && setupMac !== nd.mac) return "Different board than was set up: run Motor Setup";
    if (!f.includes("calibrated")) return "Not set up: run Motor Setup";
    if (!f.includes("homed") && S.tick?.kinds?.[nd.id] !== "continuous") return "Position unknown: Motor Setup > Find home";
  }
  return "";
}
const memoryMode = () => (S.tick?.home_mode || S.config?.mechanics.home_mode) === "memory";
const kindOf = (m) => (S.kinds || {})[m?.kind || "spool"] || {};
const JOG_STEPS = { spool: [50, 10, 1], linear: [20, 5, 0.5], rotary: [45, 10, 1], continuous: [1, 0.25, 0.05] };
const jogSteps = (i) => JOG_STEPS[S.tick?.kinds?.[i] || "spool"] || JOG_STEPS.spool;
const unitOf = (i) => S.tick?.units?.[i] || "mm";
const fmtVal = (v, u) => (u === "rev/s" ? v.toFixed(2) : v.toFixed(0)) + (u === "°" ? "°" : u === "mm" ? "" : " " + u);
function niceStep(range) {
  const raw = range / 8, p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw) || raw;
}
const travelOf = (i) => S.tick?.travel?.[i] ?? (S.config.motors[i].travel_mm || S.config.mechanics.travel_mm);
function renderNodes() {
  const tb = $("#nodeRows"); if (!S.tick) return;
  if (tb.contains(document.activeElement)) return;   // don't rebuild under the user's cursor
  const nodes = [...S.tick.nodes].sort((a, b) => (a.id ?? 99) - (b.id ?? 99));
  const idSelect = (nd) => `<select data-assign="${nd.mac}" aria-label="Assign axis number"><option value="">${nd.id === null ? "Assign…" : "Reassign…"}</option>` +
    S.config.motors.map((m, i) => `<option value="${i}">${esc(m.name)} (#${i})</option>`).join("") + `</select>`;
  tb.innerHTML = nodes.map((nd) => {
    const m = nd.id !== null ? S.config.motors[nd.id] : null;
    const f = nd.flags, id = nd.id, fault = nodeFault(nd);
    const homedLed = f.includes("homing") ? led("warn", "Homing") : led(f.includes("homed") ? "on" : (f.includes("pos_lost") ? "bad" : ""),
      f.includes("homed") ? (f.includes("restored") ? "Position recovered from memory" : "Position known") : (f.includes("pos_lost") ? "Position unknown" : "Not homed"));
    const mem = memoryMode();
    const errWarn = nd.err_mm !== null && Math.abs(nd.err_mm) > 5;
    return `<tr class="${nd.online ? "" : "offline"} ${id === null ? "unassigned" : ""}">
      <td>${m ? `<b>${esc(m.name)}</b> <span class="sub">#${id}</span>` : `<b>Unassigned</b>`} ${idSelect(nd)}</td>
      <td><span class="mono">${esc(nd.mac)}</span> <span class="sub">fw ${nd.fw} · ${fmtTime(nd.uptime)}</span></td>
      <td class="c">${led(nd.online ? (f.includes("streaming") ? "on" : "on") : "bad", nd.online ? (f.includes("streaming") ? "Streaming" : "Online") : "Offline")}</td>
      <td class="c">${led(f.includes("enabled") ? "on" : "", f.includes("enabled") ? "Driver enabled" : "Driver released")}</td>
      <td class="c">${homedLed}</td>
      <td class="c">${led(f.includes("limit") ? "warn" : "", f.includes("limit") ? "Limit switch closed" : "Limit open")}</td>
      <td class="c">${led(fault ? "bad" : "", fault || "No fault")}</td>
      <td class="r">${nd.vbus.toFixed(1)} V</td>
      <td class="r">${f.includes("homed") || !mem ? nd.pos_mm.toFixed(1) + " mm" : "—"}</td>
      <td class="r ${errWarn ? "warnv" : ""}">${nd.err_mm === null ? "—" : nd.err_mm.toFixed(1) + " mm"}</td>
      <td class="r">${nd.rssi} dBm</td>
      <td><div class="cmds">
        <button class="btn small" data-node="identify" data-mac="${nd.mac}">Identify</button>
        <button class="btn small" data-node="setup" data-mac="${nd.mac}" ${id !== null ? `data-id="${id}"` : ""} title="Direction, encoder check, teach top and bottom">Setup…</button>
        ${id !== null ? `<span class="gsep"></span>
        <button class="btn small" data-node="home" data-id="${id}">${mem ? "To Top" : "Home"}</button>
        ${mem ? "" : `<button class="btn small" data-node="set_zero" data-id="${id}" title="Current position = home">Zero</button>`}
        <span class="gsep"></span>
        ${jogSteps(id).slice(0, 2).map((d) => `<button class="btn small" data-node="jog" data-id="${id}" data-mm="${-d}" title="${d} ${esc(unitOf(id))} toward home">−${d}</button>`).join("")}
        ${jogSteps(id).slice(0, 2).reverse().map((d) => `<button class="btn small" data-node="jog" data-id="${id}" data-mm="${d}" title="${d} ${esc(unitOf(id))} away from home">+${d}</button>`).join("")}
        <span class="gsep"></span>
        <button class="btn small" data-node="enable" data-id="${id}" data-on="0">Release</button>
        <button class="btn small" data-node="remove" data-id="${id}" title="Remove this motor from the sculpture">Remove</button>` : ""}
      </div></td></tr>`;
  }).join("") || `<tr><td colspan="12" class="sub">No axis nodes detected. Check the bridge connection and node power.</td></tr>`;
  const missing = S.config.motors.map((m, i) => i).filter((i) => !S.tick.nodes.some((nd) => nd.online && nd.id === i));
  if (missing.length) tb.insertAdjacentHTML("beforeend", `<tr><td colspan="12" class="sub">Empty or not detected: ${missing.map((i) =>
    `${esc(S.config.motors[i].name)} <button class="btn small" data-node="setup_slot" data-id="${i}" title="Set up the board for ${esc(S.config.motors[i].name)}">Setup…</button><button class="btn small" data-node="remove" data-id="${i}" title="Remove ${esc(S.config.motors[i].name)}">Remove</button>`).join(" &nbsp; ")}</td></tr>`);
}
$("#nodeRows").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-node]"); if (!b) return;
  const a = b.dataset.node, id = b.dataset.id !== undefined ? +b.dataset.id : undefined;
  if (a === "remove") {
    const name = S.config.motors[id]?.name || `M${id + 1}`;
    if (S.config.motors.length <= 1) { toast("Keep at least one motor", true); return; }
    const later = S.config.motors.length - 1 - id;
    if (confirm(`Remove ${name}? Its board is released, and its setup is discarded.` +
      (later > 0 ? `\n\nThe ${later} motor(s) after it move down one number (their boards are renumbered automatically).` : ""))) {
      send("remove_motor", { id }); setTimeout(loadState, 600);
    }
    return;
  }
  if (a === "identify") send("identify", { mac: b.dataset.mac });
  else if (a === "setup_slot") location.href = `/setup?from=editor&motor=${id}`;
  else if (a === "setup") location.href = `/setup?from=editor&mac=${encodeURIComponent(b.dataset.mac)}${id !== undefined ? `&motor=${id}` : ""}`;
  else if (a === "jog") send("jog", { id, mm: +b.dataset.mm });
  else if (a === "enable") send("enable", { id, on: b.dataset.on === "1" });
  else send(a, { id });
});
$("#nodeRows").addEventListener("change", (ev) => {
  const s = ev.target; if (!s.dataset.assign || s.value === "") return;
  send("assign", { mac: s.dataset.assign, new_id: +s.value }); s.blur(); toast("Axis number assigned");
});
$$("[data-all]").forEach((b) => b.addEventListener("click", () => {
  const a = b.dataset.all;
  if (a === "enable_off") send("enable", { id: "all", on: false });
  else if (a === "set_zero") { if (confirm("Set every axis' current position as home (fully wound in)?")) send("set_zero", { id: "all" }); }
  else if (a === "autostart") send(a);
  else if (a === "setup") location.href = "/setup?from=editor";
  else if (a === "add_motor") location.href = "/setup?from=editor&new=1";
  else if (a === "count_inc") { send("set_motor_count", { n: S.config.motors.length + 1 }); }
  else if (a === "count_dec") {
    const n = S.config.motors.length, last = n - 1, m = S.config.motors[last];
    if (n <= 1) { toast("Keep at least one motor", true); return; }
    const busy = S.tick?.commissioned?.[last] || (S.tick?.nodes || []).some((nd) => nd.online && nd.id === last);
    if (busy && !confirm(`${m.name} is set up or has a board. Remove it?`)) return;
    send("set_motor_count", { n: n - 1, force: true });
  }
  else if (a === "remove_unused") {
    if (confirm("Remove every motor that hasn't been set up and has no board reporting?")) { send("remove_unused"); setTimeout(loadState, 600); }
  }
  else if (a === "sim_cut") { if (confirm("SIMULATOR: cut power to every motor (power-loss save works)?")) send("sim_power_cut", { save_works: true }); }
  else if (a === "sim_cut_fail") { if (confirm("SIMULATOR: cut power with the power-loss save FAILING? Moving motors lose their position.")) send("sim_power_cut", { save_works: false }); }
  else send(a, { id: "all" });
}));

// ------------------------------------------------------------ setup
const MECH_FIELDS = [
  { sec: "Drive train" },
  { key: "spool_diameter_mm", label: "Spool diameter", unit: "mm", step: 0.1, help: "Default for cable-spool motors: effective diameter where the cable wraps. Other mechanisms set their size per motor." },
  { key: "travel_mm", label: "Travel", unit: "mm", step: 1, help: "Default for cable-spool motors until their ends are taught." },
  { key: "full_steps_per_rev", label: "Motor steps/rev", options: [200, 400] },
  { key: "microsteps", label: "Microstepping", options: [4, 8, 16, 32, 64] },
  { sec: "Motion limits" },
  { key: "max_speed_mm_s", label: "Max velocity", unit: "mm/s", step: 1, help: "Default for cable-spool motors. Set other motors per motor (Axis Layout table)." },
  { key: "max_accel_mm_s2", label: "Max acceleration", unit: "mm/s²", step: 1, help: "Default for cable-spool motors." },
  { sec: "Driver" },
  { key: "run_current_pct", label: "Run current", unit: "%", step: 1 },
  { key: "hold_current_pct", label: "Hold current", unit: "%", step: 1 },
  { key: "pd_volts", label: "Supply request (USB-PD)", unit: "V", options: [9, 12, 15, 20] },
  { key: "spreadcycle", label: "SpreadCycle chopper", bool: true, help: "Louder, more torque at speed. Disables StallGuard homing." },
  { key: "invert_dir", label: "Invert motor direction", bool: true, help: "Default for motors not yet through Motor Setup. Set-up motors keep their own direction." },
  { key: "invert_encoder", label: "Invert encoder", bool: true, help: "Default for motors not yet through Motor Setup." },
  { sec: "Homing" },
  { key: "home_mode", label: "Method", options: ["memory", "limit", "stallguard", "manual"], labels: { memory: "Position memory (no switch)", limit: "Limit switch (AUX2)", stallguard: "StallGuard (sensorless)", manual: "Manual zero" },
    help: "Position memory: each motor's top and bottom are taught once in Motor Setup; the encoder and a saved turn count recover the position after power cuts." },
  { key: "home_speed_mm_s", label: "Homing velocity", unit: "mm/s", step: 1 },
  { key: "home_backoff_mm", label: "Back-off distance", unit: "mm", step: 1 },
  { key: "home_timeout_s", label: "Timeout", unit: "s", step: 1 },
  { key: "stall_threshold", label: "StallGuard threshold", unit: "0–255", step: 1 },
];
const START_FIELDS = [
  { sec: "Power-up behaviour" },
  { key: "autoplay", label: "Run show at power-up", bool: true },
  { key: "auto_home", label: "Home axes before running", bool: true },
  { key: "assume_home_at_boot", label: "Manual homing: accept power-up position as home", bool: true },
  { sec: "Communication" },
  { key: "frame_rate", label: "Update rate", unit: "Hz", options: [25, 40, 50] },
  { key: "serial_port", label: "Bridge serial port", text: true, help: "auto, or a device such as /dev/ttyACM0" },
];
function formHtml(fields, obj) {
  return fields.map((f) => {
    if (f.sec) return sec(f.sec);
    const v = obj[f.key], id = `s_${f.key}`;
    let input;
    if (f.bool) input = `<input type="checkbox" id="${id}" name="${f.key}" ${v ? "checked" : ""}>`;
    else if (f.options) input = `<select id="${id}" name="${f.key}">${f.options.map((o) => `<option value="${o}" ${String(o) === String(v) ? "selected" : ""}>${esc(f.labels?.[o] ?? o)}</option>`).join("")}</select>${f.unit ? `<span class="unit">${f.unit}</span>` : ""}`;
    else if (f.text) input = `<input type="text" id="${id}" name="${f.key}" value="${esc(v)}">`;
    else input = `<input type="number" class="num" id="${id}" name="${f.key}" step="${f.step || 1}" value="${v}"><span class="unit">${f.unit || ""}</span>`;
    return prop(`<label for="${id}">${esc(f.label)}</label>`, input, f.help);
  }).join("");
}
function readForm(root, fields) {
  const out = {};
  for (const f of fields) {
    if (f.sec) continue;
    const el = root.querySelector(`[name="${f.key}"]`);
    if (f.bool) out[f.key] = el.checked;
    else if (f.text || (f.options && typeof f.options[0] === "string")) out[f.key] = el.value;
    else out[f.key] = parseFloat(el.value);
  }
  return out;
}
function renderSettings() {
  $("#mechForm").innerHTML = formHtml(MECH_FIELDS, S.config.mechanics);
  $("#startForm").innerHTML = formHtml(START_FIELDS, S.config);
  $("#spmInfo").textContent = `${S.spm.toFixed(3)} µstep/mm`;
  $("#presetCount").value = S.config.motors.length;
  renderLayoutTable();
  S.groups = null; renderGroups();
}
$("#btnSaveMech").onclick = async () => {
  const mech = readForm($("#mechForm"), MECH_FIELDS);
  const r = await api("PUT", "/api/config", { mechanics: mech });
  S.config = r.config; S.spm = r.steps_per_mm; renderSettings();
  send("push_config", { id: "all" });
  toast(S.tick?.armed ? "Applied. Axes accept new settings once stationary." : "Applied and sent to axes");
  requestRender();
};
$("#btnSaveStart").onclick = async () => {
  const r = await api("PUT", "/api/config", readForm($("#startForm"), START_FIELDS));
  S.config = r.config; toast("Applied");
};
$("#btnPreset").onclick = async () => {
  const count = clamp(parseInt($("#presetCount").value, 10) || 8, 1, 16);
  if (!confirm(`Replace the layout with ${count} axes in a ${$("#presetKind").value} arrangement? Names and trims will reset.`)) return;
  await api("PUT", "/api/config", { layout_preset: $("#presetKind").value, layout_preset_count: count, layout: $("#presetKind").value });
  await loadState(); toast("Layout generated");
};
// ------------------------------------------------------------ motor groups
function renderGroups() {
  const box = $("#groupsBox"); if (!box || !S.config) return;
  S.groups = S.groups || JSON.parse(JSON.stringify(S.config.groups || []));
  if (!S.groups.length) { box.innerHTML = `<p class="note">No groups yet.</p>`; return; }
  box.innerHTML = `<table class="grid-table groups-table"><thead><tr><th>Group</th>${S.config.motors.map((m) => `<th class="c">${esc(m.name)}</th>`).join("")}<th></th></tr></thead><tbody>` +
    S.groups.map((g, gi) => `<tr><td><input type="text" data-g="name" data-gi="${gi}" value="${esc(g.name)}" aria-label="Group name"></td>` +
      S.config.motors.map((m, i) => `<td class="c"><input type="checkbox" data-g="m" data-gi="${gi}" data-mi="${i}" ${(g.motors || []).includes(i) ? "checked" : ""} aria-label="${esc(m.name)} in ${esc(g.name)}"></td>`).join("") +
      `<td><button class="btn small" data-gdel="${gi}">Delete</button></td></tr>`).join("") + `</tbody></table>`;
}
$("#groupsBox").addEventListener("input", (ev) => {
  const t = ev.target, g = S.groups?.[+t.dataset.gi]; if (!g) return;
  if (t.dataset.g === "name") g.name = t.value;
  else if (t.dataset.g === "m") {
    const i = +t.dataset.mi, set = new Set(g.motors || []);
    if (t.checked) set.add(i); else set.delete(i);
    g.motors = [...set].sort((a, b) => a - b);
  }
});
$("#groupsBox").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-gdel]"); if (!b) return;
  S.groups.splice(+b.dataset.gdel, 1); renderGroups();
});
$("#btnAddGroup").onclick = () => {
  S.groups = S.groups || [];
  S.groups.push({ name: `Group ${S.groups.length + 1}`, motors: [] }); renderGroups();
};
$("#btnSaveGroups").onclick = async () => {
  const names = S.groups.map((g) => g.name.trim());
  if (names.some((n) => !n) || new Set(names).size !== names.length) { toast("Each group needs a unique name", true); return; }
  const r = await api("PUT", "/api/config", { groups: S.groups.map((g) => ({ name: g.name.trim(), motors: g.motors || [] })) });
  S.config = r.config; S.groups = null; renderGroups(); renderEditor(); requestRender(); toast("Groups applied");
};

$("#btnSaveLayout").onclick = async () => {
  const r = await api("PUT", "/api/config", { motors: S.layoutMotors });
  S.config = r.config; S.layoutMotors = JSON.parse(JSON.stringify(S.config.motors)); renderTraceSel(); requestRender(); toast("Layout applied");
};
function renderLayoutTable() {
  $("#layoutRows").innerHTML = S.layoutMotors.map((m, i) => `<tr>
    <td class="r">${i}</td>
    <td><input type="text" data-l="name" data-i="${i}" value="${esc(m.name)}" aria-label="Name"></td>
    <td><input type="number" step="0.01" min="0" max="1" data-l="x" data-i="${i}" value="${m.x}" aria-label="X"></td>
    <td><input type="number" step="0.01" min="0" max="1" data-l="y" data-i="${i}" value="${m.y}" aria-label="Y"></td>
    <td><select data-l="kind" data-i="${i}" aria-label="Mechanism">${Object.entries(S.kinds || {}).map(([k, ki]) =>
      `<option value="${k}" ${(m.kind || "spool") === k ? "selected" : ""}>${esc(ki.label)}</option>`).join("")}</select></td>
    <td><input type="number" step="0.1" min="0.1" data-l="size" data-i="${i}" value="${m.size ?? ""}" placeholder="${kindOf(m).size_default ?? ""}" aria-label="Size" title="${esc(kindOf(m).size_label || "")}"></td>
    <td><input type="number" step="any" data-l="max_speed" data-i="${i}" value="${m.max_speed ?? ""}" placeholder="default" aria-label="Max speed" title="${esc(kindOf(m).unit)}/s"></td>
    <td><input type="number" step="any" data-l="max_accel" data-i="${i}" value="${m.max_accel ?? ""}" placeholder="default" aria-label="Max acceleration" title="${esc(kindOf(m).unit)}/s²"></td>
    <td><input type="number" step="1" data-l="trim_mm" data-i="${i}" value="${m.trim_mm || 0}" aria-label="Trim"></td>
    <td>${(m.kind || "spool") === "continuous" ? `<select data-l="bidirectional" data-i="${i}" aria-label="Directions"><option value="0" ${m.bidirectional ? "" : "selected"}>forward only</option><option value="1" ${m.bidirectional ? "selected" : ""}>both directions</option></select>` :
      m.commissioned && m.bottom_fs ? `<span class="sub" title="Taught in Motor Setup. Re-run Motor Setup to change it.">taught ${(S.tick?.travel?.[i] ?? 0).toFixed(0)} ${esc(kindOf(m).unit)}</span>` :
      `<input type="number" step="1" data-l="travel_mm" data-i="${i}" value="${m.travel_mm ?? ""}" placeholder="default" aria-label="Travel">`}</td></tr>`).join("");
}
$("#layoutRows").addEventListener("input", (ev) => {
  const t = ev.target, m = S.layoutMotors[+t.dataset.i]; if (!m) return;
  const k = t.dataset.l;
  if (k === "name") m.name = t.value;
  else if (k === "kind") { m.kind = t.value; renderLayoutTable(); }
  else if (k === "bidirectional") m.bidirectional = t.value === "1";
  else if (["size", "max_speed", "max_accel"].includes(k)) m[k] = t.value === "" ? null : parseFloat(t.value);
  else if (k === "travel_mm") m.travel_mm = t.value === "" ? null : parseFloat(t.value);
  else m[k] = k === "trim_mm" ? parseFloat(t.value) || 0 : clamp(parseFloat(t.value) || 0);
});
function drawLayout() {
  const cv = $("#layoutCanvas"); if (!cv.clientWidth || !S.layoutMotors) return;
  const [ctx, W, H] = fitCanvas(cv); const p = 26;
  ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);
  ctx.lineWidth = 1;
  for (let k = 0; k <= 10; k++) {
    const a = Math.round(p + (k / 10) * (W - 2 * p)) + 0.5;
    ctx.strokeStyle = k % 5 === 0 ? "#cfd3d8" : "#eceef0";
    ctx.beginPath(); ctx.moveTo(a, p); ctx.lineTo(a, H - p); ctx.moveTo(p, a); ctx.lineTo(W - p, a); ctx.stroke();
  }
  ctx.fillStyle = "#6d757e"; ctx.font = FONT; ctx.textAlign = "center";
  ctx.fillText("FRONT", W / 2, H - 8); ctx.fillText("PLAN VIEW", W / 2, 16);
  S.layoutMotors.forEach((m, i) => {
    const x = p + m.x * (W - 2 * p), y = p + (1 - m.y) * (H - 2 * p);
    const drag = S.layoutDrag === i;
    ctx.fillStyle = drag ? "#2f63a4" : "#2a3036";
    ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#1b1f23"; ctx.font = FONT; ctx.fillText(m.name, x, y - 11);
  });
}
(function layoutInput() {
  const cv = $("#layoutCanvas"), p = 26;
  const toXY = (ev) => { const r = cv.getBoundingClientRect(); return [clamp((ev.clientX - r.left - p) / (r.width - 2 * p)), clamp(1 - (ev.clientY - r.top - p) / (r.height - 2 * p))]; };
  cv.addEventListener("pointerdown", (ev) => {
    const [x, y] = toXY(ev);
    let best = -1, bd = 0.05;
    S.layoutMotors.forEach((m, i) => { const d = Math.hypot(m.x - x, m.y - y); if (d < bd) { bd = d; best = i; } });
    if (best >= 0) { S.layoutDrag = best; cv.setPointerCapture(ev.pointerId); }
  });
  cv.addEventListener("pointermove", (ev) => {
    if (S.layoutDrag === null) return;
    const [x, y] = toXY(ev); const m = S.layoutMotors[S.layoutDrag];
    m.x = Math.round(x * 100) / 100; m.y = Math.round(y * 100) / 100;
  });
  cv.addEventListener("pointerup", () => { if (S.layoutDrag !== null) { S.layoutDrag = null; renderLayoutTable(); } });
})();

// ------------------------------------------------------------ tabs + frame loop
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${b.dataset.tab}`));
}));

let lastSlow = 0;
function frame(ts) {
  if ($("#view-design").classList.contains("active")) { drawStage(); drawScore(); }
  if ($("#view-settings").classList.contains("active")) drawLayout();
  if (ts - lastSlow > 250) {
    lastSlow = ts; renderStatus();
    if ($("#view-motors").classList.contains("active")) renderNodes();
  }
  requestAnimationFrame(frame);
}

loadState().then(() => { connectWs(); requestAnimationFrame(frame); }).catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<p style="padding:20px;color:#c62828">Could not reach the controller: ${esc(e.message)}</p>`);
});
