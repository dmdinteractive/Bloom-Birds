/* Bloom Birds Moves screen (laptop / tablet).
 *
 * One page: run the show, set the knobs of the move, see every bird, change the few hardware
 * settings that affect speed. It talks to the controller three ways:
 *   GET /api/state   once at load: settings, the show, and the list of moves with their knobs
 *   PUT /api/config  when a setting changes
 *   /ws              live state 20 times a second in, commands out
 */
"use strict";

const S = { config: null, moves: null, tick: null, ws: null, wsOk: false, events: [], knobKey: "", birdKey: "", editing: null };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const led = (st) => `<span class="led ${st || ""}"></span>`;
const fmt = (v, d = 0) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

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
  if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify({ cmd, ...extra })); return true; }
  toast("Not connected to the controller", true); return false;
}

// ------------------------------------------------------------ data
async function loadState() {
  const st = await api("GET", "/api/state");
  S.config = st.config; S.moves = st.moves; S.simulate = st.simulate;
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
      S.tick = m; if (m.events) addEvents(m.events);
      if (S.config && m.travel.length !== S.config.motors.length && !S.reloading) {   // birds added or removed
        S.reloading = true; loadState().finally(() => { S.reloading = false; });
      }
    } else if (m.type === "events") addEvents(m.events);
    else if (m.type === "error") toast(m.msg, true);
  };
}
function addEvents(evs) {
  S.events = S.events.concat(evs).slice(-150);
  $("#log").innerHTML = S.events.slice().reverse().map((e) =>
    `<div class="${e.level}"><time>${new Date(e.t * 1000).toLocaleTimeString()}</time><span>${esc(e.msg)}</span></div>`).join("");
}

// ------------------------------------------------------------ birds: status of each motor
const nodeFor = (i) => (S.tick?.nodes || []).find((nd) => nd.online && nd.id === i) || (S.tick?.nodes || []).find((nd) => nd.id === i);
function birdFault(i) {
  const tk = S.tick, nd = nodeFor(i);
  if (!tk.commissioned[i] && !nd) return "";                    // empty position, not a fault
  if (!nd) return "Board not detected";
  if (!nd.online) return "Offline";
  const f = nd.flags;
  if (!f.includes("power_good")) return "No USB-PD power";
  if (!f.includes("tmc_ok")) return "Driver not responding";
  if (f.includes("overtemp")) return "Driver over temperature";
  if (f.includes("slip")) return "Position error (slipping?)";
  if (!tk.commissioned[i] || !f.includes("calibrated")) return "Not set up: run Motor Setup";
  if (!f.includes("homed")) return "Position unknown: Motor Setup → Find top only";
  return "";
}

// ------------------------------------------------------------ top bar and show box
const MODE = { idle: "idle", play: "running", hold: "holding", park: "parking at home" };
function renderTop() {
  const tk = S.tick;
  const L = [`<span class="light">${led(S.wsOk ? "on" : "bad")}Controller</span>`];
  if (tk) {
    const n = tk.travel.length;
    const online = tk.commissioned.filter((c, i) => nodeFor(i)?.online).length;
    const setUp = tk.commissioned.filter(Boolean).length;
    const faults = tk.commissioned.filter((c, i) => birdFault(i)).length;
    L.push(`<span class="light">${led(tk.link ? "on" : "bad")}Radio bridge</span>`);
    L.push(`<span class="light">${led(setUp && online >= setUp ? "on" : online ? "warn" : "bad")}Birds <b>${setUp}/${n}</b> set up</span>`);
    L.push(`<span class="light">${led(faults ? "bad" : "")}Faults <b>${faults}</b></span>`);
    if (S.simulate) L.push(`<span class="light">${led("warn")}Simulation</span>`);
    $("#btnEnable").setAttribute("aria-pressed", String(tk.armed));
    $("#enLed").className = "led" + (tk.armed ? " on" : "");
    $("#enText").textContent = tk.armed ? "ENABLED" : "DISABLED";
    $("#modeText").textContent = `${MODE[tk.mode] || tk.mode}${tk.startup === "running" ? "" : " · startup: " + tk.startup}`;
    $("#armNote").textContent = tk.armed ? "" : "Motors are DISABLED: the show plays on screen only. Press ENABLE to move them.";
  }
  $("#lights").innerHTML = L.join("");
}
$("#btnEnable").onclick = () => {
  const on = !S.tick?.armed;
  if (on && !confirm("Enable the motors? They will start following the show.")) return;
  send("arm", { on });
};
$("#btnStop").onclick = () => { send("stop_all"); toast("All motors stopping"); };
$$("[data-cmd]").forEach((b) => (b.onclick = () => send(b.dataset.cmd)));

function drawBirds() {
  const cv = $("#birds"), tk = S.tick; if (!cv.clientWidth || !tk) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== Math.round(W * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  const n = tk.travel.length, T = 12, B = 40, colW = W / n, bw = Math.min(54, colW * 0.4);
  for (let i = 0; i < n; i++) {
    const travel = tk.travel[i] || 1, cx = colW * (i + 0.5);
    const y = (v) => T + (v / travel) * (H - T - B);
    const empty = !tk.commissioned[i] && !nodeFor(i), fault = birdFault(i), lim = tk.limited[i];
    ctx.fillStyle = "#eef0f2"; ctx.fillRect(cx - bw / 2, T, bw, H - T - B);
    if (!empty) { ctx.fillStyle = "#4a525b"; ctx.fillRect(cx - bw / 2, T, bw, Math.max(0, y(tk.out_mm[i]) - T)); }
    // the range the flap covers
    const m = tk.move;
    ctx.strokeStyle = "#2e9e44"; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    for (const v of [m.top, m.bottom]) { const yy = T + v * (H - T - B); ctx.beginPath(); ctx.moveTo(cx - bw / 2 - 8, yy); ctx.lineTo(cx + bw / 2 + 8, yy); ctx.stroke(); }
    ctx.setLineDash([]);
    ctx.strokeStyle = fault ? "#c62828" : lim ? "#d18a00" : "#c9cdd2"; ctx.lineWidth = fault || lim ? 3 : 1;
    ctx.strokeRect(cx - bw / 2 + 0.5, T + 0.5, bw - 1, H - T - B - 1);
    const act = tk.actual_mm[i];
    if (act !== null && act !== undefined) {
      ctx.strokeStyle = "#2f63a4"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(cx - bw / 2 - 6, y(act)); ctx.lineTo(cx + bw / 2 + 6, y(act)); ctx.stroke();
    }
    ctx.textAlign = "center"; ctx.fillStyle = "#1b1f23"; ctx.font = "600 13px 'Segoe UI', Roboto, Arial, sans-serif";
    ctx.fillText(S.config.motors[i]?.name || `M${i + 1}`, cx, H - 22);
    ctx.font = "12px Consolas, Menlo, monospace"; ctx.fillStyle = "#4a525b";
    ctx.fillText(empty ? "empty" : `${Math.round(act ?? tk.out_mm[i])} ${tk.units[i]}`, cx, H - 6);
  }
}

// ------------------------------------------------------------ the move and its knobs
// Percent knobs are stored as 0..1 and shown as 0..100.
const shown = (p, v) => (p.unit === "%" ? Math.round(v * 100) : v);
const stored = (p, v) => (p.unit === "%" ? v / 100 : v);
function renderMove() {
  const tk = S.tick; if (!tk || !S.moves) return;
  const m = tk.move, def = S.moves[m.type];
  if (S.knobKey !== m.type) {
    S.knobKey = m.type;
    $("#moveTitle").textContent = `Move: ${def.label}`;
    $("#moveHelp").textContent = def.help;
    $("#knobs").innerHTML = def.params.map((p) => {
      // a slider that goes to 120 s is useless for fine work: the slider covers 0.5-30 s, the box takes anything
      const hi = p.key === "period" ? 30 : shown(p, p.max), lo = p.key === "period" ? 0.5 : shown(p, p.min);
      const step = p.unit === "%" ? 1 : 0.1;
      return `<div class="knob" data-key="${p.key}">
        <label for="k_${p.key}">${esc(p.label)}</label>
        <input type="range" id="k_${p.key}" min="${lo}" max="${hi}" step="${step}">
        <span class="num"><input type="number" min="${shown(p, p.min)}" max="${shown(p, p.max)}" step="${step}" aria-label="${esc(p.label)}">${esc(p.unit)}</span>
        <div class="help">${esc(p.help)}</div></div>`;
    }).join("");
  }
  for (const p of def.params) {
    if (S.editing === p.key) continue;                          // don't fight the user's hand
    const row = $(`.knob[data-key="${p.key}"]`), v = shown(p, m[p.key]);
    row.querySelector("input[type=range]").value = v;
    row.querySelector("input[type=number]").value = v;
  }
  const fit = tk.fit, f = $("#fit");
  const worst = fit.birds.map((b, i) => ({ ...b, i })).filter((b) => tk.commissioned[b.i] && b.fastest >= fit.fastest - 0.11)[0];
  const who = worst ? ` (${esc(S.config.motors[worst.i]?.name)}'s ${worst.limit} limit sets this)` : "";
  f.className = "fit" + (fit.ok ? "" : " bad");
  f.innerHTML = (fit.ok
    ? `Fastest the motors can flap this far: <b>${fit.fastest.toFixed(1)} s</b>${who}.`
    : `<b>Too fast for the motors.</b> They will lag behind and the flap will look smaller. Fastest for this stroke: <b>${fit.fastest.toFixed(1)} s</b>${who}.`)
    + `<button class="btn small" id="btnFastest">Set to fastest</button>`;
}
$("#knobs").addEventListener("input", (ev) => {
  const row = ev.target.closest(".knob"); if (!row) return;
  const def = S.moves[S.tick.move.type], p = def.params.find((x) => x.key === row.dataset.key);
  const v = parseFloat(ev.target.value); if (!isFinite(v)) return;
  S.editing = p.key; clearTimeout(S.editT); S.editT = setTimeout(() => (S.editing = null), 800);
  // keep the slider and the number box showing the same value
  for (const el of row.querySelectorAll("input")) if (el !== ev.target) el.value = v;
  send("set_move", { knobs: { [p.key]: stored(p, v) } });
});
$("#fit").addEventListener("click", (ev) => {
  if (ev.target.id !== "btnFastest") return;
  S.editing = null;
  send("set_move", { knobs: { period: S.tick.fit.fastest } });
});

function renderBirds() {
  const tk = S.tick; if (!tk) return;
  const rows = tk.travel.map((tr, i) => {
    const nd = nodeFor(i), empty = !tk.commissioned[i] && !nd, fault = birdFault(i), u = tk.units[i], st = tk.speed_test[i];
    const speed = `${fmt(tk.vmax[i])} ${u}/s <span class="sub">(${tk.rpm_max[i]} RPM)</span>`;
    const src = st ? `speed test ${esc(st.at)}${st.limit === "motor" ? ": motor's limit" : st.limit === "test" ? ": never slipped" : ""}` : "default, not tested yet";
    const q = nd ? `motor=${i}&mac=${encodeURIComponent(nd.mac)}` : `motor=${i}`;
    return `<tr><td><b>${esc(S.config.motors[i]?.name || "M" + (i + 1))}</b></td>
      <td class="${fault ? "fault" : ""}">${empty ? "Empty position" : fault || "Ready"}</td>
      <td class="num">${empty ? "—" : fmt(tr)} ${empty ? "" : u}</td>
      <td class="num">${speed}<div class="sub">${src}</div></td>
      <td class="num">${fmt(tk.fit.birds[i]?.fastest, 1)} s</td>
      <td class="num">${nd ? `${fmt(nd.vbus, 1)} V · ${nd.rssi} dBm` : "—"}</td>
      <td><a class="btn small" href="/setup?from=editor&${q}">Setup / speed test</a>
        ${tk.travel.length > 1 ? `<button class="btn small" data-remove="${i}">Remove</button>` : ""}</td></tr>`;
  }).join("");
  const html = `<tr><th>Bird</th><th>Status</th><th>Travel</th><th>Top speed</th><th>Fastest flap</th><th>Supply · radio</th><th></th></tr>${rows}`;
  if (html !== S.birdKey) { S.birdKey = html; $("#birdTable").innerHTML = html; }
}
$("#birdTable").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-remove]"); if (!b) return;
  const i = +b.dataset.remove, name = S.config.motors[i]?.name;
  if (confirm(`Remove ${name}? Its board is released and its setup is discarded. Birds after it move down one number.`)) send("remove_motor", { id: i });
});

// ------------------------------------------------------------ settings: only what matters day to day
// Supply voltage (20 V) and the driver mode (quiet when slow, strong when fast) are already at
// their best values in config.json and are left alone here.
const SETTINGS = [
  { key: "run_current_pct", label: "Motor current", unit: "%", min: 10, max: 100, step: 5,
    help: "More current is more torque, so more speed before the motor slips, and more heat. Run the speed test again after changing it." },
];
function renderSettings() {
  const mech = S.config.mechanics;
  $("#settings").innerHTML = SETTINGS.map((s) => `<div class="set"><label for="s_${s.key}">${esc(s.label)}</label>
      <span>${s.options
        ? `<select id="s_${s.key}" data-set="${s.key}">${s.options.map((o) => `<option value="${o}" ${String(mech[s.key]) === String(o) ? "selected" : ""}>${esc(s.labels?.[o] || o)}</option>`).join("")}</select>`
        : `<input id="s_${s.key}" data-set="${s.key}" type="number" min="${s.min}" max="${s.max}" step="${s.step}" value="${mech[s.key]}">`} ${esc(s.unit || "")}</span>
      <span class="help">${esc(s.help)}</span></div>`).join("") +
    `<div class="set"><label for="s_autoplay">Run show at power-up</label>
      <span><input id="s_autoplay" data-top="autoplay" type="checkbox" ${S.config.autoplay ? "checked" : ""} style="width:auto"></span><span class="help"></span></div>`;
}
$("#settings").addEventListener("change", async (ev) => {
  const el = ev.target;
  try {
    let body;
    if (el.dataset.top) body = { [el.dataset.top]: el.checked };
    else {
      const s = SETTINGS.find((x) => x.key === el.dataset.set);
      let v = s.options && typeof s.options[0] === "string" ? el.value : Number(el.value);
      if (!s.options) v = Math.min(s.max, Math.max(s.min, v));
      body = { mechanics: { [s.key]: v } };
    }
    S.config = (await api("PUT", "/api/config", body)).config;
    renderSettings(); toast("Saved and sent to the motors");
  } catch (e) { toast("Not saved: " + e.message, true); }
});

// ------------------------------------------------------------ loop
let lastSlow = 0;
function frame(ts) {
  drawBirds();
  if (ts - lastSlow > 200) { lastSlow = ts; renderTop(); renderMove(); renderBirds(); }
  requestAnimationFrame(frame);
}
loadState().then(() => { connectWs(); requestAnimationFrame(frame); }).catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<p style="padding:20px;color:#c62828">Controller not reachable: ${esc(e.message)}. Retrying…</p>`);
  setTimeout(() => location.reload(), 3000);
});
