// Pendulum Lab console. Talks to bridge.py over a WebSocket.
"use strict";

// ---------------------------------------------------------------- constants
const TRACK_HALF_MM = 470;          // limit switch to centre (homing: 12,405 counts)
const SOFT_LIMIT_MM = 0.85 * TRACK_HALF_MM;
const CHART_SECONDS = 20;

const STATES = {
  OFFLINE: ["Rig offline", "Waiting for the bridge to find the motor board."],
  IDLE:    ["Ready", "Press Start to home the cart, swing the pendulum up and balance it."],
  HOMING:  ["Homing", "Finding both ends of the track, then centring the cart."],
  HANG:    ["Settling", "Measuring where the pendulum hangs at rest."],
  SWING:   ["Swinging up", "Pumping energy into the pendulum to bring it to the top."],
  BALANCE: ["Balancing", "Holding the pendulum upright and the cart near the centre."],
  BRAKE:   ["Braking", "The cart reached the software limit and is stopping."],
  STEP:    ["Step test", "Moving the cart back and forth to test the speed loop."],
  CHECK:   ["Sign check", "Motor off. Angle and position are shown for checking."],
  DONE:    ["Test finished", "Motor off."],
  DAC:     ["Pi control", "The Raspberry Pi is driving the motor."],
};
const RUNNING = new Set(["HOMING", "HANG", "SWING", "BALANCE", "BRAKE", "STEP", "CHECK", "DONE", "DAC"]);

const GROUPS = [
  { title: "Balance", names: ["BAL_MODE", "BAL_POLE", "BAL_K_TH", "BAL_K_THD", "BAL_K_X", "BAL_K_V",
                              "CATCH_ANGLE_DEG", "CATCH_RATE", "DROP_ANGLE_DEG"] },
  { title: "Swing-up", names: ["SWING_V", "SWING_E_TARGET", "SWING_E_SLOW", "SWING_PHASE_DEG", "K_X_SWING"] },
  { title: "Cart speed loop", names: ["KV_P", "V_FF", "V_MAX"] },
];
const LABELS = {
  BAL_MODE: "Gain mode",
  BAL_POLE: "Stiffness",
  BAL_K_TH: "Angle gain",
  BAL_K_THD: "Angular-rate gain",
  BAL_K_X: "Cart position gain",
  BAL_K_V: "Cart speed gain",
  CATCH_ANGLE_DEG: "Catch angle",
  CATCH_RATE: "Catch speed limit",
  DROP_ANGLE_DEG: "Drop angle",
  SWING_V: "Swing speed",
  SWING_E_TARGET: "Energy target",
  SWING_E_SLOW: "Energy ease-off",
  SWING_PHASE_DEG: "Push timing lead",
  K_X_SWING: "Centring pull",
  KV_P: "Speed loop gain",
  V_FF: "Feed-forward",
  V_MAX: "Top speed",
};
// Help text shown under each field (the board's own descriptions are terser)
const HELP = {
  BAL_POLE: "Higher is stiffer. The four gains are computed from this.",
  BAL_K_TH: "Cart acceleration per radian of lean.",
  BAL_K_THD: "Per rad/s of rotation. Damps the motion.",
  BAL_K_X: "Per metre from centre. Brings the cart home.",
  BAL_K_V: "Per m/s of cart speed.",
  CATCH_ANGLE_DEG: "Switch to balancing inside this angle from upright.",
  CATCH_RATE: "Only catch when the pendulum turns slower than this.",
  DROP_ANGLE_DEG: "Give up and swing up again past this angle.",
  SWING_V: "How fast the cart moves while pumping energy in.",
  SWING_E_TARGET: "0 just reaches upright. Higher arrives faster.",
  SWING_E_SLOW: "Larger values ease off the push earlier near the top.",
  SWING_PHASE_DEG: "0 pushes as the pendulum passes the bottom, 90 at the swing ends.",
  K_X_SWING: "Pulls the cart back toward the centre while swinging.",
  KV_P: "How hard the motor corrects speed errors.",
  V_FF: "Motor command per m/s of target speed.",
  V_MAX: "Speed limit for the cart.",
};
const MANUAL_ONLY = new Set(["BAL_K_TH", "BAL_K_THD", "BAL_K_X", "BAL_K_V"]);
const POLE_ONLY = new Set(["BAL_POLE"]);

// ---------------------------------------------------------------- state
const app = {
  ws: null,
  connected: false,
  role: "free",          // "you" | "other" | "free"
  state: "OFFLINE",
  params: [],            // from the board
  pending: {},           // name -> value typed but not applied
  errors: {},            // name -> message
  samples: [],           // {t, th, x}
  pose: { th: 180, x: 0, has: false },
  extra: null,
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- connection
let retryMs = 500;
function connect() {
  const token = new URLSearchParams(location.search).get("token");
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws` +
              (token ? `?token=${encodeURIComponent(token)}` : "");
  setConn("connecting", "Connecting");
  const ws = new WebSocket(url);
  app.ws = ws;
  ws.onopen = () => { app.connected = true; retryMs = 500; setConn("ok", "Connected"); render(); };
  ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = (e) => {
    app.connected = false;
    app.role = "free";
    setConn("down", e.code === 4401 ? "Wrong or missing token" : "Disconnected");
    render();
    if (e.code !== 4401) {
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 8000);
    }
  };
}
function send(msg) {
  if (app.ws && app.ws.readyState === WebSocket.OPEN) app.ws.send(JSON.stringify(msg));
}
function setConn(state, text) {
  $("conn").dataset.state = state;
  $("connText").textContent = text;
}
setInterval(() => { if (app.role === "you") send({ cmd: "ping" }); }, 1000);

// ---------------------------------------------------------------- messages
function onMessage(m) {
  switch (m.kind) {
    case "hello":
      app.role = m.control;
      setState(m.state, true);
      if (m.params && m.params.length) setParams(m.params);
      else send({ cmd: "params" });             // bridge hasn't got them yet: ask
      $("viewerCount").textContent = m.clients;
      break;
    case "control":
      app.role = m.control;
      $("viewerCount").textContent = m.clients;
      break;
    case "state":
      setState(m.state);
      break;
    case "params":
      setParams(m.params);
      break;
    case "telemetry":
      onTelemetry(m);
      return;                       // high rate: no full re-render
    case "reply":
      onReply(m);
      break;
    case "event":
      addEvent(m.text, toneOf(m.text));
      break;
  }
  render();
}

function setState(s, quiet) {
  if (!STATES[s]) s = s === "STEP TEST" ? "STEP" : "IDLE";
  if (s === app.state) return;
  app.state = s;
  if (!quiet && s !== "OFFLINE") addEvent(STATES[s][0], s === "BALANCE" ? "good" : s === "BRAKE" ? "bad" : "");
  if (s === "HOMING") { app.samples = []; }
}

function setParams(list) {
  app.params = list;
  // Drop pending edits that now match the board
  for (const p of list) {
    if (p.name in app.pending && Math.abs(app.pending[p.name] - p.value) < 1e-9) delete app.pending[p.name];
  }
  renderParams();
}

function onReply(m) {
  const [name, ...rest] = m.text.split(" ");
  const isParam = app.params.some((p) => p.name === name);
  if (isParam) {
    if (m.ok) {
      delete app.errors[name];
    } else {
      app.errors[name] = rest.join(" ").replace(/^range /, "Allowed range ");
    }
    renderParams();
    return;
  }
  if (!m.ok) toast(clean(m.text), "bad");
  else if (name === "defaults") toast("Defaults restored");
}

function onTelemetry(m) {
  const now = performance.now() / 1000;
  if (m.mode === "SWING" || m.mode === "BAL") {
    app.pose = { th: m.th_deg, x: m.x_mm, has: true };
    app.samples.push({ t: now, th: m.th_deg, x: m.x_mm });
    $("roAngle").textContent = m.th_deg.toFixed(1);
    $("roX").textContent = m.x_mm.toFixed(0);
    if (m.mode === "SWING") {
      $("roExtraLabel").textContent = "Swing energy";
      $("roExtra").textContent = m.Eend.toFixed(2);
      $("roExtraUnit").textContent = "of 0";
    } else {
      $("roExtraLabel").textContent = "Upright trim";
      $("roExtra").textContent = m.trim_deg.toFixed(2);
      $("roExtraUnit").textContent = "deg";
    }
  } else if (m.mode === "HANG") {
    app.pose = { th: 180, x: 0, has: true };
  }
  const cutoff = now - CHART_SECONDS;
  while (app.samples.length && app.samples[0].t < cutoff) app.samples.shift();
  $("vizEmpty").hidden = app.pose.has;
}

// ---------------------------------------------------------------- render
function canEdit() { return app.connected && app.role === "you" && app.state === "IDLE"; }

function render() {
  const [label, help] = STATES[app.state] || STATES.IDLE;
  const st = $("stateLabel");
  st.textContent = app.connected ? label : "Not connected";
  st.dataset.state = app.connected ? app.state : "OFFLINE";
  $("stateHelp").textContent = !app.connected ? "Trying to reach the bridge on this computer."
    : app.state === "IDLE" && app.role !== "you" ? "Take control to start a run." : help;

  const online = app.connected && app.state !== "OFFLINE";
  $("startBtn").disabled = !(online && app.role === "you" && app.state === "IDLE");
  $("stopBtn").disabled = !online;          // anyone connected can stop

  const cb = $("controlBtn");
  if (app.role === "you") { cb.textContent = "Release control"; $("ownerText").textContent = "You are in control"; }
  else if (app.role === "other") { cb.textContent = "Take control"; $("ownerText").textContent = "Someone else is in control"; }
  else { cb.textContent = "Take control"; $("ownerText").textContent = "Viewing"; }
  cb.disabled = !app.connected || app.role === "other";

  $("paramsNote").textContent =
    app.role !== "you" ? "Take control to change settings. Changes apply while the rig is idle."
    : app.state !== "IDLE" ? "Settings are locked while the rig is running. Stop it to make changes."
    : "Edit values, then apply. They take effect on the next run.";
  $("applyBtn").disabled = !canEdit() || Object.keys(app.pending).length === 0;
  $("defaultsBtn").disabled = !canEdit();
  document.querySelectorAll("#paramsBody input, #paramsBody .segmented button")
    .forEach((el) => { el.disabled = !canEdit(); });
}

function valueOf(name) {
  if (name in app.pending) return app.pending[name];
  const p = app.params.find((q) => q.name === name);
  return p ? p.value : undefined;
}

function fmt(v) {
  if (v === undefined || v === null || Number.isNaN(v)) return "";
  return String(Math.round(v * 1000) / 1000);
}

function renderParams() {
  const body = $("paramsBody");
  if (!app.params.length) return;               // keep the skeleton until data arrives
  body.removeAttribute("aria-busy");
  const manual = valueOf("BAL_MODE") >= 0.5;
  const byName = Object.fromEntries(app.params.map((p) => [p.name, p]));
  const focusedId = document.activeElement && document.activeElement.id;
  body.innerHTML = "";

  for (const g of GROUPS) {
    const sec = document.createElement("div");
    sec.className = "group";
    sec.innerHTML = `<h3>${g.title}</h3><div class="group-fields"></div>`;
    const fields = sec.querySelector(".group-fields");
    for (const name of g.names) {
      const p = byName[name];
      if (!p) continue;
      if (manual && POLE_ONLY.has(name)) continue;
      if (!manual && MANUAL_ONLY.has(name)) continue;
      fields.appendChild(name === "BAL_MODE" ? modeField(p) : numberField(p));
    }
    body.appendChild(sec);
  }
  if (focusedId) { const el = $(focusedId); if (el) el.focus(); }
  render();
}

function modeField(p) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const manual = valueOf(p.name) >= 0.5;
  wrap.innerHTML = `
    <label>${LABELS[p.name]}</label>
    <div class="segmented" role="group" aria-label="${LABELS[p.name]}">
      <button type="button" data-v="0" aria-pressed="${!manual}">Pole placement</button>
      <button type="button" data-v="1" aria-pressed="${manual}">Manual gains</button>
    </div>
    <span class="help">${manual ? "Type the four feedback gains directly." : "All four gains are computed from the stiffness."}</span>`;
  wrap.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    setPending(p, Number(b.dataset.v));
    renderParams();
  }));
  if (p.name in app.pending) wrap.classList.add("dirty");
  return wrap;
}

function numberField(p) {
  const wrap = document.createElement("div");
  wrap.className = "field" + (p.name in app.pending ? " dirty" : "");
  const id = `f_${p.name}`;
  wrap.innerHTML = `
    <label for="${id}">${LABELS[p.name] || p.name}<span class="range">${fmt(p.min)} to ${fmt(p.max)}</span></label>
    <div class="input-row">
      <input type="number" id="${id}" step="any" inputmode="decimal" value="${fmt(valueOf(p.name))}"
             min="${p.min}" max="${p.max}" aria-describedby="${id}_h ${id}_e">
      <span class="unit">${p.unit || ""}</span>
    </div>
    <span class="help" id="${id}_h">${HELP[p.name] || capitalise(p.desc)}${Math.abs(p.value - p.default) > 1e-9 ? ` (default ${fmt(p.default)})` : ""}</span>
    <span class="error" id="${id}_e">${app.errors[p.name] || ""}</span>`;
  const input = wrap.querySelector("input");
  input.addEventListener("input", () => {
    const v = input.value === "" ? NaN : Number(input.value);
    const err = wrap.querySelector(".error");
    if (Number.isNaN(v)) { err.textContent = "Enter a number"; return; }
    if (v < p.min || v > p.max) { err.textContent = `Allowed range ${fmt(p.min)} to ${fmt(p.max)}`; }
    else { err.textContent = ""; delete app.errors[p.name]; }
    setPending(p, v);
    wrap.classList.toggle("dirty", p.name in app.pending);
    render();
  });
  return wrap;
}

function setPending(p, v) {
  if (Math.abs(v - p.value) < 1e-9) delete app.pending[p.name];
  else app.pending[p.name] = v;
}

function capitalise(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""; }
function clean(s) { return s.replace(/[—–]/g, "-"); }   // no em/en dashes in the UI

// ---------------------------------------------------------------- actions
$("controlBtn").addEventListener("click", () => send({ cmd: app.role === "you" ? "release" : "claim" }));
$("startBtn").addEventListener("click", () => $("startDialog").showModal());
$("startDialog").addEventListener("close", () => {
  if ($("startDialog").returnValue === "start") send({ cmd: "start" });
});
$("stopBtn").addEventListener("click", () => send({ cmd: "stop" }));
$("defaultsBtn").addEventListener("click", () => $("defaultsDialog").showModal());
$("defaultsDialog").addEventListener("close", () => {
  if ($("defaultsDialog").returnValue === "ok") { app.pending = {}; app.errors = {}; send({ cmd: "defaults" }); }
});
$("applyBtn").addEventListener("click", () => {
  const entries = Object.entries(app.pending);
  let blocked = false;
  for (const [name, v] of entries) {
    const p = app.params.find((q) => q.name === name);
    if (!p || Number.isNaN(v) || v < p.min || v > p.max) { blocked = true; continue; }
    send({ cmd: "set", name, value: v });       // the board re-checks every limit
  }
  if (blocked) toast("Some values are out of range. Fix the highlighted fields.", "bad");
});
document.addEventListener("keydown", (e) => {   // Esc = stop, anywhere on the page
  if (e.key === "Escape" && !$("stopBtn").disabled && !document.querySelector("dialog[open]")) send({ cmd: "stop" });
});

// ---------------------------------------------------------------- events & toasts
function toneOf(t) {
  return /SOFT LIMIT|OVERSPEED|LIMIT HIT|ERROR|STOP|heartbeat|aborted/i.test(t) ? "bad" : "";
}
function addEvent(text, tone = "") {
  const list = $("eventList");
  const empty = list.querySelector(".event-empty");
  if (empty) empty.remove();
  const li = document.createElement("li");
  if (tone) li.dataset.tone = tone;
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  li.innerHTML = `<time>${time}</time><span></span>`;
  li.querySelector("span").textContent = clean(text);
  list.prepend(li);
  while (list.children.length > 60) list.lastChild.remove();
}
function toast(text, tone = "") {
  const el = document.createElement("div");
  el.className = "toast";
  if (tone) el.dataset.tone = tone;
  el.textContent = clean(text);
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------- drawing
let colors = {};
function readColors() {
  const cs = getComputedStyle(document.documentElement);
  for (const k of ["text", "text-2", "text-3", "line", "line-strong", "accent", "stop", "surface", "surface-2"]) {
    colors[k] = cs.getPropertyValue(`--${k}`).trim();
  }
}
readColors();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", readColors);

function fitCanvas(c) {
  const r = c.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: r.width, h: r.height };
}

const smooth = { th: 180, x: 0 };
function drawViz() {
  const { ctx, w, h } = fitCanvas($("viz"));
  ctx.clearRect(0, 0, w, h);
  const margin = 28;
  const railY = h * 0.68;
  const sx = (mm) => w / 2 + (mm / TRACK_HALF_MM) * (w / 2 - margin);

  // ease toward the latest sample (purely visual, data is ~50 Hz)
  let dth = ((app.pose.th - smooth.th + 540) % 360) - 180;
  smooth.th += dth * 0.5;
  smooth.x += (app.pose.x - smooth.x) * 0.5;

  // rail, limit stops, soft-limit marks
  ctx.strokeStyle = colors["line-strong"]; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(sx(-TRACK_HALF_MM), railY); ctx.lineTo(sx(TRACK_HALF_MM), railY); ctx.stroke();
  ctx.fillStyle = colors["text-3"];
  for (const s of [-1, 1]) ctx.fillRect(sx(s * TRACK_HALF_MM) - 2, railY - 10, 4, 20);
  ctx.setLineDash([3, 4]); ctx.strokeStyle = colors["stop"]; ctx.globalAlpha = 0.5; ctx.lineWidth = 1;
  for (const s of [-1, 1]) { ctx.beginPath(); ctx.moveTo(sx(s * SOFT_LIMIT_MM), railY - 14); ctx.lineTo(sx(s * SOFT_LIMIT_MM), railY + 14); ctx.stroke(); }
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  ctx.strokeStyle = colors["line"];
  ctx.beginPath(); ctx.moveTo(sx(0), railY + 8); ctx.lineTo(sx(0), railY + 16); ctx.stroke();

  // cart
  const cx = sx(smooth.x), cw = Math.max(44, w * 0.07), ch = 18;
  ctx.fillStyle = colors["text"];
  roundRect(ctx, cx - cw / 2, railY - ch / 2, cw, ch, 5); ctx.fill();

  // pendulum: theta 0 = up, positive = top leans right (+x)
  const L = Math.min(h * 0.55, w * 0.22);
  const rad = smooth.th * Math.PI / 180;
  const px = cx, py = railY - ch / 2;
  const bx = px + L * Math.sin(rad), by = py - L * Math.cos(rad);
  ctx.strokeStyle = app.state === "BALANCE" ? colors["accent"] : colors["text-2"];
  ctx.lineWidth = 5; ctx.lineCap = "round";
  ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(bx, by); ctx.stroke();
  ctx.fillStyle = ctx.strokeStyle;
  ctx.beginPath(); ctx.arc(bx, by, 8, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = colors["surface"];
  ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

function niceScale(maxAbs, steps) { return steps.find((s) => maxAbs <= s) || steps[steps.length - 1]; }

function drawChart(canvas, key, steps, color) {
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const left = 40, right = 6, top = 6, bottom = 6;
  const pw = w - left - right, ph = h - top - bottom;
  const data = app.samples;
  const maxAbs = data.reduce((m, s) => Math.max(m, Math.abs(s[key])), 0);
  const range = niceScale(maxAbs, steps);
  const y = (v) => top + ph / 2 - (v / range) * (ph / 2);

  ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
  ctx.fillStyle = colors["text-3"]; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.strokeStyle = colors["line"]; ctx.lineWidth = 1;
  for (const v of [range, 0, -range]) {
    ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(w - right, y(v)); ctx.stroke();
    ctx.fillText(String(v), left - 6, y(v));
  }
  if (data.length < 2) {
    ctx.textAlign = "center"; ctx.fillText("Waiting for data", left + pw / 2, top + ph / 2 - 12);
    return;
  }
  const tEnd = performance.now() / 1000;
  const xAt = (t) => left + pw * (1 - (tEnd - t) / CHART_SECONDS);
  ctx.strokeStyle = color; ctx.lineWidth = 1.75; ctx.lineJoin = "round";
  ctx.beginPath();
  data.forEach((s, i) => {
    const vx = xAt(s.t), vy = y(Math.max(-range, Math.min(range, s[key])));
    if (i === 0) ctx.moveTo(vx, vy);
    else if (key === "th" && Math.abs(s.th - data[i - 1].th) > 180) ctx.moveTo(vx, vy);  // wrap at +-180
    else ctx.lineTo(vx, vy);
  });
  ctx.stroke();
}

function frame() {
  drawViz();
  drawChart($("chartAngle"), "th", [10, 30, 90, 180], colors["accent"]);
  drawChart($("chartX"), "x", [50, 100, 250, 500], colors["text"]);
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- go
render();
connect();
requestAnimationFrame(frame);
