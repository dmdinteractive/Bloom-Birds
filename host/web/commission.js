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
async function loadConfig() {
  const st = await api("GET", "/api/state");
  C.config = st.config; C.kinds = st.kinds; C.sizePresets = st.size_presets;
}
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
// The step list comes from the controller (it depends on the mechanism: continuous rotation has
// no ends to teach). Labels use each mechanism's own words: top/bottom, home/far end, start/end angle.
const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : t);
function stepLabel(k, w) {
  if (k === "choose") return "Choose motor";
  if (k === "mechanism") return "Mechanism";
  if (k === "direction") return "Direction";
  if (k === "encoder") return "Encoder";
  if (k === "top") return (w?.recover ? "Find " : "Teach ") + (w?.home || "top");
  if (k === "bottom") return "Teach " + (w?.far || "bottom");
  if (k === "test") return "Test";
  if (k === "speed") return "Speed test";
  return "Save";
}
function stepsFor(w) {
  const keys = ["choose", ...(w?.steps || ["mechanism", "direction", "encoder", "top", "bottom", "test", "finish"])];
  return keys.map((k) => [k, stepLabel(k, w)]);
}

function renderSteps(w) {
  const list = stepsFor(w), cur = w ? w.step : "choose";
  const idx = list.findIndex(([k]) => k === cur);
  const canGo = (k, i) => w && !w.busy && i < idx && i > 0 && ["mechanism", "direction", "encoder", "top", "bottom"].includes(k);
  $("#steps").innerHTML = list.map(([k, label], i) =>
    `<button class="step ${i < idx ? "done" : ""} ${i === idx ? "current" : ""} ${canGo(k, i) ? "can-go" : ""}" data-goto="${canGo(k, i) ? k : ""}">
      <span class="num">${i < idx ? "✓" : i + 1}</span><span>${esc(label)}</span></button>`).join("") +
    `<p class="sub">${w ? (w.recover ? `Re-finding the ${esc(w.home)} keeps the ${esc(w.far)} that was taught before.` : "Tap a finished step to go back to it.") : "The show stops while a motor is being set up."}</p>`;
}
$("#steps").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-goto]"); if (!b || !b.dataset.goto) return;
  send("wiz_goto", { step: b.dataset.goto });
});

// jog pad: negative = toward home, positive = toward the far end, in the motor's units
const JOG_WORDS = {
  spool: ["Up (wind in)", "Down (pay out)", "Up", "Down"],
  linear: ["Toward home", "Toward far end", "Back", "Out"],
  rotary: ["Toward start angle", "Toward end angle", "Back", "Turn"],
  continuous: ["Backward", "Forward", "Back", "Fwd"],
};
function jogHtml(w) {
  const [hA, hB, a, b] = JOG_WORDS[w.kind] || JOG_WORDS.spool;
  const st = w.jog_steps || [50, 10, 1], u = w.unit || "mm";
  const glyph = ["▲▲", "▲", "△"], glyphB = ["▼▼", "▼", "▽"];
  return `<div class="jog">
    <div class="jog-col"><div class="jog-h">${esc(hA)}</div>
      ${st.map((d, k) => `<button class="big" data-jog="${-d}"><span class="gl">${glyph[k]}</span>${esc(a)} ${d} ${esc(u)}</button>`).join("")}</div>
    <div class="jog-col"><div class="jog-h">${esc(hB)}</div>
      ${st.map((d, k) => `<button class="big" data-jog="${d}"><span class="gl">${glyphB[k]}</span>${esc(b)} ${d} ${esc(u)}</button>`).join("")}</div>
    <button class="big stop jog-stop" data-act="stop"><span class="sq"></span>Stop moving</button>
  </div>`;
}

function msgBox(w) {
  if (!w) return "";
  if (w.error) return `<div class="msg err">${esc(w.error)}</div>`;
  return `<div class="msg ${w.busy ? "busy" : ""}">${esc(w.msg)}</div>`;
}

// mechanism step: choices are local until "Continue"
const HOME_HINT = {
  spool: ["its highest safe position, where it should stop when fully wound in", "its lowest safe position"],
  linear: ["the end of travel you want as home (0)", "the other end of its safe travel"],
  rotary: ["the start of its swing (0°)", "the end of its swing"],
};
const KIND_BLURB = {
  spool: "Winds a cable in and out to raise and lower a hanging piece. Units: mm of cable.",
  linear: "A lead screw or belt moving a carriage along a straight line. Units: mm.",
  rotary: "Turns an arm, wing or crank between two angles. Units: degrees.",
  continuous: "Spins without limits (wheels, propellers, cams). The motion design sets its speed. Units: rev/s.",
};
function mechHtml(w) {
  if (!C.mech || C.mech.motor !== w.motor) C.mech = { motor: w.motor, kind: w.kind, size: w.size, bidir: !!w.bidirectional };
  const kinds = C.kinds || {};
  const k = C.mech.kind, info = kinds[k] || {};
  const presets = (C.sizePresets || {})[k] || [];
  const fine = k === "spool" || k === "linear" ? 0.5 : 0.1;
  return `<div class="step-title">${esc(mname(w.motor))}: Mechanism</div>
    <div class="step-text">What does this motor drive? This sets its units, its limits and the wording on the next steps.</div>
    <div class="kind-grid">${Object.entries(kinds).map(([key, ki]) =>
      `<button class="pick ${key === k ? "sel" : ""}" data-kind="${key}"><span class="p-name">${esc(ki.label)}</span>
        <span class="p-state">${esc(KIND_BLURB[key] || "")}</span></button>`).join("")}</div>
    <div class="sec-row"><span class="section-h">${esc(info.size_label || "Size")}</span>
      <div class="stepper"><button class="mid" data-size="${-fine}">−</button>
        <div class="step-val">${Number(C.mech.size).toFixed(fine < 0.5 ? 1 : 1)} ${esc(info.size_unit || "")}</div>
        <button class="mid" data-size="${fine}">+</button></div></div>
    <div class="chips">${presets.map((v) => `<button class="mid chip ${Number(C.mech.size) === v ? "sel" : ""}" data-preset="${v}">${v}${esc(info.size_unit || "")}</button>`).join("")}</div>
    ${k === "continuous" ? `<div class="sec-row"><span class="section-h">Directions</span>
      <div class="seg"><button class="mid ${!C.mech.bidir ? "sel" : ""}" data-bidir="0">Forward only</button>
      <button class="mid ${C.mech.bidir ? "sel" : ""}" data-bidir="1">Both directions</button></div></div>
      <div class="hint">${C.mech.bidir ? "Design values: 0 = full reverse, 0.5 = stopped, 1 = full forward." : "Design values: 0 = stopped, 1 = full speed forward."}</div>` : ""}
    ${msgBox(w)}
    <div class="spacer"></div>
    <div class="step-actions sticky"><button class="big go" data-act="mech_ok" ${w.busy ? "disabled" : ""}>Continue</button></div>`;
}

// speed test: rising speeds, then rising accelerations, until the encoder sees missed steps
const fmtLim = (v, dflt) => { const x = v ?? dflt; return x == null ? "default" : Number(x) >= 100 ? Math.round(x) : Number(x).toFixed(2); };
function speedHtml(w) {
  const r = w.speed, u = w.unit || "mm", busy = w.busy;
  const intro = `<div class="step-text">Finds how fast ${esc(mname(w.motor))} can move <b>with its real load attached</b>.
    It runs back and forth over the middle of its travel${w.kind === "continuous" ? " (10 turns at a time)" : ""}, faster each time,
    until the encoder shows the motor slipped. Then it tries harder starts and stops. It suggests limits about 30–40% below what ran clean.
    <br><b>Keep people and hands clear: it will move fast.</b> A slipped run makes a buzzing or clunk; that is expected and harmless.</div>`;
  const rows = r?.levels?.length ? `<table class="summary speed-table"><tr><th>Run</th><th>Speed</th><th>Accel</th><th>Result</th></tr>${r.levels.map((l, k) =>
    `<tr class="${l.ok ? "" : "bad"}"><td>${k + 1} ${l.phase === "accel" ? "· start/stop" : ""}</td><td>${fmtLim(l.v)} ${esc(u)}/s</td><td>${fmtLim(l.a)} ${esc(u)}/s²</td><td>${l.ok ? "clean" : "slipped"}</td></tr>`).join("")}</table>` : "";
  let actions;
  if (busy) actions = `<button class="big stop" data-act="speed_stop"><span class="sq"></span>Stop the test</button>`;
  else if (r && r.v_save) actions = `<button class="big go" data-act="speed_use">Use ${fmtLim(r.v_save)} ${esc(u)}/s and ${fmtLim(r.a_save)} ${esc(u)}/s²</button>
      <button class="big" data-act="speed_keep">Keep current (${fmtLim(r.v_now)} / ${fmtLim(r.a_now)})</button>
      <button class="mid" data-act="speed_run">Run again</button>`;
  else actions = `<button class="big primary" data-act="speed_run">Run speed test</button>
      ${w.speed_only ? "" : `<button class="mid" data-act="skip_speed">Skip</button>`}`;
  const result = r && r.v_save && !busy ? `<div class="msg">Ran clean up to <b>${fmtLim(r.v_ok)} ${esc(u)}/s</b> (≈${Math.round(r.rpm_save / 0.7)} RPM) and
    <b>${fmtLim(r.a_ok)} ${esc(u)}/s²</b>${r.stopped ? `; stopped because: ${esc(r.stopped)}` : ""}. Current limits: ${fmtLim(r.v_now)} ${esc(u)}/s, ${fmtLim(r.a_now)} ${esc(u)}/s².</div>` : "";
  return intro + result + msgBox(w) + rows + `<div class="spacer"></div><div class="step-actions sticky">${actions}</div>`;
}

function stepHtml(w) {
  const n = w?.node || {};
  const moving = n.moving || w?.busy;
  if (!w) return C.finished ? doneHtml() : chooseHtml();
  const title = (t) => `<div class="step-title">${esc(mname(w.motor))}: ${esc(t)}</div>`;
  const u = w.unit || "mm", d = w.test_step || 10, [homeHint, farHint] = HOME_HINT[w.kind] || HOME_HINT.spool;
  switch (w.step) {
    case "mechanism":
      return mechHtml(w);
    case "direction":
      return title("Direction") + `
        <div class="step-text">Make sure ${esc(mname(w.motor))} can move freely. Press <b>Test move</b>: the motor moves
          ${d} ${esc(u)} in the direction the software calls <b>positive</b>. It should <b>${esc(w.positive)}</b>.</div>
        ${msgBox(w)}
        <div class="step-actions">
          ${!w.dir_tested || w.busy ? `<button class="big primary" data-act="dir_test" ${moving ? "disabled" : ""}>Test move (${d} ${esc(u)})</button>` :
            `<button class="big go" data-act="lowered" ${moving ? "disabled" : ""}>Yes, it did</button>
             <button class="big" data-act="raised" ${moving ? "disabled" : ""}>No, the other way</button>
             <button class="mid" data-act="dir_test" ${moving ? "disabled" : ""}>Move again</button>`}
        </div>`;
    case "encoder":
      return title("Encoder check") + `
        <div class="step-text">The controller moves the motor ${2 * d} ${esc(u)} and back while it compares the steps sent with what
          the encoder magnet reports. If the encoder counts backwards it is corrected automatically.</div>
        ${msgBox(w)}
        <div class="step-actions"><button class="big primary" data-act="encoder" ${moving ? "disabled" : ""}>${w.error ? "Try again" : "Check encoder"}</button></div>`;
    case "top":
      return title((w.recover ? "Find the " : "Teach the ") + w.home) + `
        <div class="step-text">Jog to <b>${esc(homeHint)}</b>. Use the smallest steps for the last part, then press
          <b>Set as ${esc(w.home)}</b>. This becomes 0 ${esc(u)}.
          ${w.recover ? `<br>Use the same spot as before: the ${esc(w.far)} is measured from it.` : ""}</div>
        ${jogHtml(w)}
        ${msgBox(w)}
        <div class="step-actions"><button class="big go" data-act="top" ${moving ? "disabled" : ""}>Set as ${esc(w.home)}</button></div>`;
    case "bottom": {
      const tooClose = (n.pos_mm ?? 0) < 2 * d;
      return title("Teach the " + w.far) + `
        <div class="step-text">Jog to <b>${esc(farHint)}</b>. The motor will never be sent past it.
          The readout on the right shows how far from the ${esc(w.home)} you are.</div>
        ${jogHtml(w)}
        ${msgBox(w)}
        <div class="step-actions"><button class="big go" data-act="bottom" ${moving || tooClose ? "disabled" : ""}>Set as ${esc(w.far)}</button>
          <span class="hint">${tooClose ? `Go at least ${2 * d} ${esc(u)} from the ${esc(w.home)}.` : ""}</span></div>`;
    }
    case "test":
      return title("Test") + (w.kind === "continuous" ? `
        <div class="step-text">The motor turns forward for 3 seconds at a quarter of its top speed, then back the same amount.</div>` : `
        <div class="step-text">The motor now moves slowly to the <b>middle</b>, the <b>${esc(w.far)}</b> (${mm(w.bottom_mm, 0)} ${esc(u)})
          and back to the <b>${esc(w.home)}</b>, staying inside the limits you taught. Watch that it stops at the right places.</div>`) + `
        ${msgBox(w)}
        <div class="step-actions"><button class="big primary" data-act="test" ${moving ? "disabled" : ""}>${w.kind === "continuous" ? "Run spin test" : "Run test moves"}</button>
          <button class="mid" data-act="skip_test" ${moving ? "disabled" : ""}>Skip</button></div>`;
    case "speed":
      return title("Speed test") + speedHtml(w);
    case "finish": {
      const m = C.config.motors[w.motor] || {}, kinfo = (C.kinds || {})[w.kind] || {};
      const rows = w.recover
        ? [["Motor", mname(w.motor)], ["Board", w.mac], [cap(w.home), "re-taught"], [cap(w.far), `kept (${mm(C.tick?.travel?.[w.motor], 0)} ${u})`]]
        : [["Motor", mname(w.motor)], ["Board", w.mac], ["Mechanism", kinfo.label || w.kind],
           [kinfo.size_label || "Size", `${w.size} ${kinfo.size_unit || ""}`],
           ["Motor direction", m.invert_dir ? "reversed" : "normal"], ["Encoder direction", m.invert_encoder ? "reversed" : "normal"],
           ...(w.kind === "continuous" ? [["Directions", w.bidirectional ? "both" : "forward only"]]
             : [[`Travel (${w.home} to ${w.far})`, `${mm(w.bottom_mm, 0)} ${u}`]]),
           ["Test", w.test_done ? "done" : "skipped"],
           ["Speed limit", `${fmtLim(C.tick?.vmax?.[w.motor])} ${u}/s, ${fmtLim(C.tick?.amax?.[w.motor])} ${u}/s²${w.speed?.v_save ? " (speed test)" : ""}`]];
      return title("Save") + `
        <div class="step-text">Check the summary and save. ${w.kind === "continuous" ? "The show will drive this motor's speed." :
          "The motor will remember its position from now on, including across power cuts, and the show can use it."}</div>
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
  const ends = (C.selMotor !== null && C.tick?.ends?.[C.selMotor]) || ["top", "bottom"];
  const canRecover = C.tick?.kinds?.[C.selMotor] !== "continuous" && !isNew && (sel && C.selMotor !== null && comm[C.selMotor] && sel.flags.includes("calibrated") || lost);
  const canSpeed = !isNew && sel && C.selMotor !== null && sel.id === C.selMotor && comm[C.selMotor] && nodeState(sel)[0] === "on";
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
      ${canRecover ? `<button class="big primary" data-act="recover">Find ${esc(ends[0])} only</button>` : ""}
      ${canSpeed ? `<button class="big" data-act="speed_only">Speed test only</button>` : ""}
      <span class="hint">${canRecover ? `Find ${esc(ends[0])} only: for a motor that lost its position. Keeps its ${esc(ends[1])} limit. ` : ""}${canSpeed ? "Speed test only: measure how fast this motor can safely go, keeping everything else." : ""}</span>
    </div>`;
}

function doneHtml() {
  const f = C.finished;
  return `<div class="step-title">${esc(mname(f.motor))} ${f.speedOnly ? "speed test done" : "is set up"}</div>
    <div class="step-text">${f.speedOnly ? "Its speed and acceleration limits are saved. Motion designs now show the fastest cycle it can do." : f.recover ? "The top was re-found. The motor knows its position again." :
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
  const kindB = ev.target.closest("[data-kind]");
  if (kindB && C.mech) {
    C.mech.kind = kindB.dataset.kind;
    C.mech.size = (C.kinds?.[C.mech.kind] || {}).size_default ?? 1;
    if (C.mech.kind === "spool") C.mech.size = C.config.mechanics.spool_diameter_mm;
    C.key = ""; render(); return;
  }
  const sz = ev.target.closest("[data-size]");
  if (sz && C.mech) { C.mech.size = Math.max(0.1, Math.round((Number(C.mech.size) + Number(sz.dataset.size)) * 10) / 10); C.key = ""; render(); return; }
  const pr = ev.target.closest("[data-preset]");
  if (pr && C.mech) { C.mech.size = Number(pr.dataset.preset); C.key = ""; render(); return; }
  const bd = ev.target.closest("[data-bidir]");
  if (bd && C.mech) { C.mech.bidir = bd.dataset.bidir === "1"; C.key = ""; render(); return; }
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
  else if (a === "speed_only") {
    const armed = C.tick?.armed;
    if (armed && !(await modal("The speed test stops the show. Continue?", [{ label: "Cancel", value: false }, { label: "Stop show and continue", value: true, cls: "primary" }]))) return;
    C.finished = null;
    send("wiz_start", { mac: C.selMac, motor: C.selMotor, speed_only: true });
  }
  else if (a === "speed_run") send("wiz_speed");
  else if (a === "speed_stop") send("wiz_speed_stop");
  else if (a === "speed_use") send("wiz_speed_save", { use: true });
  else if (a === "speed_keep") send("wiz_speed_save", { use: false });
  else if (a === "stop") send("wiz_stop");
  else if (a === "dir_test") send("wiz_dir_test");
  else if (a === "lowered") send("wiz_dir_answer", { correct: true });
  else if (a === "raised") send("wiz_dir_answer", { correct: false });
  else if (a === "mech_ok") send("wiz_mech", { kind: C.mech.kind, size: C.mech.size, bidirectional: C.mech.bidir });
  else if (a === "encoder") send("wiz_encoder");
  else if (a === "top") send("wiz_top");
  else if (a === "bottom") send("wiz_bottom");
  else if (a === "test") send("wiz_test");
  else if (a === "skip_test") send("wiz_goto", { step: C.tick.wizard.steps.includes("speed") ? "speed" : "finish" });
  else if (a === "skip_speed") send("wiz_goto", { step: "finish" });
  else if (a === "finish") { C.finished = { motor: C.tick.wizard.motor, recover: C.tick.wizard.recover, speedOnly: C.tick.wizard.speed_only }; send("wiz_finish"); }
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
        encoder, and teaches the two ends of its travel (top and bottom, home and far end, or start and end angle).
        Without home switches, the motors use the taught home and the encoder to know where they are after a power cut.
        Continuous-rotation motors skip the ends: the show drives their speed.</div>`;
    return;
  }
  const n = w.node || {};
  const bottom = w.bottom_mm ?? (w.recover ? C.tick?.travel?.[w.motor] : null);
  const u = w.unit || "mm", home = w.home || "top", far = w.far || "bottom", cont = w.kind === "continuous";
  ro.innerHTML = `
    <div><div class="ro-name">${esc(mname(w.motor))}</div><div class="ro-mac">${esc(w.mac)}</div></div>
    <div><div class="ro-label">${n.homed && !cont ? `From the ${esc(home)}` : "Position"}</div>
      ${n.homed && !cont ? `<div class="ro-pos">${mm(n.pos_mm)} <small>${esc(u)}</small></div>` :
        `<div class="ro-wait">${cont ? "Continuous rotation: no ends to teach" : `Known once the ${esc(home)} is taught`}</div>`}</div>
    <div class="ro-rows">
      <span>Encoder</span><span class="v">${mm(n.enc_mm)} ${esc(u)}</span>
      <span>Magnet</span><span class="v">${esc(n.magnet || "—")}</span>
      ${cont ? "" : `<span>${esc(cap(far))}</span><span class="v">${bottom ? mm(bottom, 0) + " " + esc(u) : "—"}</span>`}
    </div>
    <div class="ro-leds">
      <span>${led(n.online ? "on" : "bad")}Link</span>
      <span>${led(n.moving ? "warn" : "")}Moving</span>
      <span>${led(n.calibrated ? "on" : "")}${esc(cap(home))} taught</span>
      <span>${led(n.limits ? "on" : "")}Limits on</span>
    </div>
    ${w.sim_drop_mm !== undefined ? `<div class="sim-hint"><b>Simulator:</b> the moving part is really at ${mm(w.sim_drop_mm)} ${esc(u)}
      from its physical ${esc(home || "start")}. Use this in place of watching it.</div>` : ""}
    ${n.homed && !cont ? `<canvas id="gauge"></canvas>` : ""}`;
  if (n.homed && !cont) drawGauge(n.pos_mm, bottom, w.sim_drop_mm, home, far);
}

function drawGauge(pos, bottom, truth, home = "top", far = "bottom") {
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
  mark(0, `${home} 0`, "#4a525b");
  if (bottom) mark(bottom, `${far} ${Math.round(bottom)}`, "#4a525b");
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
    (n.pos_mm ?? 0) >= 2 * (w.test_step || 10), C.config.motors[w.motor], w.kind, C.mech] :
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
