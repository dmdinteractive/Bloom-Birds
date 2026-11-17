/* Bloom Birds touch panel, for the Pi's 7" Touch Display 2 (1280×720, landscape).
 *
 * Two views. RUN is all that is needed day to day: Run / Hold / Park, the birds' heights, and
 * the four knobs of the flap. SERVICE is for setting up a bird, reading the log, and turning
 * the screen. No typing anywhere: buttons only.
 *
 * It talks to the controller two ways: GET /api/state once (bird names, the list of knobs),
 * and the /ws socket (live state in 20 times a second, commands out). If the socket drops it
 * reconnects by itself and covers the screen until it is back.
 */
"use strict";

const P = { config: null, moves: null, host: null, tick: null, ws: null, wsOk: false, lastTick: 0,
  events: [], lastWarn: null, knobKey: "", birdKey: "" };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const led = (st) => `<span class="led ${st || ""}"></span>`;

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

// ------------------------------------------------------------ confirm box (browser dialogs would freeze a kiosk)
function confirmBox(msg, ok = "OK", danger = false) {
  return new Promise((resolve) => {
    $("#modalMsg").textContent = msg;
    $("#modalBody").innerHTML = "";
    $("#modalBtns").innerHTML = `<button class="big" data-v="0">Cancel</button><button class="big ${danger ? "danger" : "primary"}" data-v="1">${esc(ok)}</button>`;
    const m = $("#modal"); m.hidden = false;
    m.onclick = (ev) => {
      const b = ev.target.closest("button");
      if (!b && ev.target !== m) return;
      m.hidden = true; m.onclick = null; resolve(b?.dataset.v === "1");
    };
  });
}

// ------------------------------------------------------------ data
async function loadState() {
  const st = await api("GET", "/api/state");
  P.config = st.config; P.moves = st.moves; P.host = st.host; P.simulate = st.simulate;
  P.knobKey = ""; P.birdKey = "";
}
function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  P.ws = ws;
  ws.onopen = () => { P.wsOk = true; loadState().catch(() => {}); };      // the controller may have restarted
  ws.onclose = () => { P.wsOk = false; setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.type === "tick") {
      P.tick = m; P.lastTick = Date.now(); if (m.events) addEvents(m.events);
      if (P.config && m.travel.length !== P.config.motors.length && !P.reloading) {   // birds added or removed
        P.reloading = true; loadState().catch(() => {}).finally(() => { P.reloading = false; });
      }
    } else if (m.type === "events") addEvents(m.events);
    else if (m.type === "error") toast(m.msg, true);
  };
}
function addEvents(evs) {
  for (const e of evs) { P.events.push(e); if (e.level === "warn") P.lastWarn = e; }
  P.events = P.events.slice(-150);
  $("#log").innerHTML = P.events.slice().reverse().map((e) =>
    `<div class="${e.level}"><time>${new Date(e.t * 1000).toLocaleTimeString()}</time><span>${esc(e.msg)}</span></div>`).join("");
}

// ------------------------------------------------------------ birds
const nBirds = () => P.tick?.travel.length || 0;
const nameOf = (i) => P.config?.motors[i]?.name || `M${i + 1}`;
const nodeFor = (i) => (P.tick?.nodes || []).find((nd) => nd.online && nd.id === i) || (P.tick?.nodes || []).find((nd) => nd.id === i);
const isEmpty = (i) => !P.tick?.commissioned?.[i] && !nodeFor(i);      // no board yet: not a fault
function birdFault(i) {
  if (isEmpty(i)) return "";
  const nd = nodeFor(i);
  if (!nd) return "Board not detected";
  if (!nd.online) return "Offline: check its power";
  const f = nd.flags;
  if (!f.includes("power_good")) return "No power from its supply";
  if (!f.includes("tmc_ok")) return "Motor driver not responding";
  if (f.includes("overtemp")) return "Motor driver too hot";
  if (f.includes("slip")) return "Slipped: position error";
  if (f.includes("link_lost")) return "Radio link lost";
  if (!P.tick.commissioned[i] || !f.includes("calibrated")) return "Not set up: Service → Set up";
  if (!f.includes("homed")) return "Position unknown: Service → Set up → Find top only";
  return "";
}
const setupUrl = (q) => `/setup?from=panel&${new URLSearchParams(q)}`;

// ------------------------------------------------------------ top bar, state, footer
function renderTop() {
  const tk = P.tick, stale = !P.wsOk || Date.now() - P.lastTick > 3000;
  $("#offline").hidden = !stale || !P.lastTick;
  const L = [`<span class="light">${led(stale ? "bad" : "on")}Controller</span>`];
  if (tk) {
    const n = nBirds(), ready = [...Array(n).keys()].filter((i) => !isEmpty(i) && !birdFault(i)).length;
    const used = [...Array(n).keys()].filter((i) => !isEmpty(i)).length;
    const fi = [...Array(n).keys()].find((i) => birdFault(i));
    L.push(`<span class="light">${led(tk.link ? "on" : "bad")}Radio</span>`);
    L.push(`<span class="light">${led(used && ready === used ? "on" : ready ? "warn" : "bad")}Birds ready <b>${ready}/${used}</b></span>`);
    if (P.simulate) L.push(`<span class="light">${led("warn")}Simulation</span>`);

    const st = $("#state"), running = tk.armed && tk.mode === "play";
    const label = !tk.armed ? "STOPPED" : tk.mode === "play" ? "RUNNING" : tk.mode === "park" ? "PARKED AT TOP" : "HOLDING";
    st.textContent = label; st.className = "state" + (tk.armed ? "" : " stopped");
    $("#stateSub").textContent = !tk.armed ? "Motors are off. Press Run to start the birds."
      : running && !tk.fit.ok ? "Flap is set faster than the motors can follow." : "";
    $$(".cmds .big").forEach((b) => b.classList.toggle("cur", tk.armed && { run: "play", hold: "hold", park: "park" }[b.dataset.cmd] === tk.mode));

    $("#stState").textContent = tk.startup === "running" || tk.armed ? label.toLowerCase() : `startup: ${tk.startup}`;
    const exc = $("#stException");
    if (!tk.link) { exc.className = "exc bad"; exc.textContent = "Radio bridge not connected: check its USB cable"; }
    else if (fi !== undefined) { exc.className = "exc bad"; exc.textContent = `${nameOf(fi)}: ${birdFault(fi)}`; }
    else if (P.lastWarn && Date.now() / 1000 - P.lastWarn.t < 120) { exc.className = "exc warn"; exc.textContent = P.lastWarn.msg; }
    else { exc.className = "exc"; exc.textContent = "No faults"; }
  }
  $("#lights").innerHTML = L.join("");
}
$("#btnStop").onclick = () => { send("stop_all"); toast("All motors stopping"); };
$$("[data-cmd]").forEach((b) => b.addEventListener("click", async () => {
  const c = b.dataset.cmd, armed = !!P.tick?.armed;
  // Run and Park move the birds. If the motors are off, ask first, then switch them on.
  if (c !== "hold" && !armed) {
    if (!(await confirmBox(c === "run" ? "Start the birds? Keep people clear of them." : "Move every bird to its top position?", c === "run" ? "Run" : "Park"))) return;
    send("arm", { on: true });
  }
  send(c === "run" ? "play" : c);
}));

function drawGauges() {
  const cv = $("#gauges"), tk = P.tick; if (!cv.clientWidth || !tk || !P.config) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  const n = nBirds(), T = 10, B = 46, colW = W / n, bw = Math.min(60, colW * 0.45);
  for (let i = 0; i < n; i++) {
    const travel = tk.travel[i] || 1, cx = colW * (i + 0.5), y = (v) => T + (v / travel) * (H - T - B);
    const empty = isEmpty(i), fault = birdFault(i), lim = tk.limited[i], act = tk.actual_mm[i];
    ctx.fillStyle = "#eef0f2"; ctx.fillRect(cx - bw / 2, T, bw, H - T - B);
    if (!empty) { ctx.fillStyle = "#4a525b"; ctx.fillRect(cx - bw / 2, T, bw, Math.max(0, y(tk.out_mm[i]) - T)); }
    ctx.strokeStyle = fault ? "#c62828" : lim ? "#d18a00" : "#c9cdd2"; ctx.lineWidth = fault || lim ? 3 : 1;
    ctx.strokeRect(cx - bw / 2 + 0.5, T + 0.5, bw - 1, H - T - B - 1);
    if (act !== null && act !== undefined) {
      ctx.strokeStyle = "#2f63a4"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(cx - bw / 2 - 6, y(act)); ctx.lineTo(cx + bw / 2 + 6, y(act)); ctx.stroke();
    }
    ctx.textAlign = "center"; ctx.fillStyle = fault ? "#c62828" : "#1b1f23"; ctx.font = "600 16px 'Segoe UI', Roboto, Arial, sans-serif";
    ctx.fillText(nameOf(i), cx, H - 26);
    ctx.font = "14px Consolas, Menlo, 'DejaVu Sans Mono', monospace"; ctx.fillStyle = "#4a525b";
    ctx.fillText(empty ? "empty" : fault ? "fault" : `${Math.round(act ?? tk.out_mm[i])}`, cx, H - 8);
  }
}

// ------------------------------------------------------------ the four knobs
const knobFmt = (p, v) => (p.unit === "%" ? `${Math.round(v * 100)}%` : `${v.toFixed(1)} s`);
function knobStep(p, v, d) {                      // seconds per flap: fine steps when fast, coarse when slow
  if (p.key !== "period") return p.step;
  return (d < 0 ? v <= 2 : v < 2) ? 0.1 : 0.5;
}
function renderKnobs() {
  const m = P.tick?.move, def = P.moves?.[m?.type]; if (!m || !def) return;
  if (P.knobKey !== m.type) {
    P.knobKey = m.type;
    $("#knobs").innerHTML = def.params.map((p) => `<div class="knob-row"><div class="lbl">${esc(p.label)}</div>
      <div class="stepper">
        <button class="big" data-knob="${p.key}" data-d="-1" aria-label="${esc(p.label)} down">−</button>
        <div class="step-val" data-val="${p.key}"></div>
        <button class="big" data-knob="${p.key}" data-d="1" aria-label="${esc(p.label)} up">+</button>
      </div></div>`).join("");
  }
  for (const p of def.params) $(`[data-val="${p.key}"]`).textContent = knobFmt(p, m[p.key]);
  const fit = P.tick.fit, f = $("#fit");
  f.className = "fit-line" + (fit.ok ? "" : " bad");
  f.textContent = fit.ok ? `Fastest for this stroke: ${fit.fastest.toFixed(1)} s per flap.`
    : `Too fast: the birds will lag. Fastest for this stroke is ${fit.fastest.toFixed(1)} s.`;
}
$("#knobs").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-knob]"); if (!b) return;
  const m = P.tick?.move, def = P.moves?.[m?.type]; if (!m || !def) return;
  const p = def.params.find((x) => x.key === b.dataset.knob), d = +b.dataset.d, step = knobStep(p, m[p.key], d);
  const v = clamp(Math.round((m[p.key] + d * step) / step) * step, p.min, p.max);
  send("set_move", { knobs: { [p.key]: Math.round(v * 1000) / 1000 } });
});
$("#btnFastest").onclick = () => {
  const fit = P.tick?.fit; if (!fit?.fastest) return;
  send("set_move", { knobs: { period: fit.fastest } }); toast(`Set to ${fit.fastest.toFixed(1)} s per flap`);
};

// ------------------------------------------------------------ service view
function renderService() {
  const tk = P.tick; if (!tk || !P.config) return;
  const rows = [...Array(nBirds()).keys()].map((i) => {
    const nd = nodeFor(i), fault = birdFault(i), empty = isEmpty(i);
    const detail = nd ? `${nd.vbus.toFixed(1)} V · radio ${nd.rssi} dBm · top speed ${Math.round(tk.vmax[i])} mm/s${tk.speed_test[i] ? "" : " (not speed tested)"}` : "";
    return `<div class="bird-row"><b>${esc(nameOf(i))}</b>
      <div class="st ${fault ? "bad" : ""}">${empty ? "Empty position" : fault || "Ready"}<small>${esc(detail)}</small></div>
      <button class="mid" data-setup="${i}">Set up</button></div>`;
  });
  const fresh = tk.nodes.filter((nd) => nd.online && nd.id === null).map((nd) =>
    `<div class="bird-row"><b>New</b><div class="st">New board found<small>${esc(nd.mac)}</small></div>
      <button class="mid" data-mac="${nd.mac}">Set up</button></div>`);
  const html = rows.concat(fresh).join("");
  if (html !== P.birdKey) { P.birdKey = html; $("#birdList").innerHTML = html; }
  const h = P.host || {};
  $("#addr").textContent = `Moves screen from a laptop: http://${h.hostname || "bloombirds"}.local:8080` + ((h.ips || [])[0] ? ` or http://${h.ips[0]}:8080` : "");
}
$("#birdList").addEventListener("click", (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  if (b.dataset.mac) { location.href = setupUrl({ mac: b.dataset.mac }); return; }
  const i = +b.dataset.setup, nd = nodeFor(i);
  location.href = setupUrl(nd ? { motor: i, mac: nd.mac } : { motor: i });
});
$("#btnAdd").onclick = () => { location.href = setupUrl({ new: 1 }); };
$("#btnRotate").onclick = () => { window.screenRotation.flip(); toast("Screen flipped"); };
$("#btnService").onclick = () => {
  const on = $("#btnService").getAttribute("aria-pressed") !== "true";
  $("#btnService").setAttribute("aria-pressed", String(on));
  $("#btnService").textContent = on ? "Back" : "Service";
  $("#v-run").classList.toggle("active", !on); $("#v-service").classList.toggle("active", on);
  P.birdKey = "";
};
document.addEventListener("contextmenu", (e) => e.preventDefault());   // long-press menus off

// ------------------------------------------------------------ loop
let lastSlow = 0;
function frame(ts) {
  try {
    const run = $("#v-run").classList.contains("active");
    if (run) drawGauges();
    if (ts - lastSlow > 250) { lastSlow = ts; renderTop(); if (run) renderKnobs(); else renderService(); }
  } catch (e) { console.error(e); }                 // one bad frame must never stop the panel
  requestAnimationFrame(frame);
}
// If the controller isn't up yet (the Pi just booted), keep trying instead of showing an error page.
(async function start() {
  for (;;) {
    try { await loadState(); break; } catch (e) { $("#stState").textContent = "Waiting for the controller…"; await new Promise((r) => setTimeout(r, 2000)); }
  }
  connectWs(); requestAnimationFrame(frame);
})();
