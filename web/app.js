/* DMX Scene Builder: iPad / browser front end. Vanilla JS, no build step. */
"use strict";

// ------------------------------------------------------------------ tiny helpers
const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const uid8 = () => Math.random().toString(16).slice(2, 10);

async function api(method, path, body) {
  // Every request gives up eventually, so one lost on flaky Wi-Fi can't freeze the page.
  // Scans, Mode 7 switching and saves talk to the lights and can take a minute.
  const slow = /\/(scan|apply|rdm|save|verify|box|autopatch)\b/.test(path);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), slow ? 150000 : 8000);
  let r;
  try {
    r = await fetch(path, {
      method, headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined, signal: ctl.signal,
    });
  } catch (e) {
    throw new Error(e.name === "AbortError" ? "The Mac didn't answer in time. Try again." : "Can't reach the Mac: " + e.message);
  } finally { clearTimeout(timer); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
  return data;
}

function toast(msg, kind = "") {
  const root = $("#toastRoot");
  root.innerHTML = "";
  const t = h("div", { class: "toast " + kind }, msg);
  root.append(t);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.remove(), kind === "bad" ? 6000 : 2600);
}

function modal(title, body, actions) {
  const root = $("#modalRoot");
  const close = () => { root.innerHTML = ""; };
  const scrim = h("div", { class: "scrim", onclick: (e) => { if (e.target === scrim) close(); } },
    h("div", { class: "modal" }, h("h2", null, title), body,
      h("div", { class: "btn-row", style: { marginTop: "18px", justifyContent: "flex-end" } },
        (actions || []).map(a => h("button", { class: "btn " + (a.cls || ""), onclick: async () => { const keep = await a.fn(close); if (!keep) close(); } }, a.label)),
        h("button", { class: "btn ghost", onclick: close }, "Close"))));
  root.innerHTML = "";
  root.append(scrim);
  return close;
}

function ask(title, label, value) {
  return new Promise(resolve => {
    const inp = h("input", { class: "f", value: value || "" });
    modal(title, h("div", { class: "field" }, h("label", null, label), inp),
      [{ label: "OK", cls: "gold", fn: () => resolve(inp.value.trim()) }]);
    setTimeout(() => inp.focus(), 50);
  });
}

function confirmBox(title, text, okLabel = "Yes", cls = "gold") {
  return new Promise(resolve => {
    modal(title, h("p", null, text), [{ label: okLabel, cls, fn: () => resolve(true) }]);
  });
}

// ------------------------------------------------------------------ color preview (mirrors fixtures.py)
function kelvinToRgb(k) {
  const t = clamp(k, 1000, 40000) / 100;
  let r, g, b;
  if (t <= 66) { r = 255; g = 99.4708025861 * Math.log(t) - 161.1195681661; b = t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307; }
  else { r = 329.698727446 * Math.pow(t - 60, -0.1332047592); g = 288.1221695283 * Math.pow(t - 60, -0.0755148492); b = 255; }
  return [clamp(r, 0, 255) / 255, clamp(g, 0, 255) / 255, clamp(b, 0, 255) / 255];
}
function hsvToRgb(hh, s, v) {
  const i = Math.floor(hh / 60) % 6, f = hh / 60 - Math.floor(hh / 60);
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  return [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][i];
}
const DEFAULT_STATE = { dim: 1, kind: "white", cct: 3000, hue: 30, sat: 1, white: 0, boost: false };
function twMin(fx) { return fx.mode === 7 ? 3000 : 2700; }  // Mode 7 mixes the 3000K and 6500K LEDs directly
function effVariant(fx) { return (MODES[fx.variant] && MODES[fx.variant][fx.mode]) ? fx.variant : "RGBW"; }
function stateOf(fl, fx) { return Object.assign({}, DEFAULT_STATE, (fl.live || {})[fx.id] || {}); }
function previewCss(fx, st) {
  const v = effVariant(fx);
  let rgb;
  if (v === "RGBW" && st.kind === "color") {
    rgb = hsvToRgb(st.hue, st.sat, 1);
    const w = kelvinToRgb(6500);
    rgb = rgb.map((c, i) => Math.min(1, c + st.white * w[i]));
  } else if (v === "PW") rgb = kelvinToRgb(3000);
  else if (v === "RGBW" && !MODES_ROLES(fx).includes("ctc")) rgb = kelvinToRgb(fx.variant === "RGBW" ? 6500 : 3000);
  else rgb = kelvinToRgb(v === "TW" ? clamp(st.cct, twMin(fx), 6500) : st.cct);
  const d = st.dim;
  // perceptual boost so dim looks still read on screen
  const k = d <= 0 ? 0 : 0.12 + 0.88 * Math.pow(d, 0.6);
  return `rgb(${rgb.map(c => Math.round(c * k * 255)).join(",")})`;
}
function tunedMix(k) {
  const cal = ((S.project && S.project.white_cal) || {}).RGBW || {};
  const pts = Object.entries(cal).map(([kk, v]) => [parseInt(kk, 10), v]).filter(p => p[1] && p[1].length === 4).sort((a, b) => a[0] - b[0]);
  if (!pts.length || k < pts[0][0]) return null;
  if (k >= pts[pts.length - 1][0]) return pts[pts.length - 1][1];
  for (let i = 0; i < pts.length - 1; i++) {
    const [k0, a] = pts[i], [k1, b] = pts[i + 1];
    if (k >= k0 && k <= k1) { const t = (k - k0) / (k1 - k0); return a.map((x, j) => x + (b[j] - x) * t); }
  }
  return null;
}

function mixValues(fx, st) {
  // Mirrors fixtures.mix_values on the Mac: what the light is asked to make, for record keeping.
  const v = effVariant(fx), roles = MODES_ROLES(fx), out = { dim: Math.round(st.dim * 100) };
  if (v === "RGBW") {
    let r, g, b, w;
    if (st.kind === "color") { [r, g, b] = hsvToRgb(((st.hue % 360) + 360) % 360, st.sat, 1); w = st.white; }
    else {
      const mix = roles.includes("w") ? tunedMix(st.cct) : null;
      if (mix) [r, g, b, w] = mix;
      else if (roles.includes("ctc")) { r = g = b = w = 1; out.k = Math.round(clamp(st.cct, 1800, 6500) / 10) * 10; }
      else if (roles.includes("w")) { r = g = b = 0; w = 1; }
      else { r = g = b = 1; w = 0; }
    }
    Object.assign(out, { r: Math.round(r * 255), g: Math.round(g * 255), b: Math.round(b * 255), w: Math.round(w * 255) });
  } else if (v === "TW") out.k = Math.round(clamp(st.cct, twMin(fx), 6500));
  return out;
}

function mixText(fx, st, short) {
  const m = mixValues(fx, st);
  if (effVariant(fx) === "RGBW") {
    const t = short ? `R${m.r} G${m.g} B${m.b} W${m.w}` : `R ${m.r} · G ${m.g} · B ${m.b} · W ${m.w}`;
    return m.k ? t + (short ? "" : ` (fixture white ${m.k}K)`) : t;
  }
  return m.k ? m.k + "K" + (fx.variant === "TW" && fx.mode === 7 && st.boost ? (short ? " max" : " · max output") : "") : "";
}

function MODES_ROLES(fx) { const m = (MODES[effVariant(fx)] || {})[fx.mode]; return m ? m.roles : []; }
function footprint(fx) { return MODES_ROLES(fx).length || 0; }

// ------------------------------------------------------------------ app state
const S = {
  project: null, engine: {}, sim: null, settings: {}, version: "",
  floatId: localStorage.getItem("fl.float") || null,
  tab: localStorage.getItem("fl.tab") || "look",
  sel: new Set(),
  patchSel: new Set(),  // fixtures ticked on the Patch tab for bulk edits
  rev: 0,            // project structure revision (patch/looks/palette); other devices' edits bump it
  pointerDown: false,
  scan: {},          // box_id -> scan result
  links: {},         // scanned uid -> fixture id ("" = none, "__new" = add)
  discover: null,    // nodes list
  saving: false, saveTimer: null,
};
let MODES = {};      // variant -> mode -> {name, roles}
let MODE_LIST = [];

function curFloat() { return S.project && S.project.floats.find(f => f.id === S.floatId); }
function isLive(fl) { return fl && S.engine.active_float === fl.id && S.engine.output; }

async function loadState() {
  const st = await api("GET", "/api/state");
  S.project = st.project; S.rev = st.project.rev || 0; S.engine = st.engine; S.sim = st.sim; S.settings = st.settings || {}; S.version = st.version;
  MODES = {}; MODE_LIST = st.modes;
  for (const m of st.modes) { (MODES[m.variant] = MODES[m.variant] || {})[m.mode] = m; }
  if (!curFloat() && S.project.floats.length) S.floatId = S.project.floats[0].id;
}

// persist float edits (patch / names) with a short debounce
function saveFloat(fl, immediate) {
  clearTimeout(S.saveTimer);
  S.savePending = true;
  const go = async () => {
    try {
      const body = JSON.parse(JSON.stringify(fl));
      delete body.problems; delete body.live; delete body.looks;
      body.rev = fl.rev || 0;
      const res = await api("PUT", "/api/floats/" + fl.id, body);
      fl.rev = res.rev;
      S.savePending = false;
      await refreshProblems();
    } catch (e) {
      S.savePending = false;
      toast(e.message, "bad");
      if (/another device/.test(e.message)) { await reloadFloat(fl.id); renderMain(); }
    }
  };
  if (immediate) return go();
  S.saveTimer = setTimeout(go, 350);
}
async function refreshProblems() {
  const st = await api("GET", "/api/state");
  S.rev = st.project.rev || 0;
  for (const f of st.project.floats) {
    const mine = S.project.floats.find(x => x.id === f.id);
    if (mine) mine.problems = f.problems;
  }
  renderRail();
  const pb = $("#patchProblems");
  if (pb) pb.replaceWith(problemsBox(curFloat()));
}

// live look changes, merged and throttled
const liveQ = { pending: {}, timer: null, inflight: false };
function sendLive(fl, changes) {
  ensureLive(fl);  // touching a control takes control (like a console after Release)
  for (const [id, st] of Object.entries(changes)) {
    fl.live[id] = Object.assign({}, DEFAULT_STATE, fl.live[id] || {}, st);
    liveQ.pending[id] = Object.assign(liveQ.pending[id] || {}, st);
  }
  if (!liveQ.timer) liveQ.timer = setTimeout(() => flushLive(fl.id), 45);
}
async function flushLive(fid) {
  liveQ.timer = null;
  if (liveQ.inflight) { liveQ.timer = setTimeout(() => flushLive(fid), 30); return; }
  const changes = liveQ.pending; liveQ.pending = {};
  if (!Object.keys(changes).length) return;
  liveQ.inflight = true;
  try { await api("POST", `/api/floats/${fid}/live`, { changes }); }
  catch (e) { toast(e.message, "bad"); }
  finally { liveQ.inflight = false; }
}

// ------------------------------------------------------------------ top bar & status
function renderStatus() {
  const pill = $("#statusPill"), txt = $("#statusText");
  const e = S.engine;
  const live = S.project && S.project.floats.find(f => f.id === e.active_float);
  pill.className = "status-pill";
  if (e.bind_error) { pill.classList.add("warn"); txt.textContent = "Port busy"; pill.title = e.bind_error; }
  else if (e.output && e.send_error) { pill.classList.add("warn"); txt.textContent = "Can't reach box"; pill.title = e.send_error + ". Check the cable and the Mac's Ethernet IP (Setup)."; }
  else if (e.output && live) { pill.classList.add("live"); txt.textContent = "Live · " + (live.code || live.name); pill.title = "Sending to " + live.name; }
  else if (S.sim) { pill.classList.add("sim"); txt.textContent = "Simulator on"; pill.title = "Virtual E-Box is running"; }
  else { txt.textContent = "Not sending"; pill.title = "Nothing is being sent to the boxes"; }
  if (e.paused_for_rdm) { txt.textContent = "Talking to fixtures"; }
  if (e.hold) { pill.className = "status-pill warn"; txt.textContent = "Held dark"; pill.title = "A mode/address change didn't finish, so output is held at zero. Press Clear hold."; }
  else if (e.sweep) { pill.className = "status-pill warn"; txt.textContent = "Address finder running"; pill.title = "Everything else is dark. Stop it on the Patch tab."; }
  const rb = $("#releaseBtn");
  rb.textContent = e.hold ? "Clear hold" : e.output ? "Release" : "Released";
  rb.className = "btn small " + (e.output ? "danger" : "off");
  const bb = $("#blackoutBtn");
  bb.className = "btn small " + (e.blackout ? "danger" : "off");
  bb.textContent = e.blackout ? "Blackout on" : "Blackout";
}

async function pollStatus() {
  try {
    const st = await api("GET", "/api/status");
    const wasJob = S.engine.job && S.engine.job.state;
    const prevActive = S.engine.active_float + ":" + S.engine.output;
    S.engine = st.engine; S.sim = st.sim;
    renderStatus();
    await maybeSync(st.rev);
    updateLiveBits();
    if ((S.engine.active_float + ":" + S.engine.output) !== prevActive) { renderRail(); renderFloatHead(); }
    if (wasJob === "running" && S.engine.job && S.engine.job.state !== "running" && S.tab === "save") renderMain();
  } catch (e) {
    $("#statusText").textContent = "No connection";
    $("#statusPill").className = "status-pill warn";
  }
}

async function maybeSync(rev) {
  if (rev === undefined || rev === S.rev) return;
  const a = document.activeElement;
  const editing = a && /INPUT|SELECT|TEXTAREA/.test(a.tagName);
  if (editing || S.pointerDown || S.savePending || $("#modalRoot").children.length || liveQ.timer || liveQ.inflight) return;
  const scroll = $("#main").scrollTop;
  await loadState();
  renderRail(); renderMain();
  $("#main").scrollTop = scroll;
}

function updateLiveBits() {
  // job progress
  const jp = $("#jobBox");
  if (jp) jp.replaceWith(jobBox());
  const sw = $("#sweepBox");
  if (sw) sw.replaceWith(sweepBox());
  const sb = $("#simBox");
  if (sb) sb.replaceWith(simBox());
}

// ------------------------------------------------------------------ rail (float list)
function renderRail() {
  const pick = $("#floatPick");
  if (!pick || !S.project) return;
  pick.innerHTML = "";
  for (const fl of S.project.floats) {
    const live = isLive(fl) ? "● " : "";
    pick.append(h("option", { value: fl.id, selected: fl.id === S.floatId }, live + (fl.code ? fl.code + "  " : "") + fl.name));
  }
  if (!S.project.floats.length) pick.append(h("option", { value: "" }, "No floats"));
  pick.onchange = () => selectFloat(pick.value);
}

async function addFloat() {
  const name = await ask("New float", "Float name", "");
  if (!name) return;
  const fl = await api("POST", "/api/floats", { name });
  fl.problems = [];
  S.project.floats.push(fl);
  selectFloat(fl.id);
}

function selectFloat(id) {
  S.floatId = id; S.sel.clear();
  localStorage.setItem("fl.float", id);
  renderRail(); renderMain();
  $("#main").scrollTop = 0;
  const fl = curFloat();
  if (fl) ensureLive(fl);  // a set-up float is controllable the moment you pick it
}

let liveReq = null;
function ensureLive(fl) {
  // Go live on this float unless it already is, has no box IP yet, or the lights are mid-change.
  const e = S.engine;
  if (isLive(fl) || liveReq || !fl.boxes.some(b => b.ip)) return liveReq;
  if (e.hold || e.applying || e.sweep || (e.job && e.job.state === "running")) return null;
  liveReq = api("POST", "/api/output", { action: "activate", float_id: fl.id })
    .then(r => { S.engine = r.engine; renderStatus(); renderRail(); renderFloatHead(); })
    .catch(err => toast(err.message, "bad"))
    .finally(() => { liveReq = null; });
  return liveReq;
}

// ------------------------------------------------------------------ main area
function renderMain() {
  const root = $("#mainInner");
  root.innerHTML = "";
  const fl = curFloat();
  if (!fl) {
    root.append(h("div", { class: "empty" }, h("img", { src: "img/logo.png", alt: "" }),
      h("div", { style: { marginTop: "20px" } }, h("button", { class: "btn gold", onclick: addFloat }, "Add a float"))));
    return;
  }
  root.append(h("div", { id: "floatHead" }));
  renderFloatHead();
  const tabs = [["look", "Look"], ["patch", "Patch"], ["save", fl.run_mode === "live_dmx" ? "Parade" : "Save"]];
  root.append(h("div", { class: "tabs" }, tabs.map(([k, label]) =>
    h("button", { class: "tab" + (S.tab === k ? " active" : ""), onclick: () => { S.tab = k; localStorage.setItem("fl.tab", k); renderMain(); } }, label))));
  const body = h("div", { id: "tabBody" });
  root.append(body);
  if (S.tab === "look") renderLook(body, fl);
  else if (S.tab === "patch") renderPatch(body, fl);
  else renderSave(body, fl);
}

function renderFloatHead() {
  const el = $("#floatHead");
  const fl = curFloat();
  if (!el || !fl) return;
  el.innerHTML = "";
  const live = isLive(fl);
  const noIp = fl.boxes.some(b => !b.ip);
  const errs = (fl.problems || []).some(p => p.level === "error");
  el.append(h("div", { class: "float-head" },
    h("div", { class: "grow" },
      h("h1", null, fl.name),
      h("div", { class: "btn-row", style: { marginTop: "6px" } },
        fl.run_mode === "live_dmx" ? h("span", { class: "chip-warn", style: { cursor: "default" } }, "Live DMX float") : null,
        noIp ? h("button", { class: "chip-warn", onclick: () => { S.tab = "patch"; renderMain(); } }, "Set box IP") : null,
        errs ? h("button", { class: "chip-warn", onclick: () => { S.tab = "patch"; renderMain(); } }, "Address overlap") : null)),
    S.engine.hold && live
      ? h("button", { class: "btn danger", onclick: () => $("#releaseBtn").click() }, "Held dark · Clear hold")
      : live
      ? h("span", { class: "status-pill live" }, h("span", { class: "dot" }), "Live")
      : h("button", { class: "btn gold", onclick: () => goLive(fl) }, "Go live")));
}

async function goLive(fl) {
  try {
    const r = await api("POST", "/api/output", { action: "activate", float_id: fl.id });
    S.engine = r.engine;
    renderStatus(); renderRail(); renderMain();
    toast("Live on " + fl.name, "ok");
  } catch (e) { toast(e.message, "bad"); }
}

// ------------------------------------------------------------------ LOOK tab
function renderLook(body, fl) {
  if (!fl.fixtures.length) {
    body.append(h("div", { class: "card" }, h("p", null, "No fixtures on this float yet."),
      h("button", { class: "btn", onclick: () => { S.tab = "patch"; renderMain(); } }, "Go to Patch")));
    return;
  }
  const layout = h("div", { class: "look-layout" });
  const left = h("div");
  const right = h("div", { class: "panel" });
  layout.append(left, right);
  body.append(layout);

  // selection bar
  const quick = [
    ["All", () => fl.fixtures.map(f => f.id)],
    ["Tunable white", () => fl.fixtures.filter(f => f.variant === "TW").map(f => f.id)],
    ["RGB + White", () => fl.fixtures.filter(f => f.variant === "RGBW").map(f => f.id)],
  ];
  const types = [...new Set(fl.fixtures.map(f => f.type_id).filter(Boolean))].sort();
  const bar = h("div", { class: "select-bar" },
    quick.map(([label, fn]) => h("button", { class: "chip", onclick: () => { S.sel = new Set(fn()); refreshLook(); } }, label)),
    types.length > 1 ? types.map(t => h("button", { class: "chip", onclick: () => { S.sel = new Set(fl.fixtures.filter(f => f.type_id === t).map(f => f.id)); refreshLook(); } }, t)) : null,
    h("button", { class: "chip", onclick: () => { S.sel.clear(); refreshLook(); } }, "None"));
  left.append(bar);

  const grid = h("div", { class: "fx-grid", id: "fxGrid" });
  left.append(grid);
  left.append(h("hr", { class: "rule" }));
  left.append(looksCard(fl));

  function tile(fx) {
    const st = stateOf(fl, fx);
    const sel = S.sel.has(fx.id);
    const addrTxt = fx.address ? "ch " + fx.address : "no address";
    return h("div", {
      class: "fx" + (sel ? " sel" : ""), "data-id": fx.id,
      onclick: (e) => {
        if (S.sel.has(fx.id)) S.sel.delete(fx.id); else S.sel.add(fx.id);
        refreshLook();
      },
    },
      sel ? h("div", { class: "check" }, "✓") : null,
      !fx.address ? h("div", { class: "warn-dot", title: "No DMX address" }) : null,
      h("div", { class: "swatch", style: { background: previewCss(fx, st) } }),
      h("div", { class: "lbl", title: fx.notes || "" }, fx.label || "Fixture"),
      h("div", { class: "sub" }, h("span", { class: "type" }, fx.variant === "RGBW" ? "RGB+W" : fx.variant), h("span", null, Math.round(st.dim * 100) + "%")),
      h("div", { class: "sub mono", style: { fontSize: "10px" } }, mixText(fx, st, true)),
      null);
  }
  function refreshTiles() {
    grid.innerHTML = "";
    for (const fx of fl.fixtures) grid.append(tile(fx));
  }
  function refreshLook() { refreshTiles(); renderPanel(); }
  S._refreshTiles = refreshTiles;

  function renderPanel() {
    right.innerHTML = "";
    const chosen = fl.fixtures.filter(f => S.sel.has(f.id));
    const card = h("div", { class: "card" });
    right.append(card);
    if (!chosen.length) {
      card.append(h("div", { class: "sel-summary" }, "Tap lights to select"));
      card.append(paletteSection(fl, [], null, renderPanel));
      return;
    }
    const variants = new Set(chosen.map(effVariant));
    const states = chosen.map(f => stateOf(fl, f));
    const texts = [...new Set(chosen.map(f => Math.round(stateOf(fl, f).dim * 100) + "% · " + mixText(f, stateOf(fl, f), false)))];
    card.append(h("div", { class: "sel-summary" }, chosen.length === 1 ? chosen[0].label : chosen.length + " selected"),
      h("div", { class: "mono", id: "mixLine", style: { fontSize: "13px", color: "var(--champagne)", margin: "2px 0 6px" } }, texts.length === 1 ? texts[0] : "Mixed values"));

    const apply = (partial) => {
      const changes = {};
      for (const f of chosen) changes[f.id] = typeof partial === "function" ? partial(f, stateOf(fl, f)) : partial;
      sendLive(fl, changes);
      refreshTiles();
      const ml = $("#mixLine");
      if (ml) {
        const t = [...new Set(chosen.map(f => Math.round(stateOf(fl, f).dim * 100) + "% · " + mixText(f, stateOf(fl, f), false)))];
        ml.textContent = t.length === 1 ? t[0] : "Mixed values";
      }
    };

    // intensity
    const dims = states.map(s => s.dim);
    card.append(sliderCtl({
      label: "Brightness", min: 0, max: 100, step: 1, value: Math.round(dims[0] * 100), mixed: new Set(dims.map(d => Math.round(d * 100))).size > 1,
      fmt: v => v + "%", track: "linear-gradient(90deg,#000,#F6E3AE)",
      onInput: v => apply({ dim: v / 100 }),
    }));
    card.append(h("div", { class: "presets" },
      [0, 25, 50, 75, 80, 100].map(p => h("button", { onclick: () => { apply({ dim: p / 100 }); renderPanel(); } }, p === 0 ? "Off" : p + "%"))));

    const hasRGB = variants.has("RGBW");
    const rgbStates = chosen.filter(f => effVariant(f) === "RGBW").map(f => stateOf(fl, f));
    const kind = rgbStates.length ? (rgbStates.every(s => s.kind === "color") ? "color" : rgbStates.every(s => s.kind === "white") ? "white" : "mixed") : "white";

    if (hasRGB) {
      card.append(h("div", { class: "ctl" }, h("div", { class: "ctl-label" }, h("span", { class: "kicker small" }, "RGB")),
        h("div", { class: "seg" },
          h("button", { class: kind === "white" ? "on" : "", onclick: () => { apply((f) => effVariant(f) === "RGBW" ? { kind: "white" } : {}); renderPanel(); } }, "White"),
          h("button", { class: kind === "color" ? "on" : "", onclick: () => { apply((f) => effVariant(f) === "RGBW" ? { kind: "color" } : {}); renderPanel(); } }, "Color"))));
    }

    // color temperature (TW, or RGB in white)
    const showCct = variants.has("TW") || (hasRGB && kind !== "color");
    if (showCct) {
      const onlyRgb = !variants.has("TW");
      const tws = chosen.filter(f => f.variant === "TW");
      const lo = onlyRgb ? 1800 : (tws.length && tws.every(f => f.mode === 7) ? 3000 : 2700), hi = 6500;
      const ccts = states.map(s => Math.round(s.cct));
      const noCtc = chosen.filter(f => effVariant(f) === "RGBW" && !MODES_ROLES(f).includes("ctc"));
      card.append(sliderCtl({
        label: "White", min: lo, max: hi, step: 50, value: clamp(ccts[0], lo, hi), mixed: new Set(ccts).size > 1,
        fmt: v => v + "K", track: `linear-gradient(90deg, ${[lo, 3000, 4000, 5000, hi].map(k => "rgb(" + kelvinToRgb(k).map(c => Math.round(c * 255)).join(",") + ")").join(",")})`,
        onInput: v => apply({ cct: v }),
      }));
      card.append(h("div", { class: "presets" },
        [2700, 3000, 4000, 5600, 6500].filter(k => k >= lo).map(k => h("button", { onclick: () => { apply({ cct: k }); renderPanel(); } }, k + "K"))));
      if (noCtc.length) card.append(h("p", { class: "hint" }, `${noCtc.length} RGB fixture(s) can't tune white in their mode.`));
      const tw7 = chosen.filter(f => f.variant === "TW" && f.mode === 7);
      if (tw7.length) {
        const boosts = tw7.map(f => !!stateOf(fl, f).boost);
        const all = boosts.every(Boolean), none = !boosts.some(Boolean);
        const only = (on) => (f) => (f.variant === "TW" && f.mode === 7 ? { boost: on } : {});
        card.append(h("div", { class: "ctl" }, h("div", { class: "ctl-label" }, h("span", { class: "kicker small" }, "Output")),
          h("div", { class: "seg" },
            h("button", { class: none ? "on" : "", onclick: () => { apply(only(false)); renderPanel(); } }, "Even"),
            h("button", { class: all ? "on" : "", onclick: () => { apply(only(true)); renderPanel(); } }, "Max output"))));
      }
    }

    // color (RGB in color)
    if (hasRGB && kind !== "white") {
      const st0 = rgbStates[0] || DEFAULT_STATE;
      const hueTrack = "linear-gradient(90deg,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)";
      card.append(sliderCtl({ label: "Color", min: 0, max: 359, step: 1, value: Math.round(st0.hue), mixed: new Set(rgbStates.map(s => Math.round(s.hue))).size > 1, fmt: v => v + "°", track: hueTrack,
        onInput: v => apply(f => effVariant(f) === "RGBW" ? { hue: v, kind: "color" } : {}) }));
      const [r, g, b] = hsvToRgb(st0.hue, 1, 1).map(c => Math.round(c * 255));
      card.append(sliderCtl({ label: "Saturation", min: 0, max: 100, step: 1, value: Math.round(st0.sat * 100), mixed: false, fmt: v => v + "%", track: `linear-gradient(90deg,#fff,rgb(${r},${g},${b}))`,
        onInput: v => apply(f => effVariant(f) === "RGBW" ? { sat: v / 100, kind: "color" } : {}) }));
      card.append(sliderCtl({ label: "Add white", min: 0, max: 100, step: 1, value: Math.round(st0.white * 100), mixed: false, fmt: v => v + "%", track: "linear-gradient(90deg,#000,#fff)",
        onInput: v => apply(f => effVariant(f) === "RGBW" ? { white: v / 100, kind: "color" } : {}) }));
      const sw = [["Red", 0, 1], ["Amber", 30, 1], ["Gold", 42, .85], ["Yellow", 55, 1], ["Green", 120, 1], ["Teal", 170, 1],
        ["Cyan", 185, 1], ["Blue", 235, 1], ["Deep blue", 245, 1], ["Lavender", 265, .45], ["Magenta", 300, 1], ["Pink", 330, .55]];
      card.append(h("div", { class: "swatches" }, sw.map(([name, hue, sat]) => {
        const c = hsvToRgb(hue, sat, 1).map(x => Math.round(x * 255));
        return h("button", { title: name, style: { background: `rgb(${c.join(",")})` }, onclick: () => { apply(f => effVariant(f) === "RGBW" ? { kind: "color", hue, sat, white: 0 } : {}); renderPanel(); } });
      })));
    }

    card.append(paletteSection(fl, chosen, apply, renderPanel));

    card.append(h("hr", { class: "rule" }),
      h("div", { class: "btn-row" },
        h("button", { class: "btn small", onclick: () => flashFixtures(fl, chosen) }, "Identify"),
        chosen.length === 1 && chosen[0].address ? h("span", { class: "hint", style: { margin: 0 } }, `ch ${chosen[0].address}`) : null));
  }

  refreshTiles();
  renderPanel();
  if (S.sim) body.append(h("div", { style: { marginTop: "14px" } }, simBox()));
}

// ------------------------------------------------------------------ saved colors (shared by all floats)
function paletteCss(st) {
  let rgb;
  if (st.kind === "color") {
    rgb = hsvToRgb(st.hue || 0, st.sat === undefined ? 1 : st.sat, 1);
    const w = kelvinToRgb(6500);
    rgb = rgb.map((c, i) => Math.min(1, c + (st.white || 0) * w[i]));
  } else rgb = kelvinToRgb(st.cct || 3000);
  return `rgb(${rgb.map(c => Math.round(c * 255)).join(",")})`;
}

function paletteChanges(entry, f) {
  const st = entry.state, v = effVariant(f);
  const out = {};
  if (entry.include_dim && st.dim !== undefined) out.dim = st.dim;
  if (st.kind === "color") {
    if (v !== "RGBW") return entry.include_dim ? out : null;   // tunable white can't show a color
    Object.assign(out, { kind: "color", hue: st.hue, sat: st.sat, white: st.white });
  } else {
    if (v === "RGBW") Object.assign(out, { kind: "white", cct: st.cct });
    else if (v === "TW") out.cct = st.cct;
  }
  return out;
}

function paletteSection(fl, chosen, apply, rerender) {
  const pal = S.project.palette || (S.project.palette = []);
  const wrap = h("div", { class: "ctl" });
  wrap.append(h("div", { class: "ctl-label" }, h("span", { class: "kicker small" }, "Saved colors"),
    chosen.length ? h("button", { class: "btn tiny", onclick: () => saveColor(chosen, fl, rerender) }, "+ Save this color") : null));
  if (!pal.length) {
    wrap.append(h("div", { class: "muted", style: { fontSize: "13px" } }, chosen.length
      ? "None saved yet."
      : "None saved yet."));
    return wrap;
  }
  wrap.append(h("div", { class: "presets" }, pal.map(c => h("div", { class: "look-chip" },
    h("button", {
      title: c.include_dim ? "Color + brightness" : "Color only",
      disabled: !chosen.length || null,
      onclick: () => {
        let skipped = 0;
        apply((f) => { const ch = paletteChanges(c, f); if (ch === null) { skipped++; return {}; } return ch; });
        rerender();
        toast(skipped ? `Applied “${c.name}”. ${skipped} tunable white light(s) skipped: they can't show a color.` : `Applied “${c.name}”`);
      },
    }, h("span", { style: { display: "inline-block", width: "18px", height: "18px", borderRadius: "5px", marginRight: "8px", verticalAlign: "-3px", background: paletteCss(c.state), border: "1px solid rgba(255,255,255,.2)" } }),
      c.name + (c.include_dim ? " · " + Math.round((c.state.dim || 0) * 100) + "%" : "")),
    h("button", { class: "more", title: "Rename or delete", onclick: () => colorMenu(c, rerender) }, "⋯")))));
  return wrap;
}

function saveColor(chosen, fl, rerender) {
  const src = chosen[0];
  const st = stateOf(fl, src);
  const v = effVariant(src);
  const state = v === "RGBW" && st.kind === "color"
    ? { kind: "color", hue: st.hue, sat: st.sat, white: st.white, dim: st.dim }
    : { kind: "white", cct: st.cct, dim: st.dim };
  const name = h("input", { class: "f", value: state.kind === "color" ? "Color " + ((S.project.palette || []).length + 1) : Math.round(state.cct) + "K white" });
  const dimBox = h("input", { type: "checkbox" });
  const body = h("div", null,
    h("div", { style: { display: "flex", gap: "14px", alignItems: "center", marginBottom: "12px" } },
      h("div", { style: { width: "54px", height: "54px", borderRadius: "10px", background: paletteCss(state), border: "1px solid rgba(255,255,255,.2)" } }),
      h("div", { class: "muted", style: { fontSize: "13px" } }, chosen.length > 1 ? `Taken from ${src.label} (the first selected light).` : `Taken from ${src.label}.`,
        h("br"), state.kind === "color" ? "RGB color. Tunable white lights can't use it." : "White. Works on tunable white and RGB lights.")),
    h("div", { class: "field" }, h("label", null, "Name"), name),
    h("label", { style: { display: "flex", gap: "10px", alignItems: "center", marginTop: "12px" } }, dimBox, `Also save brightness (${Math.round(st.dim * 100)}%)`));
  modal("Save color", body, [{ label: "Save", cls: "gold", fn: async () => {
    try {
      const entry = await api("POST", "/api/palette", { name: name.value.trim() || "Color", state, include_dim: dimBox.checked });
      (S.project.palette = S.project.palette || []).push(entry);
      rerender(); toast(`Saved “${entry.name}” for every float`, "ok");
    } catch (e) { toast(e.message, "bad"); }
  } }]);
  setTimeout(() => name.select(), 50);
}

function colorMenu(c, rerender) {
  modal("“" + c.name + "”", h("p", { class: "muted" }, c.include_dim ? "Saved color and brightness." : "Saved color only (brightness is left alone when applied)."), [
    { label: "Rename", fn: async () => { const n = await ask("Rename color", "Name", c.name); if (n) { Object.assign(c, await api("PUT", "/api/palette/" + c.id, { name: n })); rerender(); } } },
    { label: "Delete", cls: "danger", fn: async () => { await api("DELETE", "/api/palette/" + c.id); S.project.palette = S.project.palette.filter(x => x.id !== c.id); rerender(); } },
  ]);
}

function sliderCtl({ label, min, max, step, value, mixed, fmt, track, onInput }) {
  const valEl = h("span", { class: "val" }, mixed ? "Mixed" : fmt(value));
  const fill = h("div", { class: "fill", style: { display: track ? "none" : "" } });
  const thumb = h("div", { class: "thumb" });
  const sl = h("div", { class: "slider" + (mixed ? " mixed" : ""), role: "slider", "aria-label": label, "aria-valuemin": min, "aria-valuemax": max, "aria-valuenow": value, tabindex: "0" },
    track ? h("div", { class: "track", style: { background: track } }) : null, fill, thumb);
  let cur = value;
  const place = (v) => {
    const p = (v - min) / (max - min) * 100;
    thumb.style.left = p + "%"; fill.style.width = p + "%";
  };
  place(value);
  const fromEvent = (e) => {
    const r = sl.getBoundingClientRect();
    const x = clamp((e.clientX - r.left) / r.width, 0, 1);
    let v = min + x * (max - min);
    v = Math.round(v / step) * step;
    return clamp(v, min, max);
  };
  const set = (v) => {
    if (v === cur && !mixed) return;
    mixed = false; sl.classList.remove("mixed");
    cur = v; place(v); valEl.textContent = fmt(v); sl.setAttribute("aria-valuenow", v);
    onInput(v);
  };
  sl.addEventListener("pointerdown", (e) => { sl.setPointerCapture(e.pointerId); set(fromEvent(e)); });
  sl.addEventListener("pointermove", (e) => { if (sl.hasPointerCapture(e.pointerId)) set(fromEvent(e)); });
  sl.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowUp") { set(clamp(cur + step, min, max)); e.preventDefault(); }
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") { set(clamp(cur - step, min, max)); e.preventDefault(); }
  });
  return h("div", { class: "ctl" }, h("div", { class: "ctl-label" }, h("span", { class: "kicker small" }, label), valEl), sl);
}

async function flashFixtures(fl, list) {
  // One "Identify" for everything: RDM (works any time) for lights the app knows from a Scan,
  // otherwise a DMX flash, which only reaches the lights while this float is live.
  const needLive = [];
  let rdmCount = 0, dmxCount = 0;
  for (const fx of list) {
    const b = fl.boxes.find(x => x.id === fx.box_id);
    if (fx.uid && b && b.ip) {
      try {
        await api("POST", `/api/floats/${fl.id}/rdm`, { box_id: b.id, uid: fx.uid, action: "identify", on: true });
        rdmCount++; continue;
      } catch (e) { /* fall through to a DMX flash */ }
    }
    if (isLive(fl) && fx.address) {
      await api("POST", `/api/floats/${fl.id}/flash`, { fixture_id: fx.id, seconds: 6 });
      dmxCount++;
    } else needLive.push(fx.label);
  }
  if (rdmCount || dmxCount) toast(`Identifying ${rdmCount + dmxCount}`, "ok");
  if (needLive.length) toast(`Can't reach ${needLive.join(", ")}: Scan the box first, or go live.`, "bad");
}

function looksCard(fl) {
  const card = h("div", { class: "card" });
  card.append(h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Looks"),
    h("button", { class: "btn gold small", onclick: async () => {
      const name = await ask("Save look", "Name", "Look " + (fl.looks.length + 1));
      if (!name) return;
      const look = await api("POST", `/api/floats/${fl.id}/looks`, { name });
      fl.looks.push(look); renderMain(); toast("Saved", "ok");
    } }, "Save look")));
  card.append(h("div", { class: "looks-bar" }, fl.looks.map(l => h("div", { class: "look-chip" },
    h("button", { onclick: async () => {
      await api("POST", `/api/floats/${fl.id}/looks/${l.id}/recall`);
      fl.live = JSON.parse(JSON.stringify(l.states)); renderMain();
    } }, l.name),
    h("button", { class: "more", onclick: () => lookMenu(fl, l) }, "⋯")))));
  return card;
}

function lookMenu(fl, l) {
  modal("“" + l.name + "”", h("p", { class: "muted" }, "Saved " + new Date(l.created * 1000).toLocaleString()), [
    { label: "Rename", fn: async () => { const n = await ask("Rename look", "Name", l.name); if (n) { Object.assign(l, await api("PUT", `/api/floats/${fl.id}/looks/${l.id}`, { name: n })); renderMain(); } } },
    { label: "Overwrite with current", fn: async () => { Object.assign(l, await api("PUT", `/api/floats/${fl.id}/looks/${l.id}`, { overwrite: true })); toast("Updated “" + l.name + "”", "ok"); } },
    { label: "Delete", cls: "danger", fn: async () => { await api("DELETE", `/api/floats/${fl.id}/looks/${l.id}`); fl.looks = fl.looks.filter(x => x.id !== l.id); renderMain(); } },
  ]);
}

// ------------------------------------------------------------------ PATCH tab
function problemsBox(fl) {
  const probs = (fl && fl.problems) || [];
  const box = h("div", { id: "patchProblems" });
  const errs = probs.filter(p => p.level === "error"), warns = probs.filter(p => p.level !== "error");
  if (errs.length) box.append(h("div", { class: "note bad", style: { marginBottom: "8px" } }, [...new Set(errs.map(p => p.text))].slice(0, 4).join(" · ")));
  if (warns.length) box.append(h("div", { class: "note warn", style: { marginBottom: "8px" } }, warns.length + " without an address"));
  return box;
}

function floatSettings(fl) {
  return h("div", null,
    h("div", { class: "form-row" },
      h("div", { class: "field", style: { width: "100px" } }, h("label", null, "Code"), h("input", { class: "f", value: fl.code || "", onchange: e => { fl.code = e.target.value; saveFloat(fl); renderRail(); } })),
      h("div", { class: "field", style: { flex: 1, minWidth: "200px" } }, h("label", null, "Name"), h("input", { class: "f", value: fl.name, onchange: e => { fl.name = e.target.value; saveFloat(fl); renderRail(); renderFloatHead(); } })),
      h("div", { class: "field", style: { minWidth: "220px" } }, h("label", null, "In the parade"),
        h("select", { class: "f", onchange: e => { fl.run_mode = e.target.value; saveFloat(fl, true).then(() => { renderRail(); renderMain(); }); } },
          h("option", { value: "standalone", selected: fl.run_mode !== "live_dmx" }, "Runs on its own"),
          h("option", { value: "live_dmx", selected: fl.run_mode === "live_dmx" }, "Live DMX input")))),
    h("div", { class: "btn-row", style: { marginTop: "12px" } },
      h("a", { class: "btn small", href: "/patch/" + fl.id, target: "_blank" }, "Patch sheet"),
      h("button", { class: "btn small ghost", onclick: () => { fl.boxes.push({ id: "box_" + uid8(), name: "E-Box " + "ABCDEFGH"[fl.boxes.length], ip: "", udp_port: 6454, net: 0, subnet: 0, universe: 0, notes: "" }); saveFloat(fl, true).then(() => renderMain()); } }, "+ Box"),
      h("button", { class: "btn small danger", onclick: async () => {
        if (!(await confirmBox("Delete float?", `Delete ${fl.name} and its looks?`, "Delete", "danger"))) return;
        await api("DELETE", "/api/floats/" + fl.id);
        S.project.floats = S.project.floats.filter(f => f.id !== fl.id);
        S.floatId = S.project.floats[0] ? S.project.floats[0].id : null; renderRail(); renderMain();
      } }, "Delete float")));
}

function renderPatch(body, fl) {
  const boxes = h("div", { class: "card" });
  boxes.append(h("div", { class: "card-head" }, h("h3", { class: "grow" }, fl.boxes.length > 1 ? "Boxes" : "Box"),
    h("button", { class: "btn small", onclick: () => discover(fl) }, "Find")));
  if (S.discover) boxes.append(discoverResults(fl));
  for (const b of fl.boxes) boxes.append(boxRow(fl, b));
  body.append(boxes);

  const fxCard = h("div", { class: "card" });
  fxCard.append(h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Fixtures"),
    h("button", { class: "btn small", onclick: () => autoPatch(fl) }, "Auto-address"),
    h("button", { class: "btn small ghost icon", title: "Add fixture", onclick: () => { fl.fixtures.push({ id: "fx_" + uid8(), label: "New " + (fl.fixtures.length + 1), type_id: "", variant: "TW", mode: 11, box_id: fl.boxes[0] && fl.boxes[0].id, address: null, uid: null, notes: "" }); saveFloat(fl, true).then(() => renderMain()); } }, "+")));
  fxCard.append(problemsBox(fl));
  fxCard.append(bulkBar(fl));
  fxCard.append(h("div", { class: "tbl-wrap" }, fixtureTable(fl)));
  fxCard.append(foundExtras(fl));
  // Writing the patch to the real lights belongs after the patch is edited, not next to the scan results.
  if (fl.fixtures.some(f => f.uid)) fxCard.append(h("hr", { class: "rule" }), h("div", { class: "btn-row" },
    h("button", { class: "btn small gold", onclick: () => pushAddresses(fl) }, "Send addresses to fixtures")));
  body.append(fxCard);

  const more = h("div", { class: "card" });
  more.append(h("details", { class: "more", style: { borderTop: 0, marginTop: 0, paddingTop: 0 } }, h("summary", null, "Address finder"), sweepCard(fl)));
  more.append(h("details", { class: "more" }, h("summary", null, "Float settings"), floatSettings(fl)));
  body.append(more);
  if (S.sim) body.append(simBox());
}

function boxRow(fl, b) {
  const num = (k, lo, hi) => h("input", { class: "f num", type: "number", inputmode: "numeric", min: lo, max: hi, value: b[k], style: { width: "58px" },
    onchange: e => { b[k] = clamp(parseInt(e.target.value || "0", 10), lo, hi); e.target.value = b[k]; saveFloat(fl); } });
  const scanning = S._scanning === b.id;
  return h("div", { style: { borderTop: "1px solid var(--line)", paddingTop: "12px", marginTop: "12px" } },
    h("div", { class: "form-row" },
      fl.boxes.length > 1 ? h("div", { class: "field", style: { width: "110px" } }, h("label", null, "Name"), h("input", { class: "f", value: b.name, onchange: e => { b.name = e.target.value; saveFloat(fl); } })) : null,
      h("div", { class: "field" }, h("label", null, "IP"), h("input", { class: "f ip", value: b.ip, placeholder: "2.x.x.x", inputmode: "decimal", onchange: e => { b.ip = e.target.value.trim(); saveFloat(fl); renderFloatHead(); } })),
      h("div", { class: "field" }, h("label", null, "Net · Sub · Uni"), h("div", { class: "btn-row", style: { flexWrap: "nowrap", gap: "4px" } }, num("net", 0, 127), num("subnet", 0, 15), num("universe", 0, 15))),
      h("div", { class: "btn-row", style: { marginLeft: "auto" } },
        h("button", { class: "btn small gold", disabled: !b.ip || scanning, onclick: () => scanBox(fl, b) }, scanning ? "Scanning…" : "Scan"),
        b.ip ? h("a", { class: "btn small", href: "http://" + b.ip, target: "_blank", title: "Box web page (robe / 2479)" }, "REAP") : null,
        S.sim && b.ip !== "127.0.0.1" ? h("button", { class: "btn small ghost", onclick: () => { b.ip = "127.0.0.1"; b.udp_port = S.sim.port; b.net = S.sim.net; b.subnet = S.sim.subnet; b.universe = S.sim.universe; saveFloat(fl, true).then(() => renderMain()); } }, "Use simulator") : null,
        fl.boxes.length > 1 ? h("button", { class: "btn small ghost icon", title: "Remove box", onclick: async () => {
          if (!(await confirmBox("Remove box?", `Remove ${b.name}?`, "Remove", "danger"))) return;
          fl.boxes = fl.boxes.filter(x => x.id !== b.id); await saveFloat(fl, true); renderMain();
        } }, "✕") : null)));
}

async function discover(fl) {
  toast("Looking for boxes…");
  try {
    const r = await api("POST", "/api/discover", { targets: fl.boxes.filter(b => b.ip).map(b => ({ ip: b.ip, udp_port: b.udp_port })), wait: 2.0 });
    S.discover = r.nodes;
    renderMain();
    if (!r.nodes.length) toast("No Art-Net devices answered. Check the cable, the Mac's Ethernet IP (2.0.0.10), and that the box is set to Ethernet.", "bad");
  } catch (e) { toast(e.message, "bad"); }
}

function discoverResults(fl) {
  const nodes = S.discover || [];
  const wrap = h("div", { class: "note", style: { marginBottom: "6px" } });
  wrap.append(h("div", { class: "kicker small", style: { marginBottom: "8px" } }, nodes.length + " device(s) answered"));
  if (!nodes.length) wrap.append(h("div", null, "Nothing answered. See Setup › Field checklist."));
  const t = h("table", { class: "tbl" }, h("tr", null, ["Name", "IP", "Art-Net", "RDM", ""].map(x => h("th", null, x))));
  for (const n of nodes) {
    const pa = (n.output_port_addresses || [])[0];
    const nsu = pa === undefined ? "–" : `${(pa >> 8) & 127} : ${(pa >> 4) & 15} : ${pa & 15}`;
    t.append(h("tr", null,
      h("td", null, h("div", { style: { fontWeight: 600 } }, n.long_name || n.short_name), h("div", { class: "uid" }, n.mac)),
      h("td", { class: "mono" }, n.ip + (n.udp_port !== 6454 ? ":" + n.udp_port : "")),
      h("td", { class: "mono" }, nsu),
      h("td", null, n.rdm_capable ? "yes" : "?"),
      h("td", null, h("div", { class: "btn-row" }, fl.boxes.map(b => h("button", { class: "btn tiny", onclick: () => {
        b.ip = n.ip; b.udp_port = n.udp_port || 6454;
        if (pa !== undefined) { b.net = (pa >> 8) & 127; b.subnet = (pa >> 4) & 15; b.universe = pa & 15; }
        saveFloat(fl, true).then(() => { S.discover = null; renderMain(); toast(b.name + " set to " + n.ip, "ok"); });
      } }, "Use for " + b.name))))));
  }
  wrap.append(h("div", { class: "tbl-wrap" }, t));
  wrap.append(h("button", { class: "btn tiny ghost", style: { marginTop: "8px" }, onclick: () => { S.discover = null; renderMain(); } }, "Hide"));
  return wrap;
}

function bulkBar(fl) {
  // Edit many fixtures at once in the patch; "Send addresses to fixtures" then writes them to the lights.
  for (const id of [...S.patchSel]) if (!fl.fixtures.some(f => f.id === id)) S.patchSel.delete(id);
  const chosen = fl.fixtures.filter(f => S.patchSel.has(f.id));
  if (!chosen.length) return h("div");
  const types = [...new Set(chosen.map(f => f.variant))];
  const typeSel = h("select", { class: "f", style: { width: "auto" } },
    h("option", { value: "" }, types.length === 1 ? { TW: "TW", RGBW: "RGBW", PW: "White" }[types[0]] : "Mixed types"),
    [["TW", "TW"], ["RGBW", "RGBW"], ["PW", "White"]].map(([v, l]) => h("option", { value: v }, "→ " + l)));
  const modeFor = (v) => Object.values(MODES[v] || {}).map(m => m.mode);
  const modeSel = h("select", { class: "f", style: { width: "auto" } });
  const fillModes = () => {
    const v = typeSel.value || (types.length === 1 ? types[0] : null);
    const modes = v ? modeFor(v) : [7];  // mixed types: only Mode 7 is common to all
    modeSel.innerHTML = "";
    modeSel.append(h("option", { value: "" }, "Mode…"), ...modes.map(m => {
      const fp = ((MODES[v === "TW" && m === 7 ? "RGBW" : (v || "RGBW")] || {})[m] || {}).footprint;
      return h("option", { value: m }, "Mode " + m + (fp ? " · " + fp + " ch" : ""));
    }));
  };
  typeSel.onchange = fillModes; fillModes();
  const apply = async () => {
    const v = typeSel.value, m = parseInt(modeSel.value, 10);
    if (!v && !m) { toast("Pick a type or a mode", "bad"); return; }
    for (const fx of chosen) {
      if (v) { fx.variant = v; fx.mode = { TW: 11, RGBW: 1, PW: 13 }[v]; }
      if (m && (modeFor(fx.variant).includes(m))) fx.mode = m;
    }
    await saveFloat(fl, true); renderMain();
    toast(`Updated ${chosen.length} in the patch. Auto-address, then Send to lights.`, "ok");
  };
  const autoSel = async () => {
    const boxes = [...new Set(chosen.map(f => f.box_id))];
    try {
      for (const b of boxes) await api("POST", `/api/floats/${fl.id}/autopatch`, { box_id: b, start: 1, fixture_ids: chosen.filter(f => f.box_id === b).map(f => f.id) });
      await reloadFloat(fl.id); renderMain(); toast("Addresses assigned. Send to lights when ready.", "ok");
    } catch (e) { toast(e.message, "bad"); }
  };
  return h("div", { class: "note", style: { margin: "8px 0", display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" } },
    h("strong", null, chosen.length + " selected"), typeSel, modeSel,
    h("button", { class: "btn small gold", onclick: apply }, "Set"),
    h("button", { class: "btn small", onclick: autoSel }, "Auto-address these"),
    h("button", { class: "btn small ghost", onclick: () => { S.patchSel.clear(); renderMain(); } }, "Clear"));
}

function scannedFor(fl) {
  // uid -> {d, b} for every light the last scan of each box found
  const out = {};
  for (const b of fl.boxes) for (const d of ((S.scan[b.id] || {}).devices || [])) if (d.ok) out[d.uid] = { d, b };
  return out;
}

async function rdmDo(fl, b, d, action, extra) {
  try {
    const r = await api("POST", `/api/floats/${fl.id}/rdm`, Object.assign({ box_id: b.id, uid: d.uid, action }, extra || {}));
    if (r.info) Object.assign(d, { address: r.info.address, mode: r.info.mode, personality: r.info.personality });
    if (action === "identify") toast(extra && extra.on === false ? "Stopped" : "Identifying for 15 s", "ok");
    if (action === "address" || action === "mode") await reloadFloat(fl.id);
    renderMain();
  } catch (e) { toast(e.message, "bad"); }
}

function lightCell(fl, fx, found, scanned) {
  if (!scanned) return h("td", { class: "muted" }, "–");
  const hit = fx.uid && found[fx.uid];
  if (!hit) return h("td", null, h("span", { style: { color: fx.uid ? "var(--bad)" : "var(--muted)" } }, fx.uid ? "not found" : "not linked"));
  const { d, b } = hit;
  const same = d.address === fx.address && d.mode === fx.mode;
  return h("td", null, h("div", { class: "btn-row", style: { flexWrap: "nowrap" } },
    h("span", { style: { color: same ? "var(--ok)" : "var(--warn)", whiteSpace: "nowrap" }, title: d.label || d.uid },
      same ? "✓" : `at ${d.address} · M${d.mode}`),
    h("button", { class: "btn tiny ghost", title: "Light settings", onclick: () => paramsModal(fl, b, d) }, "⋯")));
}

function fixtureTable(fl) {
  const found = scannedFor(fl);
  const scanned = fl.boxes.some(b => S.scan[b.id] && !S.scan[b.id].error);
  const t = h("table", { class: "tbl" });
  const ids = fl.fixtures.map(f => f.id);
  const allOn = ids.length && ids.every(id => S.patchSel.has(id));
  const tickAll = h("input", { type: "checkbox", checked: allOn || null, title: "Select all", style: { width: "20px", height: "20px" },
    onchange: e => { ids.forEach(id => e.target.checked ? S.patchSel.add(id) : S.patchSel.delete(id)); renderMain(); } });
  t.append(h("tr", null, h("th", null, tickAll), ["", "Fixture", "Type", "Mode", fl.boxes.length > 1 ? "Box" : null, "Address", scanned ? "Light" : null, ""].filter(x => x !== null).map(x => h("th", null, x))));
  const errIds = new Set((fl.problems || []).filter(p => p.level === "error").map(p => p.fixture));
  fl.fixtures.forEach((fx) => {
    const st = stateOf(fl, fx);
    const modeOpts = Object.values(MODES[fx.variant] || {}).map(m => h("option", { value: m.mode, selected: m.mode === fx.mode }, m.mode + " · " + m.footprint + " ch"));
    t.append(h("tr", { class: errIds.has(fx.id) ? "err" : "" },
      h("td", null, h("input", { type: "checkbox", checked: S.patchSel.has(fx.id) || null, style: { width: "20px", height: "20px" },
        onchange: e => { e.target.checked ? S.patchSel.add(fx.id) : S.patchSel.delete(fx.id); renderMain(); } })),
      h("td", null, h("div", { style: { width: "22px", height: "22px", borderRadius: "6px", background: previewCss(fx, st), border: "1px solid rgba(255,255,255,.1)" } })),
      h("td", null, h("input", { class: "f", style: { minWidth: "96px" }, value: fx.label, title: fx.notes || "", onchange: e => { fx.label = e.target.value; saveFloat(fl); } })),
      h("td", null, h("select", { class: "f", style: { width: "84px" }, onchange: e => { fx.variant = e.target.value; fx.mode = { TW: 11, RGBW: 1, PW: 13 }[fx.variant]; saveFloat(fl, true).then(() => renderMain()); } },
        [["TW", "TW"], ["RGBW", "RGBW"], ["PW", "White"]].map(([v, l]) => h("option", { value: v, selected: v === fx.variant }, l)))),
      h("td", null, h("select", { class: "f", style: { width: "96px" }, onchange: e => { fx.mode = parseInt(e.target.value, 10); saveFloat(fl, true).then(() => renderMain()); } }, modeOpts)),
      fl.boxes.length > 1 ? h("td", null, h("select", { class: "f", onchange: e => { fx.box_id = e.target.value; saveFloat(fl); } }, fl.boxes.map(b => h("option", { value: b.id, selected: b.id === fx.box_id }, b.name)))) : null,
      h("td", null, h("input", { class: "f num", type: "number", inputmode: "numeric", min: 1, max: 512, value: fx.address || "", placeholder: "–",
        onchange: e => { const v = parseInt(e.target.value, 10); fx.address = isNaN(v) ? null : clamp(v, 1, 512); saveFloat(fl, true).then(() => renderMain()); } })),
      scanned ? lightCell(fl, fx, found, scanned) : null,
      h("td", null, h("div", { class: "btn-row", style: { flexWrap: "nowrap" } },
        h("button", { class: "btn tiny", disabled: (!fx.address && !fx.uid) || null, onclick: () => flashFixtures(fl, [fx]) }, "Identify"),
        h("button", { class: "btn tiny ghost", title: "Remove", onclick: async () => {
          if (!(await confirmBox("Remove fixture?", `Remove ${fx.label}?`, "Remove", "danger"))) return;
          fl.fixtures = fl.fixtures.filter(x => x.id !== fx.id); delete fl.live[fx.id]; await saveFloat(fl, true); renderMain();
        } }, "✕")))));
  });
  return t;
}

function foundExtras(fl) {
  // Scan errors, and lights the scan found that aren't matched to anything in the patch.
  const wrap = h("div");
  const linkedUids = new Set(fl.fixtures.map(f => f.uid).filter(Boolean));
  for (const b of fl.boxes) {
    const res = S.scan[b.id];
    if (!res) continue;
    const name = fl.boxes.length > 1 ? b.name + ": " : "";
    if (res.error) { wrap.append(h("div", { class: "note bad", style: { marginTop: "10px" } }, name + res.error)); continue; }
    const extra = res.devices.filter(d => d.ok && !linkedUids.has(d.uid));
    if (!extra.length) continue;
    const freeFx = fl.fixtures.filter(f => f.box_id === b.id && !f.uid);
    wrap.append(h("div", { class: "note warn", style: { marginTop: "10px" } },
      h("strong", null, `${name}${extra.length} light(s) not matched yet. Identify it, then say which fixture it is.`),
      extra.map(d => {
        const pick = h("select", { class: "f", style: { width: "auto" }, onchange: e => {
          if (!e.target.value) return;
          S.links[d.uid] = e.target.value;
          applyScan(fl, b, { devices: [d] });
        } },
          h("option", { value: "" }, "This is…"),
          freeFx.map(f => h("option", { value: f.id }, f.label)),
          h("option", { value: "__new" }, "A new fixture"));
        return h("div", { class: "btn-row", style: { marginTop: "8px" } },
          h("span", { style: { flex: 1 } }, `${d.label || d.uid} · ch ${d.address} · Mode ${d.mode}`),
          h("button", { class: "btn tiny", onclick: () => rdmDo(fl, b, d, "identify", { on: true }) }, "Identify"),
          pick);
      })));
  }
  return wrap;
}

async function autoPatch(fl) {
  const boxes = fl.boxes;
  const body = h("div");
  const start = h("input", { class: "f num", type: "number", value: 1, min: 1, max: 512 });
  const boxSel = h("select", { class: "f" }, boxes.map(b => h("option", { value: b.id }, b.name)));
  body.append(h("p", null, "Gives every fixture on the box its own block of channels, top to bottom in the list, based on each fixture's mode. Then press Send addresses to fixtures at the bottom of the list."),
    h("div", { class: "form-row" }, boxes.length > 1 ? h("div", { class: "field" }, h("label", null, "Box"), boxSel) : null,
      h("div", { class: "field" }, h("label", null, "Start at"), start)));
  modal("Auto-address", body, [{ label: "Auto-address", cls: "gold", fn: async () => {
    try {
      await api("POST", `/api/floats/${fl.id}/autopatch`, { box_id: boxSel.value, start: parseInt(start.value, 10) || 1 });
      await reloadFloat(fl.id); renderMain(); toast("Addresses assigned", "ok");
    } catch (e) { toast(e.message, "bad"); }
  } }]);
}

async function reloadFloat(fid) {
  const fresh = await api("GET", "/api/floats/" + fid);
  const i = S.project.floats.findIndex(f => f.id === fid);
  S.project.floats[i] = fresh;
  await refreshProblems();
  return fresh;
}

async function scanBox(fl, b) {
  S._scanning = b.id; renderMain();
  try {
    const r = await api("POST", `/api/floats/${fl.id}/scan`, { box_id: b.id });
    S.scan[b.id] = r;
    const n = r.devices.length;
    // Link every found light that matches exactly one unlinked patch fixture on this box by address.
    const linkedUids = new Set(fl.fixtures.map(f => f.uid).filter(Boolean));
    const auto = r.devices.filter(d => d.ok && !linkedUids.has(d.uid)).filter(d => {
      const same = fl.fixtures.filter(f => f.box_id === b.id && !f.uid && f.address === d.address);
      const rivals = r.devices.filter(x => x.ok && x.address === d.address);
      if (same.length === 1 && rivals.length === 1) { S.links[d.uid] = same[0].id; return true; }
      return false;
    });
    if (auto.length) await applyScan(fl, b, { devices: auto });
    toast(n ? `Found ${n} fixture(s) on ${b.name}` : `${b.name} answered but reported no fixtures`, n ? "ok" : "bad");
  } catch (e) {
    S.scan[b.id] = { error: e.message, devices: [] };
    toast(e.message, "bad");
  } finally { S._scanning = null; renderMain(); }
}

async function applyScan(fl, b, res) {
  let linked = 0, added = 0;
  for (const d of res.devices) {
    if (!d.ok) continue;
    const choice = S.links[d.uid];
    if (choice === "__new") {
      fl.fixtures.push({ id: "fx_" + uid8(), label: d.label || ("Found " + d.uid.slice(-4)), type_id: "", variant: d.variant_guess || "RGBW", mode: d.mode, box_id: b.id, address: d.address, uid: d.uid, notes: d.model || "" });
      S.links[d.uid] = fl.fixtures[fl.fixtures.length - 1].id;
      added++;
      continue;
    }
    if (!choice) continue;
    const fx = fl.fixtures.find(f => f.id === choice);
    if (!fx) continue;
    for (const other of fl.fixtures) if (other.uid === d.uid && other !== fx) other.uid = null;
    fx.uid = d.uid; fx.address = d.address; fx.variant = d.variant_guess || fx.variant;
    if (d.mode) fx.mode = d.mode;
    linked++;
  }
  await saveFloat(fl, true);
  renderMain();
  toast(added ? `Linked ${linked}, added ${added}` : `Linked ${linked}`, "ok");
}

async function pushAddresses(fl, b, res) {
  const todo = fl.fixtures.filter(f => f.uid && f.address);
  if (!todo.length) { toast("Scan first, so the app knows which light is which.", "bad"); return; }
  if (!(await confirmBox("Send to lights?", `Write the patch's address and mode to ${todo.length} light(s)? The float goes live, held dark, while this runs.`, "Send"))) return;
  await applyToLights(fl, { push: true }, "Sending addresses");
}

// ------------------------------------------------------------------ address finder
function sweepCard(fl) {
  const card = h("div", { style: { paddingTop: "6px" } });
  const boxSel = h("select", { class: "f" }, fl.boxes.map(b => h("option", { value: b.id }, b.name)));
  const modeSel = h("select", { class: "f" }, MODE_LIST.map(m => h("option", { value: m.variant + ":" + m.mode, selected: m.variant === "TW" && m.mode === 11 }, (m.variant === "RGBW" ? "RGB " : m.variant + " ") + "Mode " + m.mode + " (" + m.footprint + " ch)")));
  const start = h("input", { class: "f num", type: "number", value: 1, min: 1, max: 512 });
  const count = h("input", { class: "f num", type: "number", value: 30, min: 1, max: 170 });
  const secs = h("input", { class: "f num", type: "number", value: 2.5, min: 0.5, max: 10, step: 0.5 });
  card.append(h("p", { class: "hint", style: { marginTop: 0 } }, "Lights one address at a time. Watch which fixture comes on."),
    h("div", { class: "form-row", style: { marginTop: "10px" } },
      fl.boxes.length > 1 ? h("div", { class: "field" }, h("label", null, "Box"), boxSel) : null,
      h("div", { class: "field", style: { minWidth: "180px" } }, h("label", null, "Mode"), modeSel),
      h("div", { class: "field" }, h("label", null, "Start"), start),
      h("div", { class: "field" }, h("label", null, "Steps"), count),
      h("div", { class: "field" }, h("label", null, "Sec"), secs),
      h("button", { class: "btn gold", onclick: async () => {
        const [variant, mode] = modeSel.value.split(":");
        try {
          await api("POST", `/api/floats/${fl.id}/sweep`, { box_id: boxSel.value, variant, mode: parseInt(mode, 10), start: parseInt(start.value, 10), count: parseInt(count.value, 10), seconds: parseFloat(secs.value) });
          await pollStatus(); renderFloatHead();
        } catch (e) { toast(e.message, "bad"); }
      } }, "Start")),
    sweepBox());
  return card;
}

function sweepBox() {
  const sw = S.engine.sweep;
  const box = h("div", { id: "sweepBox" });
  if (!sw) return box;
  const fl = curFloat();
  const ctl = (a) => api("POST", `/api/floats/${fl.id}/sweep`, { action: a }).then(pollStatus);
  box.append(h("div", { class: "note", style: { marginTop: "14px", display: "flex", alignItems: "center", gap: "18px", flexWrap: "wrap" } },
    h("div", null, h("div", { class: "kicker small" }, "Now lit"), h("div", { style: { font: "900 48px/1 var(--serif)", color: "var(--champagne)" } }, sw.current),
      h("div", { class: "muted", style: { fontSize: "12px" } }, `${sw.footprint} ch · step ${sw.index + 1} of ${sw.addresses.length}${sw.paused ? " · paused" : ""}`)),
    h("div", { class: "btn-row" },
      h("button", { class: "btn small", onclick: () => ctl("prev") }, "‹ Prev"),
      sw.paused ? h("button", { class: "btn small", onclick: () => ctl("resume") }, "Resume") : h("button", { class: "btn small", onclick: () => ctl("pause") }, "Pause"),
      h("button", { class: "btn small", onclick: () => ctl("next") }, "Next ›"),
      h("button", { class: "btn small danger", onclick: () => ctl("stop") }, "Stop"))));
  return box;
}

// ------------------------------------------------------------------ virtual box
function simBox() {
  const box = h("div", { id: "simBox" });
  if (!S.sim) return box;
  const card = h("div", { class: "card" });
  card.append(h("div", { class: "card-head" }, h("h3", { class: "grow" }, "Virtual E-Box (simulator)"),
    h("span", { class: "kicker small" }, `${S.sim.ip}:${S.sim.port} · Art-Net ${S.sim.net}:${S.sim.subnet}:${S.sim.universe} · ${S.sim.receiving ? "receiving" : "idle"}`)));
  card.append(h("div", { class: "sim-row" }, S.sim.modules.map(m => h("div", { class: "sim-mod" },
    h("div", { class: "bulb", style: { background: `rgb(${m.rgb.join(",")})`, color: `rgb(${m.rgb.join(",")})` } }),
    h("div", { class: "uid" }, m.uid),
    h("div", { style: { fontSize: "12px" } }, `${m.variant} · M${m.mode} · @${m.address}`),
    m.saved_initial ? h("div", { style: { fontSize: "11px", color: "var(--ok)" } }, "look saved") : null))));
  box.append(card);
  return box;
}

// ------------------------------------------------------------------ SAVE tab
function jobBox() {
  const job = S.engine.job;
  const box = h("div", { id: "jobBox" });
  const fl = curFloat();
  if (!job || !fl || job.float !== fl.id) return box;
  const running = job.state === "running";
  box.append(h("div", { class: "note " + (job.state === "done" ? "ok" : job.state === "error" ? "bad" : ""), style: { marginTop: "14px" } },
    h("div", { style: { fontWeight: 700, marginBottom: "6px" } }, (job.kind === "verify" ? "Check: " : "Save: ") + job.step),
    h("div", { class: "progress" }, h("div", { style: { width: Math.round(job.progress * 100) + "%" } })),
    job.message ? h("div", { style: { marginTop: "6px" } }, job.message) : null,
    job.skipped && job.skipped.length ? h("div", { style: { marginTop: "6px", fontSize: "13px" } }, "Skipped: " + job.skipped.map(s => s.label + " (" + s.why + ")").join(", ")) : null,
    !running && job.state === "done" && job.kind === "save" ? h("div", { style: { marginTop: "6px" } }, "Next: press “Check what’s saved” and watch the float.") : null));
  return box;
}

async function paramsModal(fl, b, d) {
  const body = h("div", null, h("p", { class: "muted" }, "Reading settings from " + d.uid + "…"));
  modal("Fixture settings", body);
  try {
    const r = await api("POST", `/api/floats/${fl.id}/rdm`, { box_id: b.id, uid: d.uid, action: "params" });
    body.innerHTML = "";
    body.append(h("p", { class: "muted", style: { marginTop: 0 } }, `${d.model || "Fixture"} · ${d.uid}. These are the maker's own settings, read from the fixture. Change only what you understand.`));
    if (!r.params.length) { body.append(h("div", { class: "note warn" }, "This fixture didn't list any manufacturer settings over RDM.")); return; }
    const t = h("table", { class: "tbl" }, h("tr", null, ["Setting", "Now", "Range", ""].map(x => h("th", null, x))));
    for (const p of r.params) {
      const inp = h("input", { class: "f num", type: "number", value: p.value === null ? "" : p.value, min: p.min, max: p.max });
      t.append(h("tr", null,
        h("td", null, h("div", { style: { fontWeight: 600 } }, p.description), h("div", { class: "uid" }, p.pid_hex + " · " + p.data_type + " · " + p.command_class),
          r.save_pid === p.pid ? h("div", { style: { color: "var(--gold)", fontSize: "12px" } }, "Save-look setting: 1 = keep the current look") : null),
        h("td", { class: "mono" }, p.value === null ? "–" : p.value),
        h("td", { class: "mono" }, p.min + "–" + p.max),
        h("td", null, p.can_set ? h("div", { class: "btn-row", style: { flexWrap: "nowrap" } }, inp,
          h("button", { class: "btn tiny", onclick: async () => {
            try { await api("POST", `/api/floats/${fl.id}/rdm`, { box_id: b.id, uid: d.uid, action: "set_param", pid: p.pid, value: parseInt(inp.value, 10) }); toast(p.description + " set", "ok"); paramsModal(fl, b, d); }
            catch (e) { toast(e.message, "bad"); }
          } }, "Set")) : h("span", { class: "muted" }, "read only"))));
    }
    body.append(h("div", { class: "tbl-wrap" }, t));
  } catch (e) { body.innerHTML = ""; body.append(h("div", { class: "note bad" }, e.message)); }
}

function renderLiveDmxSetup(body, fl) {
  const probs = fl.problems || [];
  const overlap = probs.some(p => p.level === "error");
  const missing = fl.fixtures.filter(f => !f.address).length;
  const unlinked = fl.fixtures.filter(f => !f.uid).length;
  const row = (ok, text) => h("div", { class: "note " + (ok ? "ok" : "warn"), style: { marginBottom: "8px" } }, (ok ? "✓ " : "") + text);
  body.append(h("div", { class: "card" },
    h("h2", { style: { marginBottom: "12px" } }, "Live DMX float"),
    row(!missing, missing ? missing + " without an address" : "Every fixture has an address"),
    row(!overlap, overlap ? "Addresses overlap" : "No overlaps"),
    row(!unlinked, unlinked ? unlinked + " not confirmed by Scan" : "All confirmed by Scan"),
    h("div", { class: "btn-row", style: { marginTop: "12px" } },
      h("button", { class: "btn", onclick: () => { S.tab = "patch"; renderMain(); } }, "Patch"),
      h("a", { class: "btn gold", href: "/patch/" + fl.id, target: "_blank" }, "Print patch sheet")),
    h("p", { class: "hint" }, "Box for the parade: Output Data Enabled, DMX Input to match show control, then power-cycle.")));
}

function renderSave(body, fl) {
  if (fl.run_mode === "live_dmx") return renderLiveDmxSetup(body, fl);
  const busy = S.engine.job && S.engine.job.state === "running";
  const not7 = fl.fixtures.filter(f => f.mode !== 7);
  const ready = fl.fixtures.filter(f => f.mode === 7 && f.address);
  const unlinked = not7.filter(f => !f.uid);
  const step = (n, title, ...kids) => h("div", { class: "card" }, h("div", { class: "kicker small" }, "Step " + n), h("h3", { style: { margin: "4px 0 10px" } }, title), ...kids);

  body.append(step(1, not7.length ? `Switch ${not7.length} light(s) to Mode 7` : "All lights are in Mode 7 ✓",
    not7.length ? h("div", null,
      h("button", { class: "btn gold", disabled: busy || null, onclick: () => toMode7(fl, not7) }, "Switch to Mode 7"),
      unlinked.length ? h("p", { class: "hint" }, `Scan first (Patch tab) so the app can change ${unlinked.length} of them on the lights themselves.`) : null)
    : h("p", { class: "hint" }, "Addresses: " + ready.map(f => f.address).join(", "))));

  body.append(step(2, "Set the look",
    h("button", { class: "btn", onclick: () => { S.tab = "look"; renderMain(); } }, "Go to Look")));

  body.append(step(3, "Save it into the lights",
    h("div", { class: "btn-row" },
      h("button", { class: "btn gold big", disabled: !ready.length || busy || null, onclick: () => runSave(fl, ready, false) }, `Save look (${ready.length})`)),
    jobBox()));
  // No "check" over DMX: the lights' "show saved values" command shows nothing on the real Calumma
  // (field test 2026-10-02). Play saved look (step 4) is the only true check.

  const boxCard = step(4, "Play it on its own (this is the check)", h("div", { id: "boxOut" }, h("p", { class: "hint" }, "Reading the box…")));
  body.append(boxCard);
  refreshBoxOut(fl);
}

async function refreshBoxOut(fl) {
  const el = $("#boxOut");
  if (!el) return;
  const rows = [];
  for (const b of fl.boxes.filter(b => b.ip)) {
    let st = null;
    try { st = await api("POST", `/api/floats/${fl.id}/box`, { box_id: b.id }); } catch (e) { st = { error: e.message }; }
    const playing = st && st.output_data === "disabled";
    rows.push(h("div", { style: { margin: "8px 0" } },
      h("div", null, (fl.boxes.length > 1 ? b.name + ": " : "") + (st.error ? "can't read the box" : playing ? "Playing the saved look (app can't control it)" : "Following the app")),
      st.error ? null : h("button", { class: "btn " + (playing ? "" : "gold"), style: { marginTop: "6px" }, onclick: async () => {
        const msg = playing ? "Give control back to the app? The box restarts (about 10 s)." : "Play the saved look? The box stops listening to the app and restarts (about 10 s).";
        if (!(await confirmBox(playing ? "Back to app control" : "Play saved look", msg, "Yes"))) return;
        try { await api("POST", `/api/floats/${fl.id}/box`, { box_id: b.id, action: "output_data", enabled: playing }); toast("Box restarting…", "ok"); }
        catch (e) { toast(e.message, "bad"); }
        setTimeout(() => refreshBoxOut(fl), 12000);
      } }, playing ? "Back to app control" : "Play saved look")));
  }
  el.innerHTML = "";
  if (!rows.length) el.append(h("p", { class: "hint" }, "Set the box IP on the Patch tab."));
  rows.forEach(r => el.append(r));
}

async function runSaveRdm(fl) {
  if (!isLive(fl)) {
    if (!(await confirmBox("Go live first?", `Go live on ${fl.name} so the fixtures show the look?`, "Go live"))) return;
    await goLive(fl);
    await new Promise(r => setTimeout(r, 800));
  }
  S._savingRdm = true; renderMain();
  try {
    const r = await api("POST", `/api/floats/${fl.id}/save_rdm`, {});
    S.saveResult = { float: fl.id, saved: r.saved, bad: r.results.filter(x => !x.ok) };
  } catch (e) { toast(e.message, "bad"); }
  finally { S._savingRdm = false; renderMain(); }
}

async function runSave(fl, list, verify) {
  if (!isLive(fl)) {
    if (!(await confirmBox("Go live?", `${fl.name} isn't live. Go live on it and continue?`, "Go live"))) return;
    await goLive(fl);
  }
  try {
    const job = await api("POST", `/api/floats/${fl.id}/save`, { fixture_ids: list.map(f => f.id), verify });
    S.engine.job = job; updateLiveBits();
  } catch (e) { toast(e.message, "bad"); }
}

async function applyToLights(fl, body, what) {
  // Runs on the Mac: output goes live and is held at zero while lights change; the patch only
  // records a light's new mode/address once that light confirms it.
  toast(what + "… (keep this page open)");
  let r;
  try { r = await api("POST", `/api/floats/${fl.id}/apply`, body); }
  catch (e) { toast(e.message, "bad"); await loadState(); renderMain(); return; }
  await loadState(); renderStatus(); renderMain();
  if (r.errors.length) modal("Some lights didn't change", h("div", null,
    r.done.length ? h("p", null, "Done: " + r.done.join(", ")) : null,
    h("ul", null, r.errors.map(e => h("li", null, e))),
    h("div", { class: "note warn" }, "Output is held at zero so nothing stray reaches the lights. Fix the problem and try again, or press Release.")));
  else toast(`${what}: ${r.done.length} light(s) done`, "ok");
}

async function toMode7(fl, list) {
  const linked = list.filter(f => f.uid);
  const unlinked = list.filter(f => !f.uid);
  if (!linked.length) { toast("Scan the box first (Patch tab) so the app knows which light is which.", "bad"); return; }
  const msg = h("div", null,
    h("p", null, `Switch ${linked.length} light(s) to Mode 7 and give them new addresses. The float goes live, held dark, while this runs.`),
    unlinked.length ? h("div", { class: "note warn" }, "Not found by Scan, skipped: " + unlinked.map(f => f.label).join(", ")) : null);
  if (!(await new Promise(res => modal("Switch to Mode 7", msg, [{ label: "Switch", cls: "gold", fn: () => res(true) }])))) return;
  await applyToLights(fl, { to_mode7: linked.map(f => f.id) }, "Switching to Mode 7");
}

// ------------------------------------------------------------------ setup
async function openSetup() {
  const net = await api("GET", "/api/network");
  const port = net.http_port;
  const ifs = net.interfaces.filter(i => !i.ip.startsWith("127."));
  const body = h("div");
  const pickWrap = h("div", { style: { margin: "10px 0" } });
  body.append(
    h("div", { class: "kicker small" }, "iPad address"),
    h("div", { style: { margin: "6px 0 12px" } }, ifs.map(i => h("div", { class: "mono", style: { fontSize: "17px", color: "var(--champagne)" } }, `http://${i.ip}:${port}`))),
    net.bind_error ? h("div", { class: "note bad", style: { marginTop: "8px" } }, "Art-Net port busy. Quit other lighting apps, then restart.") : null,
    ifs.length > 1 ? pickWrap : null,
    h("div", { class: "btn-row", style: { marginTop: "14px" } },
      h("button", { class: "btn small " + (S.sim ? "go" : "off"), onclick: async () => { const r = await api("POST", "/api/sim", { on: !S.sim }); S.sim = r.sim; openSetup(); renderMain(); } }, S.sim ? "Simulator on" : "Simulator off"),
      h("button", { class: "btn small ghost", onclick: () => { $("#modalRoot").innerHTML = ""; addFloat(); } }, "+ Float"),
      h("a", { class: "btn small", href: "/api/project", download: "dmx-scene-builder-project.json" }, "Export"),
      h("label", { class: "btn small" }, "Import", h("input", { type: "file", accept: ".json,application/json", class: "hidden", onchange: async (e) => {
        const file = e.target.files[0]; if (!file) return;
        e.target.value = "";
        try {
          const data = JSON.parse(await file.text());
          await startMerge(data);
        } catch (err) { toast("Couldn't read that file: " + err.message, "bad"); }
      } })),
      h("a", { class: "btn small ghost", href: "/guide.html", target: "_blank" }, "Field guide"),
      h("button", { class: "btn small ghost", onclick: () => { $("#modalRoot").innerHTML = ""; whiteTest(); } }, "White test"),
      h("button", { class: "btn small ghost", onclick: () => { $("#modalRoot").innerHTML = ""; twTest(); } }, "Tunable white test")),
    saveInfo(),
    h("p", { class: "hint", style: { marginTop: "14px" } }, `v${S.version}`));
  if (ifs.length > 1) pickWrap.append(networkPicker(net, ifs));
  modal("Setup", body);
}

async function startMerge(incoming) {
  let plan;
  try { plan = await api("POST", "/api/project/merge_plan", incoming); }
  catch (e) { toast(e.message, "bad"); return; }
  if (!plan.new.length && !plan.conflicts.length) {
    toast(plan.identical.length ? "Nothing new: this computer already has it" : "That file has no floats", "ok");
    return;
  }
  const resolutions = {};
  // Default to whichever copy was edited more recently; the user can flip any of them.
  for (const c of plan.conflicts) resolutions[c.id] = (c.theirs.updated || 0) > (c.mine.updated || 0) ? "theirs" : "mine";
  const when = (x) => x.updated ? new Date(x.updated * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + (x.edited_on ? " · " + x.edited_on : "") : "no edit time";
  const body = h("div");
  if (plan.new.length) body.append(h("div", { class: "note ok" }, `Add ${plan.new.length}: ` + plan.new.map(f => f.name).join(", ")));
  if (plan.identical.length) body.append(h("p", { class: "hint" }, `${plan.identical.length} already match.`));
  if (plan.conflicts.length) {
    body.append(h("div", { class: "kicker small", style: { marginTop: "12px" } }, "Changed on both. Newer one is picked:"));
    for (const c of plan.conflicts) {
      const pickTheirs = resolutions[c.id] === "theirs";
      const mineBtn = h("button", { class: pickTheirs ? "" : "on" }, `This computer · ${when(c.mine)}`);
      const theirsBtn = h("button", { class: pickTheirs ? "on" : "" }, `The file · ${when(c.theirs)}`);
      mineBtn.onclick = () => { resolutions[c.id] = "mine"; mineBtn.className = "on"; theirsBtn.className = ""; };
      theirsBtn.onclick = () => { resolutions[c.id] = "theirs"; theirsBtn.className = "on"; mineBtn.className = ""; };
      body.append(h("div", { style: { margin: "8px 0" } }, h("div", null, (c.code ? c.code + " " : "") + c.name),
        h("div", { class: "seg", style: { marginTop: "6px" } }, mineBtn, theirsBtn)));
    }
  }
  body.append(h("p", { class: "hint", style: { marginTop: "12px" } }, "Nothing on this computer is ever deleted by Import."));
  modal("Import floats", body, [{ label: "Import", cls: "gold", fn: async () => {
    try {
      const r = await api("POST", "/api/project/merge_apply", { incoming, resolutions });
      await loadState(); renderRail(); renderMain();
      toast(`Added ${r.added}, replaced ${r.replaced}`, "ok");
    } catch (e) { toast(e.message, "bad"); return true; }
  } }]);
}

function saveInfo() {
  const wrap = h("div", { style: { marginTop: "16px" } }, h("div", { class: "kicker small" }, "Saving"));
  api("GET", "/api/settings").then(st => {
    const ago = (t) => t ? Math.max(0, Math.round(Date.now() / 1000 - t)) + " s ago" : "not yet";
    wrap.append(
      h("p", { class: "hint", style: { margin: "4px 0" } }, "Every change saves on this computer within a second. Last save: " + ago(st.last_saved) + "."),
      h("div", { class: "mono", style: { fontSize: "12px", color: "var(--muted)", wordBreak: "break-all" } }, st.data_file),
      h("div", { class: "field", style: { marginTop: "10px" } }, h("label", null, "Backup copy folder"),
        st.mirror_dir ? h("div", { class: "mono", style: { fontSize: "12px", wordBreak: "break-all" } }, st.mirror_dir)
          : h("div", { class: "note warn" }, "Not set. If this computer dies, the floats go with it."),
        h("div", { class: "btn-row", style: { marginTop: "6px" } },
          h("button", { class: "btn small", onclick: () => chooseFolder(st.mirror_dir) }, st.mirror_dir ? "Change…" : "Choose…"),
          st.mirror_dir ? h("button", { class: "btn small ghost", onclick: async () => { await api("POST", "/api/settings", { mirror_dir: "" }); openSetup(); } }, "Turn off") : null)),
      st.mirror_error ? h("div", { class: "note bad" }, "Backup copy failed: " + st.mirror_error)
        : st.mirror_dir ? h("p", { class: "hint" }, "Copies here every 30 s. Last copy: " + ago(st.last_mirrored) + ".") : null);
  }).catch(() => {});
  return wrap;
}

async function chooseFolder(start) {
  // Browse folders on the computer running the app (not the iPad), then pick one for backups.
  let cur;
  try { cur = await api("GET", "/api/folders?path=" + encodeURIComponent(start || "")); }
  catch (e) { cur = await api("GET", "/api/folders?path="); }
  const body = h("div");
  const draw = () => {
    body.innerHTML = "";
    body.append(h("div", { class: "kicker small" }, "On this computer"),
      h("div", { class: "btn-row", style: { margin: "6px 0 12px" } }, cur.places.map(pl =>
        h("button", { class: "btn small", onclick: () => go(pl.path) }, pl.name))));
    if (!cur.path) { body.append(h("p", { class: "hint" }, "Pick a place to start.")); return; }
    body.append(h("div", { class: "mono", style: { fontSize: "13px", wordBreak: "break-all", color: "var(--champagne)" } }, cur.path),
      h("div", { style: { maxHeight: "40vh", overflow: "auto", margin: "8px 0", borderTop: "1px solid var(--line)" } },
        cur.parent ? h("div", { class: "float-item", style: { padding: "8px" }, onclick: () => go(cur.parent) }, "‹ Up") : null,
        cur.folders.length ? cur.folders.map(f => h("div", { class: "float-item", style: { padding: "8px" }, onclick: () => go(f.path) }, "📁 " + f.name))
          : h("p", { class: "hint" }, "No folders inside.")));
  };
  const go = async (path) => { try { cur = await api("GET", "/api/folders?path=" + encodeURIComponent(path)); draw(); } catch (e) { toast(e.message, "bad"); } };
  draw();
  modal("Backup copy folder", body, [{ label: "Use this folder", cls: "gold", fn: async () => {
    if (!cur.path) { toast("Pick a folder first", "bad"); return true; }
    try { await api("POST", "/api/settings", { mirror_dir: cur.path }); toast("Backups will copy here", "ok"); setTimeout(openSetup, 50); }
    catch (e) { toast(e.message, "bad"); return true; }
  } }]);
}

function whiteTest() {
  // Step every Mode 7 RGBW light on the live float through ways of making white, and tune cool whites by eye.
  const fl = curFloat();
  if (!fl || !isLive(fl)) { toast("Go live on a float with Mode 7 RGBW lights first.", "bad"); return; }
  let method = 1, k = 6500;
  const seed = { 4200: [1, 0.9, 0.55, 1], 5600: [0.8, 0.95, 0.75, 1], 6500: [0.75, 1, 0.9, 1] };
  const cal = () => ((S.project.white_cal || {}).RGBW) || {};
  let mix = (cal()[k] || seed[k] || [0.75, 1, 0.9, 1]).slice();
  const names = { 1: "All colors + color temp channel (current)", 2: "Fixture's built-in white preset", 3: "Cool white LED only",
    4: "All colors full, no correction", 5: "Cool white LED + color temp channel", 6: "Tune by eye" };
  const body = h("div");
  let tmr = null;
  const send = (redraw = true) => {
    clearTimeout(tmr);
    tmr = setTimeout(async () => { try { await api("POST", "/api/output", { action: "white_test", method, k, mix }); } catch (e) { toast(e.message, "bad"); } }, 40);
    if (redraw) draw();
  };
  const pickK = (x) => { k = x; if (method === 6) mix = (cal()[k] || seed[k] || mix).slice(); send(); };
  const draw = () => {
    body.innerHTML = "";
    body.append(
      h("div", { class: "kicker small" }, "Method"),
      h("div", { class: "seg", style: { margin: "6px 0 4px" } }, [1, 2, 3, 4, 5, 6].map(n => h("button", { class: n === method ? "on" : "", onclick: () => { method = n; if (n === 6) mix = (cal()[k] || seed[k] || mix).slice(); send(); } }, String(n)))),
      h("p", { style: { margin: "4px 0 14px", fontWeight: 600 } }, method + ": " + names[method]),
      h("div", { class: "kicker small" }, "Color temperature"),
      h("div", { class: "seg", style: { marginTop: "6px" } }, [2700, 3200, 4200, 5600, 6500].map(x => h("button", { class: x === k ? "on" : "", onclick: () => pickK(x) }, x + "K" + (cal()[x] ? " ✓" : "")))));
    if (method === 6) {
      ["Red", "Green", "Blue", "White"].forEach((name, i) => body.append(sliderCtl({
        label: name, min: 0, max: 100, step: 1, value: Math.round(mix[i] * 100), mixed: false, fmt: v => v + "%",
        track: ["linear-gradient(90deg,#000,#f33)", "linear-gradient(90deg,#000,#3f5)", "linear-gradient(90deg,#000,#48f)", "linear-gradient(90deg,#000,#fff)"][i],
        onInput: v => { mix[i] = v / 100; send(false); } })));
      body.append(h("div", { class: "btn-row", style: { marginTop: "12px" } },
        h("button", { class: "btn gold", onclick: async () => {
          try { const r = await api("POST", "/api/white_cal", { k, mix }); S.project.white_cal = r.white_cal; toast(`Saved. RGBW lights now use this at ${k}K`, "ok"); draw(); }
          catch (e) { toast(e.message, "bad"); } } }, `Save as ${k}K`),
        cal()[k] ? h("button", { class: "btn small ghost", onclick: async () => {
          const r = await api("POST", "/api/white_cal", { k, delete: true }); S.project.white_cal = r.white_cal; toast(`Back to the fixture's own ${k}K`); draw(); } }, `Remove ${k}K`) : null));
      body.append(h("p", { class: "hint" }, "Saved temperatures (✓) replace the fixture's own white at that temperature and above, blending in between. Warmer than your coolest saved one, the fixture's own white is used."));
    } else body.append(h("p", { class: "hint" }, "Every Mode 7 RGBW light on this float shows it. Methods 3 and 4 ignore the color temperature."));
  };
  send();
  modal("White test", body, [
    { label: "Reset to fixture whites", fn: async () => {
      const r = await api("POST", "/api/white_cal", { reset_all: true }); S.project.white_cal = r.white_cal;
      await api("POST", "/api/output", { action: "white_test", method: null }); toast("All whites back to the fixture's own", "ok"); } },
    { label: "Stop test", fn: async () => { await api("POST", "/api/output", { action: "white_test", method: null }); } }]);
}

function twTest() {
  // Tunable-white lights in Mode 7 aren't documented by Robe: light one channel at a time to learn what each does.
  const fl = curFloat();
  if (!fl || !isLive(fl) || !fl.fixtures.some(f => f.variant === "TW" && f.mode === 7)) {
    toast("Go live on a float with tunable white lights in Mode 7 first.", "bad"); return; }
  let method = 7, k = 3000;
  const names = { 7: "Red channel only", 8: "Green channel only", 9: "Blue channel only", 10: "White channel only",
    11: "All four + color temp channel (what the app does now)", 12: "White channel + color temp channel" };
  const body = h("div");
  const send = () => { api("POST", "/api/output", { action: "white_test", method, k }).catch(e => toast(e.message, "bad")); draw(); };
  const draw = () => {
    body.innerHTML = "";
    body.append(
      h("div", { class: "seg", style: { margin: "6px 0 4px" } }, [7, 8, 9, 10, 11, 12].map(n => h("button", { class: n === method ? "on" : "", onclick: () => { method = n; send(); } }, String(n - 6)))),
      h("p", { style: { margin: "4px 0 14px", fontWeight: 600 } }, (method - 6) + ": " + names[method]));
    if (method >= 11) body.append(h("div", { class: "kicker small" }, "Color temperature"),
      h("div", { class: "seg", style: { marginTop: "6px" } }, [2700, 3200, 4200, 5600, 6500].map(x => h("button", { class: x === k ? "on" : "", onclick: () => { k = x; send(); } }, x + "K"))));
    body.append(h("p", { class: "hint" }, "For each step, tell Claude: off, warm, cool, or mixed. For 5 and 6, does the color change between 2700K and 6500K?"));
  };
  send();
  modal("Tunable white test", body, [{ label: "Stop test", fn: async () => { await api("POST", "/api/output", { action: "white_test", method: null }); } }]);
}

function networkPicker(net, ifs) {
  const sel = h("select", { class: "f" },
    h("option", { value: "", selected: !net.pinned_ip }, "Automatic (all adapters)"),
    ifs.map(i => h("option", { value: i.ip, selected: i.ip === net.pinned_ip }, `${i.name} — ${i.ip}`)));
  const wrap = h("div", null,
    h("label", { style: { display: "block", marginBottom: "6px" } }, h("span", { class: "kicker small" }, "Network adapter")),
    sel,
    h("p", { class: "hint" }, "More than one is active on this computer. If Find can't reach the box, pick the one plugged into it."));
  sel.onchange = async () => {
    try {
      await api("POST", "/api/network/interface", { ip: sel.value || null });
      toast("Adapter changed", "ok");
    } catch (e) { toast(e.message, "bad"); }
    openSetup();
  };
  return wrap;
}

async function refreshAddr() {
  // This computer's address(es), so it's easy to type on the iPad. Box-network adapter first.
  try {
    const net = await api("GET", "/api/network");
    const ifs = net.interfaces.filter(i => !i.ip.startsWith("127.") && !i.ip.startsWith("169.254."));
    ifs.sort((a, b) => (b.ip === net.pinned_ip) - (a.ip === net.pinned_ip));
    const el = $("#addrLine");
    el.innerHTML = "";
    if (!ifs.length) { el.textContent = "No network connection"; return; }
    el.append("iPad: ", ...ifs.map((i, n) => [n ? "  ·  " : "", h("b", null, `http://${i.ip}:${net.http_port}`)]).flat());
  } catch (e) { /* server restarting; try again next tick */ }
}

// ------------------------------------------------------------------ boot
async function boot() {
  document.addEventListener("pointerdown", () => { S.pointerDown = true; }, true);
  document.addEventListener("pointerup", () => { S.pointerDown = false; }, true);
  document.addEventListener("pointercancel", () => { S.pointerDown = false; }, true);
  $("#setupBtn").onclick = openSetup;
  $("#releaseBtn").onclick = async () => {
    const wasHold = S.engine.hold, wasLive = S.engine.output;
    const r = await api("POST", "/api/output", { action: (S.engine.hold || S.engine.output) ? "release" : "resume" });
    S.engine = r.engine; renderStatus(); renderRail(); renderFloatHead();
    if (wasHold) toast("Hold cleared. Press Go live to send your look again.", "ok");
    else if (wasLive) toast("Released: lights sent to black.");
    if (!S.engine.output && !S.engine.active_float) toast("Pick a float and press Go live");
  };
  $("#blackoutBtn").onclick = async () => {
    const r = await api("POST", "/api/output", { action: "blackout", on: !S.engine.blackout });
    S.engine = r.engine; renderStatus();
  };
  try {
    await loadState();
  } catch (e) {
    $("#mainInner").append(h("div", { class: "card" }, h("h2", null, "Can't reach DMX Scene Builder"), h("p", null, "Is the DMX Scene Builder window still open on the Mac? " + e.message)));
    return;
  }
  renderStatus(); renderRail(); renderMain();
  setInterval(pollStatus, 1000);
  refreshAddr(); setInterval(refreshAddr, 15000);
}
boot();
