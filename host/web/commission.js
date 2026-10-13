/* Motor Setup wizard: introduce a new PD Stepper, or re-find the top of one that lost its position.
 *
 * The wizard's state lives in the controller (engine.wizard) and arrives with every /ws tick, so
 * the laptop editor and the touch panel always show the same step. This page only renders that
 * state and sends wiz_* commands. No text entry anywhere (works in kiosk mode).
 *
 * URL options: /setup?from=panel|editor&mac=<board>&motor=<0-based>&recover=1
 */
"use strict";

const C = { config: null, tick: null, ws: null, wsOk: false, selMac: null, selMotor: null,
  finished: null, sawWizard: false, key: "" };
const Q = new URLSearchParams(location.search);
const FROM = Q.get("from") === "editor" ? "/" : "/panel";
if (Q.get("mac")) C.selMac = Q.get("mac");
if (Q.get("motor") !== null && Q.get("motor") !== "") C.selMotor = +Q.get("motor");
C.wantNew = Q.get("new") === "1";          // opened from an "Add Motor" button

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const led = (st) => `<span class="led ${st || ""}"></span>`;
const mm = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const mname = (i) => C.config?.motors[i]?.name || `M${i + 1}`;

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return r.json();
}
function toast(msg, warn = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (warn ? " warn" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), 2500);
}
function send(cmd, extra = {}) {
  if (C.ws && C.ws.readyState === 1) { C.ws.send(JSON.stringify({ cmd, ...extra })); return true; }
  toast("Not connected to the controller", true); return false;
}
function modal(msg, buttons) {
  return new Promise((resolve) => {
    $("#modalMsg").textContent = msg; $("#modalBody").innerHTML = "";
    $("#modalBtns").innerHTML = buttons.map((b, i) => `<button class="big ${b.cls || ""}" data-i="${i}">${esc(b.label)}</button>`).join("");
    const m = $("#modal"); m.hidden = false;
    m.onclick = (ev) => {
      const b = ev.target.closest("button[data-i]");
      if (b) { m.hidden = true; resolve(buttons[+b.dataset.i].value); }
      else if (ev.target === m) { m.hidden = true; resolve(null); }
    };
  });
}

// ------------------------------------------------------------ connection
async function loadConfig() { C.config = (await api("GET", "/api/state")).config; }
function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  C.ws = ws;
  ws.onopen = () => { C.wsOk = true; };
  ws.onclose = () => { C.wsOk = false; setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "tick") onTick(m);
    else if (m.type === "error") toast(m.msg, true);
  };
}

// ------------------------------------------------------------ steps
const FULL = [
  ["choose", "Choose motor"], ["direction", "Direction"], ["encoder", "Encoder"],
  ["top", "Teach top"], ["bottom", "Teach bottom"], ["test", "Test"], ["finish", "Save"],
];
const RECOVER = [["choose", "Choose motor"], ["top", "Find top"], ["finish", "Save"]];

function stepsFor(w) { return w?.recover ? RECOVER : FULL; }

function renderSteps(w) {
  const list = stepsFor(w), cur = w ? w.step : "choose";
  const idx = list.findIndex(([k]) => k === cur);
  const canGo = (k, i) => w && !w.busy && i < idx && i > 0 && (w.recover ? k === "top" : ["direction", "encoder", "top", "bottom"].includes(k));
  $("#steps").innerHTML = list.map(([k, label], i) =>
    `<button class="step ${i < idx ? "done" : ""} ${i === idx ? "current" : ""} ${canGo(k, i) ? "can-go" : ""}" data-goto="${canGo(k, i) ? k : ""}">
      <span class="num">${i < idx ? "✓" : i + 1}</span><span>${label}</span></button>`).join("") +
    `<p class="sub">${w ? (w.recover ? "Re-finding the top keeps the bottom that was taught before." : "Tap a finished step to go back to it.") : "The show stops while a motor is being set up."}</p>`;
}
$("#steps").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-goto]"); if (!b || !b.dataset.goto) return;
  send("wiz_goto", { step: b.dataset.goto });
});

const JOG = `
  <div class="jog">
    <div class="jog-col"><div class="jog-h">Up (wind in)</div>
      <button class="big" data-jog="-50"><span class="gl">▲▲</span>Up 50 mm</button>
      <button class="big" data-jog="-10"><span class="gl">▲</span>Up 10 mm</button>
      <button class="big" data-jog="-1"><span class="gl">△</span>Up 1 mm</button></div>
    <div class="jog-col"><div class="jog-h">Down (pay out)</div>
      <button class="big" data-jog="50"><span class="gl">▼▼</span>Down 50 mm</button>
      <button class="big" data-jog="10"><span class="gl">▼</span>Down 10 mm</button>
      <button class="big" data-jog="1"><span class="gl">▽</span>Down 1 mm</button></div>
    <button class="big stop jog-stop" data-act="stop"><span class="sq"></span>Stop moving</button>
  </div>`;

function msgBox(w) {
  if (!w) return "";
  if (w.error) return `<div class="msg err">${esc(w.error)}</div>`;
  return `<div class="msg ${w.busy ? "busy" : ""}">${esc(w.msg)}</div>`;
}

function stepHtml(w) {
  const n = w?.node || {};
  const moving = n.moving || w?.busy;
  if (!w) return C.finished ? doneHtml() : chooseHtml();
  const title = (t) => `<div class="step-title">${esc(mname(w.motor))}: ${t}</div>`;
  switch (w.step) {
    case "direction":
      return title("Direction") + `
        <div class="step-text">Make sure the area under <b>${esc(mname(w.motor))}</b> is clear. Press <b>Test move</b>:
          the motor moves 10 mm in the direction the software calls <b>down</b>. Watch the piece.</div>
        ${msgBox(w)}
        <div class="step-actions">
          ${!w.dir_tested || w.busy ? `<button class="big primary" data-act="dir_test" ${moving ? "disabled" : ""}>Test move (10 mm)</button>` :
            `<button class="big go" data-act="lowered" ${moving ? "disabled" : ""}>Yes, it lowered</button>
             <button class="big" data-act="raised" ${moving ? "disabled" : ""}>No, it went up</button>
             <button class="mid" data-act="dir_test" ${moving ? "disabled" : ""}>Move again</button>`}
        </div>`;
    case "encoder":
      return title("Encoder check") + `
        <div class="step-text">The controller moves the motor 20 mm down and back while it compares the steps sent with what
          the encoder magnet reports. If the encoder counts backwards it is corrected automatically.</div>
        ${msgBox(w)}
        <div class="step-actions"><button class="big primary" data-act="encoder" ${moving ? "disabled" : ""}>${w.error ? "Try again" : "Check encoder"}</button></div>`;
    case "top":
      return title(w.recover ? "Find the top" : "Teach the top") + `
        <div class="step-text">Jog the piece <b>up to its highest safe position</b>, the spot it should stop at when fully wound in.
          Go slowly with the 1 mm buttons for the last part, then press <b>Set as top</b>. This becomes 0 mm.
          ${w.recover ? "<br>Use the same spot as before: the bottom limit is measured from it." : ""}</div>
        ${JOG}
        ${msgBox(w)}
        <div class="step-actions"><button class="big go" data-act="top" ${moving ? "disabled" : ""}>Set as top</button></div>`;
    case "bottom":
      return title("Teach the bottom") + `
        <div class="step-text">Jog <b>down to the lowest safe position</b>. The motor will never be sent below it.
          The readout on the right shows how far below the top you are.</div>
        ${JOG}
        ${msgBox(w)}
        <div class="step-actions"><button class="big go" data-act="bottom" ${moving || (n.pos_mm ?? 0) < 20 ? "disabled" : ""}>Set as bottom</button>
          <span class="hint">${(n.pos_mm ?? 0) < 20 ? "Go at least 20 mm below the top." : ""}</span></div>`;
    case "test":
      return title("Test") + `
        <div class="step-text">The motor now moves slowly to the <b>middle</b>, the <b>bottom</b> (${mm(w.bottom_mm, 0)} mm) and back to the
          <b>top</b>, staying inside the limits you taught. Watch that it stops at the right places.</div>
        ${msgBox(w)}
        <div class="step-actions"><button class="big primary" data-act="test" ${moving ? "disabled" : ""}>Run test moves</button>
          <button class="mid" data-act="skip_test" ${moving ? "disabled" : ""}>Skip</button></div>`;
    case "finish": {
      const m = C.config.motors[w.motor] || {};
      const rows = w.recover
        ? [["Motor", mname(w.motor)], ["Board", w.mac], ["Top", "re-taught"], ["Bottom", `kept (${mm(C.tick?.travel?.[w.motor], 0)} mm)`]]
        : [["Motor", mname(w.motor)], ["Board", w.mac], ["Motor direction", m.invert_dir ? "reversed" : "normal"],
           ["Encoder direction", m.invert_encoder ? "reversed" : "normal"], ["Travel (top to bottom)", `${mm(w.bottom_mm, 0)} mm`],
           ["Test moves", w.test_done ? "done" : "skipped"]];
      return title("Save") + `
        <div class="step-text">Check the summary and save. The motor will remember its position from now on, including
          across power cuts, and the show can use it.</div>
        <table class="summary">${rows.map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join("")}</table>
        ${msgBox(w)}
        <div class="step-actions"><button class="big go" data-act="finish" ${moving ? "disabled" : ""}>Save setup</button></div>`;
    }
  }
  return "";
}

function nodeState(nd) {
  const f = nd.flags;
  if (nd.id === null) return ["warn", "New board (not assigned)"];
  if (!f.includes("calibrated")) return ["warn", "Not set up"];
  if (f.includes("pos_lost") || !f.includes("homed")) return ["bad", "Position unknown"];
  return ["on", C.tick.commissioned?.[nd.id] ? "Set up" : "Top taught"];
}

function chooseHtml() {
  const nodes = (C.tick?.nodes || []).filter((nd) => nd.online)
    .sort((a, b) => (a.id ?? -1) - (b.id ?? -1));
  if (C.selMac && !nodes.some((nd) => nd.mac === C.selMac)) C.selMac = null;
  if (!C.selMac) {   // preselect: a new board, else a lost one
    const pick = nodes.find((nd) => nd.id === null) || nodes.find((nd) => nodeState(nd)[0] !== "on");
    if (pick) C.selMac = pick.mac;
  }
  const sel = nodes.find((nd) => nd.mac === C.selMac);
  const n = C.config.motors.length;
  if (C.wantNew && C.selMotor === null && (!sel || sel.id === null)) C.selMotor = n;
  if (sel && C.selMotor === null && sel.id !== null) C.selMotor = sel.id;
  const isNew = C.selMotor === n;
  const holder = (i) => nodes.find((nd) => nd.id === i && nd.mac !== C.selMac);
  const comm = C.tick?.commissioned || [];
  const lost = sel && C.selMotor === sel.id && nodeState(sel)[0] === "bad";
  const canRecover = !isNew && (sel && C.selMotor !== null && comm[C.selMotor] && sel.flags.includes("calibrated") || lost);
  return `
    <div class="step-title">Choose the motor to set up</div>
    <div class="sec-row"><span class="section-h">1. Board</span>
      <button class="mid" data-act="identify" ${sel ? "" : "disabled"}>Identify selected board (flash LEDs)</button></div>
    <div class="pick-grid">${nodes.map((nd) => {
      const [st, label] = nodeState(nd);
      return `<button class="pick ${nd.mac === C.selMac ? "sel" : ""} ${nd.id === null ? "new" : ""}" data-mac="${esc(nd.mac)}">
        <span class="p-name">${nd.id === null ? "New board" : esc(mname(nd.id))}</span>
        <span class="p-mac">${esc(nd.mac)}</span>
        <span class="p-state">${led(st)}${esc(label)}</span></button>`;
    }).join("") || `<div class="hint">No boards are reporting. Check power and the bridge.</div>`}</div>
    <div class="section-h">2. Motor position it drives</div>
    <div class="slot-grid">${Array.from({ length: n }, (_, i) => {
      const h = holder(i);
      return `<button class="mid ${C.selMotor === i ? "sel" : ""} ${h ? "taken" : ""}" data-slot="${i}">${esc(mname(i))}
        <small>${comm[i] ? "set up" : h ? "assigned" : "empty"}</small></button>`;
    }).join("")}${n < 16 ? `<button class="mid add ${isNew ? "sel" : ""}" data-slot="${n}">+ New motor<small>becomes M${n + 1}</small></button>` : ""}</div>
    ${C.selMotor !== null && holder(C.selMotor) ? `<div class="msg">${esc(mname(C.selMotor))} is currently driven by board ${esc(holder(C.selMotor).mac)}. Starting setup moves it to the selected board.</div>` : ""}
    <div class="spacer"></div>
    <div class="step-actions sticky">
      <button class="big go" data-act="start" ${sel && C.selMotor !== null ? "" : "disabled"}>${isNew ? `Add M${n + 1} and start setup` : "Start full setup"}</button>
      ${canRecover ? `<button class="big primary" data-act="recover">Find top only</button>` : ""}
      <span class="hint">${canRecover ? "Find top only: for a motor that lost its position. Keeps its bottom limit." : ""}</span>
    </div>`;
}

function doneHtml() {
  const f = C.finished;
  return `<div class="step-title">${esc(mname(f.motor))} is set up</div>
    <div class="step-text">${f.recover ? "The top was re-found. The motor knows its position again." :
      `Travel ${mm(C.tick?.travel?.[f.motor], 0)} mm. The motor remembers its position from now on.`}
      The show is stopped. Press <b>ENABLE</b> on the panel or editor when you are ready to run it.</div>
    <div class="step-actions"><button class="big primary" data-act="another">Set up another motor</button>
      <button class="big" data-act="exit">Back to ${FROM === "/" ? "editor" : "panel"}</button></div>`;
}

$("#stepBox").addEventListener("click", async (ev) => {
  const pick = ev.target.closest("[data-mac]");
  if (pick) {
    C.selMac = pick.dataset.mac;
    const nd = C.tick.nodes.find((x) => x.mac === C.selMac);
    C.selMotor = nd?.id ?? (C.wantNew ? C.config.motors.length : null);
    C.key = ""; render(); return;
  }
  const slot = ev.target.closest("[data-slot]");
  if (slot) { C.selMotor = +slot.dataset.slot; C.key = ""; render(); return; }
  const jog = ev.target.closest("[data-jog]");
  if (jog) { send("wiz_jog", { mm: +jog.dataset.jog }); return; }
  const b = ev.target.closest("[data-act]"); if (!b || b.disabled) return;
  const a = b.dataset.act;
  if (a === "identify") send("identify", { mac: C.selMac });
  else if (a === "start" || a === "recover") {
    const armed = C.tick?.armed;
    if (armed && !(await modal("Setting up a motor stops the show. Continue?", [{ label: "Cancel", value: false }, { label: "Stop show and continue", value: true, cls: "primary" }]))) return;
    C.finished = null;
    send("wiz_start", { mac: C.selMac, motor: C.selMotor, recover: a === "recover" });
  }
  else if (a === "stop") send("wiz_stop");
  else if (a === "dir_test") send("wiz_dir_test");
  else if (a === "lowered") send("wiz_dir_answer", { lowered: true });
  else if (a === "raised") send("wiz_dir_answer", { lowered: false });
  else if (a === "encoder") send("wiz_encoder");
  else if (a === "top") send("wiz_top");
  else if (a === "bottom") send("wiz_bottom");
  else if (a === "test") send("wiz_test");
  else if (a === "skip_test") send("wiz_goto", { step: "finish" });
  else if (a === "finish") { C.finished = { motor: C.tick.wizard.motor, recover: C.tick.wizard.recover }; send("wiz_finish"); }
  else if (a === "another") { C.finished = null; C.selMac = null; C.selMotor = null; C.wantNew = true; C.key = ""; render(); }
  else if (a === "exit") location.href = FROM;
});

$("#btnStop").addEventListener("click", () => { send("wiz_stop"); send("stop_all"); });
$("#btnExit").addEventListener("click", async () => {
  if (C.tick?.wizard) {
    const ok = await modal("Leave Motor Setup? The steps done so far are not saved; the motor keeps its previous setup.",
      [{ label: "Stay", value: false }, { label: "Cancel setup and leave", value: true, cls: "danger" }]);
    if (!ok) return;
    send("wiz_cancel");
    setTimeout(() => (location.href = FROM), 300);
  } else location.href = FROM;
});

// ------------------------------------------------------------ readout (right column)
function renderReadout(w) {
  const ro = $("#readout");
  if (!w) {
    ro.innerHTML = `<div class="ro-label">Motor Setup</div>
      <div class="step-text" style="font-size:14px">Run this for every new or replaced PD Stepper. It sets the motor direction, checks the
        encoder, and teaches the top and bottom. Without home switches, the motors use the taught top and the encoder to
        know where they are after a power cut.</div>`;
    return;
  }
  const n = w.node || {};
  const bottom = w.bottom_mm ?? (w.recover ? C.tick?.travel?.[w.motor] : null);
  ro.innerHTML = `
    <div><div class="ro-name">${esc(mname(w.motor))}</div><div class="ro-mac">${esc(w.mac)}</div></div>
    <div><div class="ro-label">${n.homed ? "Below top" : "Position"}</div>
      ${n.homed ? `<div class="ro-pos">${mm(n.pos_mm)} <small>mm</small></div>` :
        `<div class="ro-wait">Known once the top is taught</div>`}</div>
    <div class="ro-rows">
      <span>Encoder</span><span class="v">${mm(n.enc_mm)} mm</span>
      <span>Magnet</span><span class="v">${esc(n.magnet || "—")}</span>
      <span>Bottom</span><span class="v">${bottom ? mm(bottom, 0) + " mm" : "—"}</span>
    </div>
    <div class="ro-leds">
      <span>${led(n.online ? "on" : "bad")}Link</span>
      <span>${led(n.moving ? "warn" : "")}Moving</span>
      <span>${led(n.calibrated ? "on" : "")}Top taught</span>
      <span>${led(n.limits ? "on" : "")}Limits on</span>
    </div>
    ${w.sim_drop_mm !== undefined ? `<div class="sim-hint"><b>Simulator:</b> the load is really ${mm(w.sim_drop_mm)} mm below its physical top.
      Use this in place of watching the piece.</div>` : ""}
    ${n.homed ? `<canvas id="gauge"></canvas>` : ""}`;
  if (n.homed) drawGauge(n.pos_mm, bottom, w.sim_drop_mm);
}

function drawGauge(pos, bottom, truth) {
  const cv = $("#gauge"); if (!cv || !cv.clientWidth) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = cv.clientWidth, H = cv.clientHeight, T = 18, B = 14;
  const span = Math.max(100, bottom || 0, (pos || 0) * 1.1, (truth || 0) * 1.1);
  const lo = Math.min(0, (pos || 0), (truth || 0)) - 20;
  const y = (v) => T + ((v - lo) / (span - lo)) * (H - T - B);
  ctx.font = "11px Consolas, Menlo, monospace"; ctx.fillStyle = "#6d757e";
  const cx = W / 2;
  ctx.strokeStyle = "#c9cdd2"; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(cx, T); ctx.lineTo(cx, H - B); ctx.stroke(); ctx.setLineDash([]);
  const mark = (v, label, col) => { ctx.fillStyle = col; ctx.fillRect(cx - 40, y(v) - 1, 80, 2); ctx.fillText(label, cx + 46, y(v) + 4); };
  mark(0, "top 0", "#4a525b");
  if (bottom) mark(bottom, `bottom ${Math.round(bottom)}`, "#4a525b");
  if (truth !== undefined && truth !== null) {   // simulator only: where the load really is
    ctx.fillStyle = "#d18a00"; ctx.beginPath(); ctx.arc(cx - 52, y(truth), 4, 0, 7); ctx.fill();
    ctx.fillText("sim", cx - 82, y(truth) + 4);
  }
  if (pos !== undefined && pos !== null) {
    ctx.strokeStyle = "#2a3036"; ctx.beginPath(); ctx.moveTo(cx, T - 10); ctx.lineTo(cx, y(pos)); ctx.stroke();
    ctx.fillStyle = "#2a3036"; ctx.fillRect(cx - 9, y(pos) - 5, 18, 10);
  }
}

// ------------------------------------------------------------ main render
function onTick(t) {
  C.tick = t;
  const w = t.wizard;
  if (w) C.sawWizard = true;
  if (!w && C.sawWizard && !C.finished) C.sawWizard = false;  // cancelled elsewhere
  render();
}

function render() {
  const t = C.tick; if (!t || !C.config) return;
  const w = t.wizard;
  $("#lights").innerHTML =
    `<span class="light">${led(C.wsOk ? "on" : "bad")}Controller</span>` +
    `<span class="light">${led(t.bridge?.online ? "on" : "bad")}Bridge</span>` +
    `<span class="light">${led(w ? (w.node?.online ? "on" : "bad") : "")}Board</span>` +
    `<span class="light">${led(t.armed ? "warn" : "")}${t.armed ? "Show running" : "Show stopped"}</span>` +
    (t.sim ? `<span class="light">${led("warn")}Simulation</span>` : "");
  renderSteps(w);
  // rebuild the step panel only when something that changes it changed (keeps taps reliable)
  const n = w?.node || {};
  const key = JSON.stringify(w ? [w.step, w.busy, w.msg, w.error, w.dir_tested, w.test_done, w.bottom_mm, !!n.moving,
    (n.pos_mm ?? 0) >= 20, C.config.motors[w.motor]] :
    [C.finished, C.selMac, C.selMotor, (t.nodes || []).filter((x) => x.online).map((x) => [x.mac, x.id, nodeState(x)[1]]), t.commissioned]);
  if (key !== C.key) { C.key = key; $("#stepBox").innerHTML = stepHtml(w); }
  renderReadout(w);
  $("#statusLeft").textContent = w ? (w.error || w.msg || `Setting up ${mname(w.motor)}`) : (t.startup || "");
  $("#statusRight").textContent = t.port || "";
}

(async function init() {
  await loadConfig();
  connectWs();
  // the config changes when a step reverses a direction; refresh it now and then
  setInterval(async () => {
    const before = JSON.stringify(C.config.motors);
    await loadConfig();
    if (JSON.stringify(C.config.motors) !== before) { C.key = ""; render(); }
  }, 3000);
})();
