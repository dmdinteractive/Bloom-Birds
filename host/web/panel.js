/* Kinetic operator panel — touch UI for the 7" Touch Display 2 (1280×720).
 * Talks to the same controller as the full editor: /api/state, /api/show, /api/config and the /ws socket.
 * No text entry anywhere: steppers, toggles and buttons only (the OS keyboard can't show over kiosk mode).
 */
"use strict";

const P = { config: null, show: null, host: null, simulate: false, tick: null, ws: null, wsOk: false,
  selAxis: 0, events: [], lastWarn: null, cardsKey: "", actionsKey: "", plKey: "" };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const fmtTime = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`; };
const led = (st) => `<span class="led ${st || ""}"></span>`;
const pct = (v) => `${Math.round(v * 100)}%`;
const nAxes = () => P.config.motors.length;
const clipById = (id) => P.show?.clips.find((c) => c.id === id);

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, warn = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (warn ? " warn" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), 2500);
}
function send(cmd, extra = {}) {
  if (P.ws && P.ws.readyState === 1) { P.ws.send(JSON.stringify({ cmd, ...extra })); return true; }
  toast("Not connected to the controller", true); return false;
}

// ------------------------------------------------------------ modal (replaces confirm/prompt)
function modal(msg, buttons, bodyHtml = "") {
  return new Promise((resolve) => {
    $("#modalMsg").textContent = msg;
    $("#modalBody").innerHTML = bodyHtml;
    $("#modalBtns").innerHTML = buttons.map((b, i) => `<button class="big ${b.cls || ""}" data-i="${i}">${esc(b.label)}</button>`).join("");
    const m = $("#modal"); m.hidden = false;
    const done = (v) => { m.hidden = true; m.onclick = null; resolve(v); };
    m.onclick = (ev) => {
      const b = ev.target.closest("button");
      if (b?.dataset.i !== undefined) done(buttons[+b.dataset.i].value);
      else if (b?.dataset.v !== undefined) done(b.dataset.v);
      else if (ev.target === m) done(null);
    };
  });
}
const confirmBox = (msg, ok = "OK", danger = false) =>
  modal(msg, [{ label: "Cancel", value: false }, { label: ok, value: true, cls: danger ? "danger" : "primary" }]);

// ------------------------------------------------------------ data
async function loadState() {
  const st = await api("GET", "/api/state");
  P.config = st.config; P.show = st.show; P.host = st.host; P.simulate = st.simulate;
  P.selAxis = clamp(P.selAxis, 0, nAxes() - 1);
  P.plKey = ""; renderPlaylist(); renderStartToggles(); renderSystem();
}
function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  P.ws = ws;
  ws.onopen = () => { P.wsOk = true; };
  ws.onclose = () => { P.wsOk = false; setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "tick") {
      P.tick = m; if (m.events) addEvents(m.events);
      if (P.config && m.travel && m.travel.length !== P.config.motors.length && !P.reloading) {   // motors added/removed
        P.reloading = true; loadState().then(() => { P.cardsKey = ""; P.actionsKey = ""; }).finally(() => { P.reloading = false; });
      }
    }
    else if (m.type === "events") addEvents(m.events);
    else if (m.type === "error") toast(m.msg, true);
  };
}
function addEvents(evs) {
  for (const e of evs) { P.events.push(e); if (e.level === "warn") P.lastWarn = e; }
  P.events = P.events.slice(-150);
  $("#log").innerHTML = P.events.slice().reverse().map((e) =>
    `<div class="${e.level}"><time>${new Date(e.t * 1000).toLocaleTimeString()}</time><span>${esc(e.msg)}</span></div>`).join("");
}

// ------------------------------------------------------------ node helpers
function duplicateIds() {
  const seen = {}, dup = new Set();
  for (const nd of P.tick?.nodes || []) if (nd.online && nd.id !== null) { if (seen[nd.id]) dup.add(nd.id); seen[nd.id] = 1; }
  return dup;
}
function nodeFault(nd) {
  if (!nd) return "Not detected";
  if (!nd.online) return "Offline";
  const f = nd.flags;
  if (nd.id !== null && duplicateIds().has(nd.id)) return "Duplicate axis number";
  if (!f.includes("power_good")) return "No USB-PD power";
  if (!f.includes("tmc_ok")) return "Driver not responding";
  if (f.includes("overtemp")) return "Driver over temperature";
  if (f.includes("home_failed")) return "Homing failed";
  if (f.includes("slip")) return "Position error";
  if (f.includes("link_lost") && nd.id !== null) return "Command stream lost";
  if (memoryMode() && !f.includes("unassigned")) {
    const setupMac = P.tick?.setup_macs?.[nd.id];
    if (P.tick?.commissioned?.[nd.id] && setupMac && setupMac !== nd.mac) return "Different board than was set up: run Motor Setup";
    if (!f.includes("calibrated")) return "Not set up: Motor Setup";
    if (!f.includes("homed")) return "Position unknown: Motor Setup > Find top";
  }
  return "";
}
const memoryMode = () => (P.tick?.home_mode || P.config?.mechanics.home_mode) === "memory";
const travelOf = (i) => P.tick?.travel?.[i] ?? (P.config.motors[i].travel_mm || P.config.mechanics.travel_mm);
const setupUrl = (q) => `/setup?from=panel&${new URLSearchParams(q)}`;
const nodeFor = (i) => (P.tick?.nodes || []).find((nd) => nd.online && nd.id === i) || (P.tick?.nodes || []).find((nd) => nd.id === i);

// ------------------------------------------------------------ top bar + status
const MODE = { idle: "IDLE", playlist: "SHOW RUNNING", solo: "PREVIEW (LOOPING ONE CLIP)", hold: "HOLDING", park: "PARKING" };
function renderTop() {
  const tk = P.tick;
  const L = [`<span class="light">${led(P.wsOk ? "on" : "bad")}Controller</span>`];
  if (tk) {
    L.push(`<span class="light">${led(tk.link ? "on" : "bad")}Bridge</span>`);
    const ids = new Set(tk.nodes.filter((nd) => nd.online && nd.id !== null && nd.id < nAxes()).map((nd) => nd.id));
    const homed = [...ids].filter((i) => nodeFor(i)?.flags.includes("homed")).length;
    const faults = P.config.motors.filter((m, i) => nodeFault(nodeFor(i))).length;
    L.push(`<span class="light">${led(ids.size === nAxes() ? "on" : ids.size ? "warn" : "bad")}Axes <b>${ids.size}/${nAxes()}</b></span>`);
    L.push(`<span class="light">${led(homed === nAxes() ? "on" : homed ? "warn" : "")}${memoryMode() ? "Located" : "Homed"} <b>${homed}/${nAxes()}</b></span>`);
    L.push(`<span class="light">${led(faults ? "bad" : "")}Faults <b>${faults}</b></span>`);
    if (P.simulate) L.push(`<span class="light">${led("warn")}Simulation</span>`);
    const b = $("#btnEnable");
    b.setAttribute("aria-pressed", String(tk.armed));
    $("#enLed").className = "led" + (tk.armed ? " on" : "");
    $("#enText").textContent = tk.armed ? "ENABLED" : "DISABLED";

    $("#stState").textContent = `${tk.armed ? "Axes enabled" : "Axes disabled"} · ${tk.startup === "running" ? "running" : "startup: " + tk.startup}`;
    const exc = $("#stException");
    const fi = P.config.motors.findIndex((m, i) => nodeFault(nodeFor(i)));
    if (fi >= 0) { exc.className = "exc bad"; exc.textContent = `${P.config.motors[fi].name}: ${nodeFault(nodeFor(fi))}`; }
    else if (P.lastWarn && Date.now() / 1000 - P.lastWarn.t < 120) { exc.className = "exc warn"; exc.textContent = P.lastWarn.msg; }
    else { exc.className = "exc"; exc.textContent = "No exceptions"; }
  }
  $("#lights").innerHTML = L.join("");
}
$("#btnEnable").onclick = async () => {
  const on = !P.tick?.armed;
  if (on && !(await confirmBox("Enable axes? The motors will start following the show.", "Enable"))) return;
  send("arm", { on });
};
$("#btnStop").onclick = () => { send("stop_all"); toast("All axes stopping"); };

// ------------------------------------------------------------ run view
function entryLength(e) { const c = clipById(e.clip); return (c?.duration || 0) * Math.max(1, e.repeat || 1); }
function renderRun() {
  const tk = P.tick; if (!tk) return;
  const c = clipById(tk.clip);
  $("#nowMode").textContent = MODE[tk.mode] || tk.mode.toUpperCase();
  $("#nowClip").textContent = c && (tk.mode === "playlist" || tk.mode === "solo") ? c.name : "—";
  let frac = 0, time = "—", next = "";
  if (tk.mode === "playlist" && P.show.playlist.length) {
    const e = P.show.playlist[tk.entry % P.show.playlist.length];
    const len = entryLength(e);
    frac = len ? clamp(tk.entry_t / len, 0, 1) : 0;
    time = `${fmtTime(tk.entry_t)} / ${fmtTime(len)}`;
    const pl = P.show.playlist;
    for (let k = 1; k <= pl.length; k++) {
      const n = pl[(tk.entry + k) % pl.length];
      if (n.enabled !== false && clipById(n.clip)) { next = `Next: ${clipById(n.clip).name}`; break; }
    }
  } else if (tk.mode === "solo" && c) {
    frac = (tk.clip_t % c.duration) / c.duration; time = `${fmtTime(tk.clip_t % c.duration)} / ${fmtTime(c.duration)}`;
  }
  $("#nowBar").style.width = `${frac * 100}%`;
  $("#nowTime").textContent = time;
  $("#nowNext").textContent = next;
  const pb = tk.playback || { speed: 1, depth: 1 };
  $("#nowAdj").textContent = pb.speed !== 1 || pb.depth !== 1 ? `Adjusted: speed ${pct(pb.speed)}, depth ${pct(pb.depth)}` : "";
}
$$("[data-cmd]").forEach((b) => b.addEventListener("click", () => send(b.dataset.cmd)));

function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(cv.clientWidth * dpr), h = Math.round(cv.clientHeight * dpr);
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, cv.clientWidth, cv.clientHeight];
}
function drawGauges() {
  const cv = $("#gauges"); if (!cv.clientWidth || !P.config) return;
  const [ctx, W, H] = fitCanvas(cv);
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  const tk = P.tick, n = nAxes();
  const T = 10, B = 44, colW = W / n, bw = Math.min(46, colW * 0.42);
  const travelMax = Math.max(...P.config.motors.map((m, i) => travelOf(i)));
  const y = (mm) => T + (mm / travelMax) * (H - T - B);
  for (let i = 0; i < n; i++) {
    const m = P.config.motors[i], cx = colW * (i + 0.5), travel = travelOf(i);
    const out = tk?.out_mm?.[i] ?? 0, act = tk?.actual_mm?.[i], lim = tk?.limited?.[i], fault = nodeFault(nodeFor(i));
    ctx.fillStyle = "#eef0f2"; ctx.fillRect(cx - bw / 2, y(0), bw, y(travel) - y(0));
    ctx.fillStyle = "#4a525b"; ctx.fillRect(cx - bw / 2, y(0), bw, Math.max(0, y(out) - y(0)));
    ctx.strokeStyle = fault ? "#c62828" : lim ? "#d18a00" : "#c9cdd2"; ctx.lineWidth = fault || lim ? 3 : 1;
    ctx.strokeRect(cx - bw / 2 + 0.5, y(0) + 0.5, bw - 1, y(travel) - y(0) - 1);
    if (act !== null && act !== undefined) {
      ctx.strokeStyle = "#2f63a4"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(cx - bw / 2 - 6, y(act)); ctx.lineTo(cx + bw / 2 + 6, y(act)); ctx.stroke();
    }
    ctx.textAlign = "center"; ctx.fillStyle = "#1b1f23"; ctx.font = "600 14px 'Segoe UI', Roboto, Arial, sans-serif";
    ctx.fillText(m.name, cx, H - 24);
    ctx.font = "13px Consolas, Menlo, 'DejaVu Sans Mono', monospace"; ctx.fillStyle = "#4a525b";
    ctx.fillText(`${Math.round(act ?? out)}`, cx, H - 7);
  }
}

// ------------------------------------------------------------ axes view
function renderAxes() {
  if (!P.tick) return;
  const nodes = P.tick.nodes;
  const unassigned = nodes.filter((nd) => nd.online && nd.id === null);
  const key = P.config.motors.map((m) => m.name).join("|") + "#" + unassigned.map((n) => n.mac).join(",");
  const wrap = $("#axisCards");
  if (key !== P.cardsKey) {
    P.cardsKey = key;
    wrap.innerHTML = P.config.motors.map((m, i) => `<button class="card" data-axis="${i}">
        <div class="card-top"><span class="card-name">${esc(m.name)}</span><span class="card-id">#${i}</span></div>
        <div class="card-pos"><span data-f="pos">—</span> <small>mm</small></div>
        <div class="card-leds"><span>${led()}Link</span><span>${led()}${memoryMode() ? "Located" : "Homed"}</span><span>${led()}En</span></div>
        <div class="card-meta" data-f="meta"></div>
        <div class="card-fault" data-f="fault"></div></button>`).join("") +
      unassigned.map((nd) => `<button class="card unassigned" data-mac="${nd.mac}">
        <div class="card-top"><span class="card-name">New node</span><span class="card-id">unassigned</span></div>
        <div class="card-pos" style="font-size:14px">${esc(nd.mac)}</div>
        <div class="card-fault" style="color:#8a5a00">Tap to identify and set up</div></button>`).join("") +
      (P.config.motors.length < 16 ? `<button class="card add-card" data-add="1">
        <div class="card-name">+ Add Motor</div>
        <div class="card-fault" style="color:var(--ink-3)">Set up a new PD Stepper as M${P.config.motors.length + 1}</div></button>` : "");
  }
  $$("#axisCards .card[data-axis]").forEach((card) => {
    const i = +card.dataset.axis, nd = nodeFor(i), fault = nodeFault(nd), f = nd?.flags || [];
    card.classList.toggle("sel", i === P.selAxis);
    card.classList.toggle("fault", !!fault);
    card.querySelector('[data-f="pos"]').textContent = nd && (f.includes("homed") || !memoryMode()) ? nd.pos_mm.toFixed(1) : "—";
    const leds = card.querySelectorAll(".card-leds .led");
    leds[0].className = "led " + (nd?.online ? "on" : "bad");
    leds[1].className = "led " + (f.includes("homing") ? "warn" : f.includes("homed") ? "on" : "");
    leds[2].className = "led " + (f.includes("enabled") ? "on" : "");
    card.querySelector('[data-f="fault"]').textContent = fault;
    card.querySelector('[data-f="meta"]').textContent = nd
      ? `${nd.vbus.toFixed(1)} V · enc Δ ${nd.err_mm === null ? "—" : nd.err_mm.toFixed(1) + " mm"} · ${nd.rssi} dBm` : "";
  });
  renderAxisActions();
}
function renderAxisActions() {
  const i = P.selAxis, m = P.config.motors[i], nd = nodeFor(i), armed = !!P.tick?.armed;
  const mem = memoryMode();
  const key = `${i}|${armed}|${!!nd}|${mem}`;
  if (key === P.actionsKey) return;
  P.actionsKey = key;
  const dis = armed ? "disabled" : "";
  $("#axisActions").innerHTML = `<span class="lbl">${esc(m?.name || "")}</span>
    <button class="mid" data-act="setup">Motor Setup</button>
    <button class="mid" data-act="identify" ${nd ? "" : "disabled"}>Identify</button>
    <button class="mid" data-act="home" ${nd ? "" : "disabled"}>${mem ? "To Top" : "Home"}</button>
    <span class="sep"></span>
    <button class="mid" data-act="jog" data-mm="-50" ${dis}>▲ 50</button>
    <button class="mid" data-act="jog" data-mm="-10" ${dis}>▲ 10</button>
    <button class="mid" data-act="jog" data-mm="10" ${dis}>▼ 10</button>
    <button class="mid" data-act="jog" data-mm="50" ${dis}>▼ 50</button>
    <span class="sep"></span>
    ${mem ? "" : `<button class="mid" data-act="zero" ${dis}>Set Zero</button>`}
    <button class="mid" data-act="release" ${dis}>Release</button>
    <button class="mid" data-act="remove" ${P.config.motors.length <= 1 ? "disabled" : ""}>Remove</button>
    ${armed ? `<span class="note">Disable axes to jog, zero or release.</span>` : `<span class="note">▲ winds in · ▼ pays out (mm)</span>`}`;
}
$("#axisCards").addEventListener("click", async (ev) => {
  const card = ev.target.closest(".card"); if (!card) return;
  if (card.dataset.add) { location.href = setupUrl({ new: 1 }); return; }
  if (card.dataset.axis !== undefined) { P.selAxis = +card.dataset.axis; P.actionsKey = ""; renderAxes(); return; }
  const mac = card.dataset.mac;
  send("identify", { mac });
  const taken = (i) => (P.tick?.nodes || []).some((nd) => nd.online && nd.id === i && nd.mac !== mac);
  const body = P.config.motors.map((m, i) => `<button class="mid ${taken(i) ? "taken" : ""}" data-v="${i}">${esc(m.name)}${taken(i) ? "<small>in use</small>" : ""}</button>`).join("");
  const v = await modal(`Node ${mac} is flashing its lights. Which axis is it?`,
    [{ label: "Cancel", value: null }, { label: "Full Motor Setup…", value: "setup", cls: "primary" }], body);
  if (v === null || v === undefined) return;
  if (v === "setup") { location.href = setupUrl({ mac }); return; }
  const name = P.config.motors[+v].name;
  if (taken(+v) && !(await confirmBox(`${name} already has a node. Two nodes on one axis will both follow the same motion. Assign anyway?`, "Assign", true))) return;
  send("assign", { mac, new_id: +v }); toast(`Assigned as ${name}`); P.cardsKey = "";
});
$("#axisActions").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-act]"); if (!b || b.disabled) return;
  const i = P.selAxis, nd = nodeFor(i), name = P.config.motors[i].name;
  switch (b.dataset.act) {
    case "identify": if (nd) send("identify", { mac: nd.mac }); break;
    case "setup": location.href = setupUrl(nd ? { motor: i, mac: nd.mac } : { motor: i }); break;
    case "home": if (await confirmBox(memoryMode() ? `Move ${name} up to its taught top?` : `Home ${name}? It will wind in until it reaches its home switch.`, memoryMode() ? "To Top" : "Home")) send("home", { id: i }); break;
    case "jog": send("jog", { id: i, mm: +b.dataset.mm }); break;
    case "zero": if (await confirmBox(`Set ${name}'s current position as home (fully wound in)?`, "Set Zero")) send("set_zero", { id: i }); break;
    case "release": send("enable", { id: i, on: false }); toast(`${name} released`); break;
    case "remove": {
      const later = nAxes() - 1 - i;
      const msg = `Remove ${name}? Its board is released and its setup is discarded.` +
        (later > 0 ? ` The ${later} motor(s) after it move down one number; their boards are renumbered automatically.` : "");
      if (await confirmBox(msg, "Remove", true)) { send("remove_motor", { id: i }); P.selAxis = Math.max(0, i - 1); toast(`${name} removed`); }
      break;
    }
  }
});

// ------------------------------------------------------------ playlist view
const saveShow = (() => { let t; return () => { clearTimeout(t); t = setTimeout(() => api("PUT", "/api/show", P.show).catch((e) => toast("Save failed: " + e.message, true)), 200); }; })();
function renderPlaylist() {
  if (!P.show) return;
  const pl = P.show.playlist;
  const total = pl.filter((e) => e.enabled !== false).reduce((a, e) => a + entryLength(e), 0);
  $("#plTotal").textContent = `${pl.filter((e) => e.enabled !== false).length} of ${pl.length} on · cycle ${fmtTime(total)}`;
  $("#plRows").innerHTML = pl.map((e, i) => {
    const c = clipById(e.clip), on = e.enabled !== false;
    return `<div class="pl-row ${on ? "" : "off"}" data-i="${i}">
      <button class="tog" role="switch" aria-checked="${on}" data-tog="${i}" aria-label="Clip on/off"></button>
      <span class="pl-num">${i + 1}</span>
      <div><div class="pl-name">${esc(c?.name || "?")}</div><div class="pl-sub">${c?.type === "timeline" ? "Timeline" : "Pattern"} · ${fmtTime(c?.duration || 0)} each · ${fmtTime(entryLength(e))} total</div></div>
      <div class="stepper"><button class="mid" data-rep="${i}" data-d="-1" aria-label="Fewer repeats">−</button><div class="step-val">×${e.repeat || 1}</div><button class="mid" data-rep="${i}" data-d="1" aria-label="More repeats">+</button></div>
      <div class="pl-order"><button class="mid" data-move="${i}" data-d="-1" aria-label="Move up" ${i === 0 ? "disabled" : ""}>▲</button><button class="mid" data-move="${i}" data-d="1" aria-label="Move down" ${i === pl.length - 1 ? "disabled" : ""}>▼</button></div>
      <button class="mid" data-playfrom="${i}">Play from here</button></div>`;
  }).join("") || `<p class="hint">The playlist is empty. Build it in the full editor.</p>`;
  markPlaying();
}
function markPlaying() {
  const tk = P.tick;
  $$("#plRows .pl-row").forEach((r) => r.classList.toggle("playing", tk?.mode === "playlist" && +r.dataset.i === tk.entry % Math.max(1, P.show.playlist.length)));
}
$("#plRows").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button"); if (!b || b.disabled) return;
  const pl = P.show.playlist;
  if (b.dataset.tog !== undefined) { const e = pl[+b.dataset.tog]; e.enabled = e.enabled === false; }
  else if (b.dataset.rep !== undefined) { const e = pl[+b.dataset.rep]; e.repeat = clamp((e.repeat || 1) + +b.dataset.d, 1, 99); }
  else if (b.dataset.move !== undefined) {
    const i = +b.dataset.move, j = i + +b.dataset.d;
    if (j < 0 || j >= pl.length) return;
    [pl[i], pl[j]] = [pl[j], pl[i]];
  } else if (b.dataset.playfrom !== undefined) {
    const i = +b.dataset.playfrom, c = clipById(pl[i].clip);
    if (await confirmBox(`Play the show starting at "${c?.name}"?`, "Play")) send("play_entry", { index: i });
    return;
  } else return;
  saveShow(); renderPlaylist();
});

// ------------------------------------------------------------ adjust view
function renderAdjust() {
  const pb = P.tick?.playback || { speed: 1, depth: 1 };
  $("#speedVal").textContent = pct(pb.speed);
  $("#depthVal").textContent = pct(pb.depth);
  $$('[data-preset="speed"] .mid').forEach((b) => b.classList.toggle("cur", Math.abs(+b.dataset.v - pb.speed) < 0.001));
  $$('[data-preset="depth"] .mid').forEach((b) => b.classList.toggle("cur", Math.abs(+b.dataset.v - pb.depth) < 0.001));
}
$("#v-adjust").addEventListener("click", (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  const pb = P.tick?.playback || { speed: 1, depth: 1 };
  if (b.dataset.step) {
    const k = b.dataset.step, lo = k === "speed" ? 0.25 : 0.1, hi = k === "speed" ? 2 : 1;
    send("set_playback", { [k]: Math.round(clamp(pb[k] + +b.dataset.d, lo, hi) * 100) / 100 });
  } else if (b.parentElement?.dataset.preset) {
    send("set_playback", { [b.parentElement.dataset.preset]: +b.dataset.v });
  } else if (b.id === "btnReset") {
    send("set_playback", { speed: 1, depth: 1 }); toast("Speed and depth reset to 100%");
  }
});
const START = [{ key: "autoplay", label: "Run show at power-up" }, { key: "auto_home", label: "Home axes before running" }];
function renderStartToggles() {
  $("#startToggles").innerHTML = START.map((s) =>
    `<label><button class="tog" role="switch" aria-checked="${!!P.config[s.key]}" data-start="${s.key}" aria-label="${esc(s.label)}"></button>${esc(s.label)}</label>`).join("");
}
$("#startToggles").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-start]"); if (!b) return;
  const k = b.dataset.start;
  const r = await api("PUT", "/api/config", { [k]: !P.config[k] });
  P.config = r.config; renderStartToggles();
});

// ------------------------------------------------------------ system view
function renderSystem() {
  const h = P.host || {}, tk = P.tick;
  $$(".sim-only").forEach((b) => (b.hidden = !P.simulate));
  const homeBtn = $('[data-sys="home"]');
  if (homeBtn) homeBtn.textContent = memoryMode() ? "All Axes To Top" : "Home All Axes";
  const urls = [`http://${h.hostname || "raspberrypi"}.local:8080`, ...(h.ips || []).map((ip) => `http://${ip}:8080`)];
  $("#sysInfo").innerHTML = `<dt>Full editor</dt><dd>${urls.map(esc).join("<br>")}</dd>
    <dt>Bridge</dt><dd>${tk ? (tk.link ? esc(tk.port) + (tk.bridge ? ` · ch ${tk.bridge.channel}` : "") : "not connected") : "—"}</dd>
    <dt>Radio TX</dt><dd>${tk?.bridge ? `${tk.bridge.tx_ok} ok · ${tk.bridge.tx_fail} failed` : "—"}</dd>
    <dt>Show</dt><dd>${esc(P.show?.name || "")}</dd>
    <dt>Axes</dt><dd>${nAxes()} configured</dd>`;
}
$("#v-system").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-sys]"); if (!b) return;
  const a = b.dataset.sys;
  if (a === "setup") { location.href = setupUrl({}); return; }
  if (a === "add_motor") { location.href = setupUrl({ new: 1 }); return; }
  if (a === "remove_unused") {
    if (await confirmBox("Remove every motor that hasn't been set up and has no board reporting?", "Remove", true)) send("remove_unused");
    return;
  }
  if (a === "sim_cut" || a === "sim_cut_fail") {
    const ok = await confirmBox(a === "sim_cut" ? "SIMULATOR: cut power to every motor? Their power-loss save works." :
      "SIMULATOR: cut power to every motor, with the power-loss save FAILING? Motors that were moving lose their position.", "Cut power", true);
    if (ok) send("sim_power_cut", { save_works: a === "sim_cut" });
    return;
  }
  const msg = { autostart: "Run the startup sequence? Axes will be found, configured, homed, then the show starts.",
    home: memoryMode() ? "Move every axis up to its taught top? The show stops." : "Home every axis? The show stops and all cables wind in to their home switches.",
    push_config: "Resend mechanics settings to every axis?" }[a];
  if (!(await confirmBox(msg, b.textContent))) return;
  send(a, a === "autostart" ? {} : { id: "all" });
});

// ------------------------------------------------------------ navigation + loop
$$(".nav").forEach((b) => b.addEventListener("click", async () => {
  $$(".nav").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `v-${b.dataset.view}`));
  if (b.dataset.view === "playlist" || b.dataset.view === "system") await loadState();   // pick up laptop edits
  P.cardsKey = ""; P.actionsKey = "";
}));
document.addEventListener("contextmenu", (e) => e.preventDefault());   // long-press menus off

let lastSlow = 0, lastState = 0;
function frame(ts) {
  const view = $(".view.active").id;
  if (view === "v-run") drawGauges();
  if (ts - lastSlow > 250) {
    lastSlow = ts; renderTop();
    if (view === "v-run") renderRun();
    if (view === "v-axes") renderAxes();
    if (view === "v-adjust") renderAdjust();
    if (view === "v-playlist") markPlaying();
    if (view === "v-system") renderSystem();
  }
  if (ts - lastState > 15000 && $("#modal").hidden && view !== "v-playlist") { lastState = ts; loadState().catch(() => {}); }
  requestAnimationFrame(frame);
}
loadState().then(() => { connectWs(); requestAnimationFrame(frame); }).catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<p style="padding:20px;color:#c62828;font-size:18px">Controller not reachable: ${esc(e.message)}. Retrying…</p>`);
  setTimeout(() => location.reload(), 3000);
});
