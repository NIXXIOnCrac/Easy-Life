/* Easy Life frontend — desktop and phone-PWA share this single-page app. */
"use strict";

/* ================================ tiny helpers ============================ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const uid = () => Math.random().toString(16).slice(2, 10) + Date.now().toString(16).slice(-4);
const apiBase = "api";
// Step count works for both full ritual objects (actions[]) and summaries (action_count).
/* "1 steps" is the kind of detail that makes a polished app read as unfinished. */
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const stepLabel = (n) => plural(n, "step", "steps");
const stepCount = (r) => (r && Array.isArray(r.actions)) ? r.actions.length : ((r && r.action_count) || 0);

/* ---- API boundary coercion ---------------------------------------------
 * Everything the API returns is stored in module state and then read by many
 * renderers (which do not re-check types). A single malformed payload — an
 * HTML body from a captive portal or proxy, a stale server shape, or a null
 * entry inside an otherwise valid array — used to throw inside a render and
 * replace the whole page with the error card. Coerce once, at the boundary
 * where the data enters state, so every consumer is safe by construction.
 */
const isPlainObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const objList = (d) => (Array.isArray(d) ? d.filter(isPlainObject) : []);

/* ================================ API client ============================= */
const Store = {
  read(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (_) { return null; } },
  write(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} },
  remove(k) { localStorage.removeItem(k); },
};

/* iOS gives a home-screen web app its OWN storage container: localStorage
 * written in Safari is invisible there. That is exactly why pairing "worked in
 * Safari but not on the home screen". A first-party cookie is what iOS copies
 * across when the icon is created, so the device token is mirrored into one and
 * read from either store. Both are JS-readable; this is not a downgrade from
 * localStorage. */
const TOKEN_KEY = "pcrituals.token";
function readCookieToken() {
  try {
    const m = document.cookie.match(/(?:^|;\s*)pcrituals_device=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  } catch (_) { return ""; }
}
function writeCookieToken(t) {
  try {
    document.cookie = t
      ? "pcrituals_device=" + encodeURIComponent(t) + "; path=/; max-age=31536000; SameSite=Lax"
      : "pcrituals_device=; path=/; max-age=0; SameSite=Lax";
  } catch (_) {}
}
let token = Store.read(TOKEN_KEY) || readCookieToken() || "";
function authHeaders(extra = {}) {
  const h = { "Content-Type": "application/json", ...extra };
  if (token) h["Authorization"] = "Bearer " + token;
  return h;
}

async function api(method, path, body, opts = {}) {
  const res = await fetch(apiBase + path, {
    method, headers: authHeaders(), body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: opts.signal,
  });
  if (res.status === 401 && !path.startsWith("/pair/") && !path.startsWith("/auth/")) {
    // Session expired (or never valid). Drop the token and, if we HAD one,
    // reload so the sign-in screen appears. Auth endpoints are excluded so a
    // failed login attempt can't sign out a valid session.
    const had = !!token;
    setToken("");
    if (had && !window.__authReloading) {
      window.__authReloading = true;
      setTimeout(() => location.reload(), 300);
    }
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}
const apiGet = (p, o) => api("GET", p, undefined, o);
const apiPost = (p, b) => api("POST", p, b);
const apiPut = (p, b) => api("PUT", p, b);
const apiDel = (p) => api("DELETE", p);

function setToken(t) { token = t; Store.write(TOKEN_KEY, t); writeCookieToken(t); }

/* ================================ toast ================================== */
let toastTimer;
function toast(msg, type = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = "toast", 2600);
}

/* ================================ modal ================================== */
let modalResolver = null;   // set by confirmModal so Escape can resolve(false)
let lastFocused = null;     // focus to restore when the dialog closes
function openModal(html) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-backdrop"><div class="modal" role="dialog" aria-modal="true">${html}</div></div>`;
  root.querySelector(".modal-backdrop").addEventListener("click", (e) => {
    if (e.target.classList.contains("modal-backdrop")) closeModal();
  });
  const first = root.querySelector("[autofocus], input, select, button");
  if (first) setTimeout(() => first.focus(), 30);
  return root.querySelector(".modal");
}
function closeModal() {
  const root = $("#modal-root");
  if (root) root.innerHTML = "";
  // A dialog that was dismissed without choosing must not leave its promise
  // hanging (the caller awaits confirmModal).
  if (modalResolver) { const r = modalResolver; modalResolver = null; r(false); }
  if (lastFocused && typeof lastFocused.focus === "function") {
    try { lastFocused.focus(); } catch (_) {}
  }
  lastFocused = null;
}

// Keyboard support for dialogs: Escape closes, Tab stays inside the modal.
document.addEventListener("keydown", (e) => {
  const modal = document.querySelector(".modal");
  if (!modal) return;
  if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
  if (e.key !== "Tab") return;
  const focusables = [...modal.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'
  )].filter((el) => el.offsetParent !== null);
  if (!focusables.length) return;
  const firstEl = focusables[0];
  const lastEl = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === firstEl || !modal.contains(active))) {
    e.preventDefault(); lastEl.focus();
  } else if (!e.shiftKey && active === lastEl) {
    e.preventDefault(); firstEl.focus();
  }
});

function confirmModal(title, message, okLabel = "Confirm", okClass = "danger") {
  return new Promise((resolve) => {
    modalResolver = resolve;
    lastFocused = document.activeElement;
    const m = openModal(`
      <h2>${esc(title)}</h2>
      <p style="color:var(--muted)">${message}</p>
      <div class="modal-actions">
        <button class="btn" data-act="no">Cancel</button>
        <button class="btn ${okClass}" data-act="yes" autofocus>${esc(okLabel)}</button>
      </div>`);
    m.querySelector('[data-act="yes"]').addEventListener("click", () => { modalResolver = null; closeModal(); resolve(true); });
    m.querySelector('[data-act="no"]').addEventListener("click", () => { modalResolver = null; closeModal(); resolve(false); });
  });
}

/* ================================ state ================================== */
const state = {
  rituals: [],
  builderDirty: false,   // unsaved edits in the Builder
  currentRun: null,
  history: [],
  pc: null,
  devices: [],
  backups: [],
  settings: null,
  pairing: null,
  deck: [],
  media: null,
  spotify: null,
  route: "dashboard",
  editingRitual: null,   // in-builder copy
};

/* ================================ router ================================= */
const routes = ["dashboard", "rituals", "builder", "deck", "obs", "remote", "activity", "integrations", "settings"];
let currentRoute = "dashboard";

function navigate(route, param) {
  if (!routes.includes(route)) route = "dashboard";
  if (!canLeaveBuilder(route)) return;
  currentRoute = route;
  state.route = route;
  // There is no #nav element (the sidebar is <aside class="sidebar">), so this
  // silently matched nothing and the active highlight never moved.
  $$("#app .nav-item").forEach((n) => n.classList.toggle("active", n.dataset.route === route));
  $$(".page").forEach((p) => p.classList.remove("active"));
  // The page element only exists once the app shell has rendered. A hashchange
  // can fire before that — a deep link, a bookmarked #/settings, or the PWA
  // restoring a URL — and dereferencing it unguarded threw and broke all
  // navigation (the error card path could not even run).
  const page = $("#page-" + route);
  if (page) page.classList.add("active");
  window.scrollTo(0, 0);
  if (route === "builder" && param) { editRitualInBuilder(param); refresh(); }
  render();
}

/* The hash can carry a query string: the pairing QR encodes
 * "#/remote?code=A2D51F". Splitting naively on "/" produced the route
 * "remote?code=A2D51F", which is not a known route, so scanning the QR dropped
 * the code entirely and dumped the phone on the Dashboard. Strip the query off
 * the path first, and expose the params to the pairing flow. */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const qi = raw.indexOf("?");
  const path = qi === -1 ? raw : raw.slice(0, qi);
  const qs = qi === -1 ? "" : raw.slice(qi + 1);
  const parts = path.split("/").filter(Boolean);
  return { route: parts[0] || "dashboard", arg: parts[1], params: new URLSearchParams(qs) };
}
function hashRoute() {
  const { route, arg } = parseHash();
  navigate(route, arg);
}
window.addEventListener("hashchange", hashRoute);

// Closing the tab or reloading is the one exit the navigate guard cannot catch.
window.addEventListener("beforeunload", (e) => {
  if (state.builderDirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});

/* ============================ navigation shell =========================== */
function renderNav() {
  $("#app").innerHTML = `
    <aside class="sidebar">
      <div class="brand"><div class="logo" aria-hidden="true"></div> Easy Life</div>
      ${navRoutes().map((r) => `<button class="nav-item" data-route="${r}">
          <span class="ico">${ICONS[r]}</span>${NAME[r]}</button>`).join("")}
      <div class="spacer"></div>
      <div class="pc-status" id="mini-pc"><span style="color:var(--muted)">Loading…</span></div>
    </aside>
    <main class="main">
      <div id="update-banner-slot"></div>
      ${routes.map((r) => `<section class="page" id="page-${r}"></section>`).join("")}
    </main>`;
  $$("#app .nav-item").forEach((n) => n.id = "nav-" + n.dataset.route);
  $$("#app .nav-item").forEach((n) => n.addEventListener("click", () => {
    const route = n.dataset.route;
    if (!canLeaveBuilder(route)) return;   // do not touch the hash when staying
    navigate(route); location.hash = "#/" + route;
  }));
}

const ICONS = { dashboard: "⌂", rituals: "▦", builder: "✎", deck: "▩", obs: "🎬", remote: "◉", activity: "≋", integrations: "⛁", settings: "⚙" };
const NAME = { dashboard: "Dashboard", rituals: "Plays", builder: "Builder", deck: "Deck", obs: "OBS", remote: "Phone Remote", activity: "Activity", integrations: "Integrations", settings: "Settings" };

/* Streamer Mode is off by default: most people never open OBS, and an always
 * empty tab is just noise to them. */
const navRoutes = () => routes.filter((r) => {
  if (r === "obs") return !!(state.settings && state.settings.streamer_mode);
  // Integrations only detects apps on Windows; on any other host the page can
  // only say "not here", so hide it rather than show a dead end.
  if (r === "integrations") return !state.pc || state.pc.platform === "windows";
  return true;
});

/* ================================ render ================================= */
async function refresh() {
  await Promise.allSettled([refreshPc(), refreshRituals(), refreshHistory(), refreshDevices(), refreshPairing(), refreshBackups(), refreshSettings(), refreshDeck(), refreshMedia(), refreshSpotify()]);
  render();
}
function render() {
  // Each step is isolated: a failure in one page must never freeze the whole
  // UI (that previously stopped all live updates — "it doesn't renew data").
  try { renderNavActive(); } catch (e) { console.error("nav:", e); }
  try { renderMiniPc(); } catch (e) { console.error("mini-pc:", e); }
  try { renderCurrentRun(); } catch (e) { console.error("runner:", e); }

  const pages = {
    dashboard: renderDashboard, rituals: renderRituals, builder: renderBuilder,
    deck: renderDeck, obs: renderOBS, remote: renderRemote, activity: renderActivity,
    integrations: renderIntegrations, settings: renderSettings,
  };
  // Make the current route's page VISIBLE, not just filled. Only navigate()
  // used to set .active, so any code path that called renderNav() then render()
  // (the Streamer Mode and tunnel toggles) left every <section class="page">
  // without .active - and .page is display:none unless active, so the whole main
  // area rendered blank while the sidebar looked fine.
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + currentRoute));

  const fn = pages[currentRoute];
  if (!fn) return;
  try {
    const result = fn();
    // `renderRemote` and `renderIntegrations` are async, so they throw *after*
    // an await — which a synchronous try/catch cannot catch. That left the page
    // blank with no error card and one unhandled rejection per poll tick.
    if (result && typeof result.catch === "function") {
      result.catch((e) => renderPageError(currentRoute, e));
    }
  } catch (e) {
    renderPageError(currentRoute, e);
  }
}

function renderPageError(route, e) {
  console.error("render failed:", route, e);
  const el = $("#page-" + route);
  // Only replace the page if it is still the one on screen and it is empty,
  // so a late rejection cannot wipe a page the user has already navigated to.
  if (!el || currentRoute !== route) return;
  el.innerHTML = `<div class="card">
    <strong>This page hit a problem.</strong>
    <p style="color:var(--muted);font-size:13px;margin-top:6px">${esc(String(e && e.message || e))}</p>
    <button class="btn sm" style="margin-top:10px" onclick="location.reload()">Reload</button>
  </div>`;
}
function renderNavActive() {
  $$("#app .nav-item").forEach((n) => n.classList.toggle("active", n.dataset.route === currentRoute));
}

async function refreshPc() {
  try {
    const d = await apiGet("/status");
    state.pc = (d && typeof d === "object") ? d : state.pc;
    // The nav is built before /status returns, so the Integrations item's
    // visibility (it only works on Windows) is decided with state.pc still
    // null. Rebuild the shell once the platform is known. Safe now: render()
    // marks the current page active, so this no longer blanks the screen.
    if (state.pc && state.pc.platform && !state.pcKnown) {
      state.pcKnown = true;
      renderNav();
      render();
    }
  } catch (_) {}
}
async function refreshRituals() {
  // Validate at the boundary: a 200 whose body is not an array — or that holds
  // a non-object entry — would otherwise put a wrong-typed value into module
  // state and make every later render throw.
  try {
    const d = await apiGet("/rituals");
    state.rituals = objList(d);
  } catch (_) {}
}
async function refreshHistory() {
  try {
    const d = await apiGet("/history?limit=50");
    // A `null` inside the array made the Activity page throw on `h.status`.
    state.history = objList(d);
  } catch (_) {}
}
async function refreshDevices() {
  try {
    const d = await apiGet("/devices");
    state.devices = objList(d);
  } catch (_) {}
}
async function refreshSettings() {
  try {
    const d = await apiGet("/settings");
    state.settings = isPlainObject(d) ? d : null;
  } catch (_) { state.settings = null; }
}
async function refreshDeck() {
  try {
    const d = await apiGet("/deck");
    state.deck = objList(d);
  } catch (_) { state.deck = []; }
}
async function refreshMedia() {
  try { const d = await apiGet("/media/now"); state.media = isPlainObject(d) ? d : null; } catch (_) { state.media = null; }
}
async function refreshSpotify() {
  try { const d = await apiGet("/spotify/status"); state.spotify = isPlainObject(d) ? d : null; } catch (_) { state.spotify = null; }
}
async function saveSpotifyConfig() {
  const id = (($("#sp-id") || {}).value || "").trim();
  const secret = (($("#sp-secret") || {}).value || "").trim();
  const redirect = (($("#sp-redirect") || {}).value || "").trim();
  if (!id) return toast("Enter your Spotify Client ID", "error");
  try {
    state.spotify = await apiPost("/spotify/config", { client_id: id, client_secret: secret, redirect_uri: redirect });
    toast("Saved. Now click Connect.", "success");
    renderSettings();
  } catch (e) { toast(e.message, "error"); }
}
async function spotifyConnect() {
  try {
    const r = await apiGet("/spotify/login");
    if (r.auth_url) window.open(r.auth_url, "_blank");
    toast("Finish signing in to Spotify, then return here.");
  } catch (e) { toast(e.message, "error"); }
}
async function spotifyDisconnect() {
  const ok = await confirmModal("Disconnect Spotify", "Disconnect your Spotify account?", "Disconnect");
  if (!ok) return;
  try { state.spotify = await apiPost("/spotify/disconnect"); await refreshSpotify(); renderSettings(); toast("Disconnected"); }
  catch (e) { toast(e.message, "error"); }
}
window.saveSpotifyConfig = saveSpotifyConfig;
window.spotifyConnect = spotifyConnect;
window.spotifyDisconnect = spotifyDisconnect;
async function saveSettings() {
  const url = (($("#upd-url") || {}).value || "").trim();
  try {
    state.settings = await apiPost("/settings", { update_url: url });
    toast("Update URL saved", "success");
    updateInfo = null; checkForUpdate();
  } catch (e) { toast(e.message, "error"); }
}
window.saveSettings = saveSettings;

async function setTunnelAutostart(on) {
  try {
    state.settings = await apiPost("/settings", { tunnel_autostart: !!on });
    toast(on ? "Will start automatically" : "Won't auto-start", "success");
  } catch (e) { toast(e.message, "error"); }
}
window.setTunnelAutostart = setTunnelAutostart;

async function toggleStreamerMode(on) {
  try {
    state.settings = await apiPost("/settings", { streamer_mode: !!on });
    toast(on ? "Streamer Mode on" : "Streamer Mode off", "success");
    renderNav(); render();
  } catch (e) {
    const cb = $("#set-streamer"); if (cb) cb.checked = !on;   // don't lie about it
    toast(e.message, "error");
  }
}
window.toggleStreamerMode = toggleStreamerMode;

async function saveOBSSettings() {
  const msg = $("#obs-set-msg");
  const body = { obs_exe: ($("#obs-exe") || {}).value || "" };
  const pass = ($("#obs-pass") || {}).value || "";
  if (pass) body.obs_password = pass;
  try {
    state.settings = await apiPost("/settings", body);
    if (msg) msg.textContent = "Saved. Checking OBS…";
    const s = await apiGet("/obs/status");
    if (msg) {
      msg.textContent = s && s.reachable
        ? "✓ Connected to OBS."
        : ("Not connected: " + ((s && s.error) || "OBS isn't running."));
    }
    render();
  } catch (e) {
    if (msg) msg.textContent = e.message;
  }
}
window.saveOBSSettings = saveOBSSettings;

async function toggleRunAtLogin(on) {
  const msg = $("#autostart-msg");
  try {
    const s = await apiPost("/settings", { run_at_login: !!on });
    state.settings = s;
    const st = (s && s.run_at_login) || {};
    if (msg) {
      msg.textContent = st.enabled
        ? "Easy Life starts automatically when you sign in, without showing a window."
        : "Off — your phone can only reach this PC while Easy Life is running.";
    }
    toast(st.enabled ? "Easy Life will start with Windows" : "Auto-start turned off", "success");
  } catch (e) {
    // Put the switch back where it was: leaving it showing "on" when the
    // registry write failed would be a lie the user acts on.
    const cb = $("#set-autostart");
    if (cb) cb.checked = !on;
    if (msg) msg.textContent = e.message;
    toast(e.message, "error");
  }
}
window.toggleRunAtLogin = toggleRunAtLogin;

async function renderTunnelBox() {
  const el = $("#tunnel-box");
  if (!el) return;
  let t = {};
  try { t = (await apiGet("/tunnel")) || {}; }
  catch (e) { el.innerHTML = `<div style="color:var(--red);font-size:13px">${esc(e.message)}</div>`; return; }

  if (t.running) {
    el.innerHTML = `
      <div style="background:rgba(52,245,165,.08);border:1px solid rgba(52,245,165,.3);border-radius:10px;padding:12px">
        <div style="font-weight:600;margin-bottom:6px">● Live</div>
        <code style="display:block;word-break:break-all;color:var(--green);font-size:12.5px">${esc(t.url)}</code>
        <div style="color:var(--muted);font-size:12px;margin-top:8px">Open this on your phone — it works anywhere.</div>
      </div>
      <button class="btn danger" style="margin-top:12px" onclick="stopTunnel()">Turn off remote access</button>`;
    return;
  }
  el.innerHTML = `
    ${t.error ? `<div style="color:var(--red);font-size:12.5px;margin-bottom:10px">${esc(t.error)}</div>` : ""}
    <button class="btn primary" onclick="startTunnel()">Turn on remote access</button>
    ${t.installed ? "" : `<p style="color:var(--muted);font-size:12.5px;margin-top:10px">
        Needs <strong>cloudflared</strong> (free). Run this once in a terminal, then try again:
        <code style="display:block;background:var(--bg-3);padding:8px;border-radius:6px;margin-top:6px;word-break:break-all">${esc(t.installed_hint || "")}</code></p>`}`;
}
async function startTunnel() {
  const el = $("#tunnel-box");
  if (el) el.innerHTML = `<div style="color:var(--muted);font-size:13px">Starting — Cloudflare is assigning an address…</div>`;
  try { await apiPost("/tunnel/start", {}); toast("Remote access is on", "success"); }
  catch (e) { toast(e.message, "error"); }
  renderTunnelBox();
}
window.startTunnel = startTunnel;
async function stopTunnel() {
  try { await apiPost("/tunnel/stop", {}); toast("Remote access is off"); }
  catch (e) { toast(e.message, "error"); }
  renderTunnelBox();
}
window.stopTunnel = stopTunnel;

async function quitApp() {
  const ok = await confirmModal("Quit Easy Life",
    "This stops the app, and your phone will lose access until you open Easy Life again.",
    "Quit", "danger");
  if (!ok) return;
  try {
    await apiPost("/quit", {});
    toast("Easy Life is closing…");
  } catch (e) {
    toast(e.message, "error");
  }
}
window.quitApp = quitApp;
async function refreshPairing() {
  try { const d = await apiGet("/pair/state"); state.pairing = isPlainObject(d) ? d : null; } catch (e) { state.pairing = null; }
}
async function refreshCurrentRun() {
  // Single-flight: the 2.5s tick and an SSE event can call this at the same
  // time, and an older response landing last made run progress jump backwards.
  if (runInFlight) return;
  runInFlight = true;
  const seq = ++runSeq;
  try {
    const r = await apiGet("/run/current");
    if (seq === runSeq) {
      // "cancelling" is still an active run — a stop request has been made but
      // the backend has not finished killing the action in flight. Keep the
      // panel up (labelled "Stopping…") until the server reports a final state;
      // only then is the run really over.
      const active = isPlainObject(r) && (r.state === "running" || r.state === "cancelling" || r.running === true);
      if (active) {
        const cr = Object.assign({}, r);
        if (cr.state !== "cancelling") cr.state = "running";
        state.currentRun = cr;
      } else {
        state.currentRun = null;
      }
    }
  } catch (_) {}
  finally { runInFlight = false; }
}

/* A stop request is a request, not an outcome. The backend acknowledges it
 * immediately but the action already in flight can keep running, so the UI must
 * say "Stopping…" until the server reports a real final state — never "stopped"
 * on the strength of the request alone. */
const isStopping = (r) => !!r && r.state === "cancelling";
const runStateLabel = (r) => !r ? "" : (isStopping(r) ? "Stopping…" : String(r.state || ""));
const runIsActive = (r) => !!r && (r.state === "running" || r.state === "cancelling");

function renderMiniPc() {
  const el = $("#mini-pc"); if (!el) return;
  if (runIsActive(state.currentRun)) {
    const r = state.currentRun;
    const stopping = isStopping(r);
    el.innerHTML = `<div class="pc-mini"><span class="dot ${stopping ? "offline" : "running"}"></span><div style="flex:1">
      <div style="font-weight:600">${esc(r.name)}</div>
      <div style="font-size:12px">${r.actions_completed}/${r.actions_total} · ${esc(stopping ? "Stopping…" : "running")}</div></div>
      ${stopping ? "" : `<button class="icon-btn" onclick="stopRun()" title="Stop" aria-label="Stop Play">■</button>`}</div>`;
  } else if (state.pc) {
    const cpu = state.pc.cpu !== null ? state.pc.cpu : 0;
    el.innerHTML = `<div class="pc-mini"><span class="dot"></span><span>PC Online</span>
      <span style="margin-left:auto;color:var(--muted);font-size:12px">CPU ${cpu}%</span></div>`;
  }
}

/* ================================ SSE =================================== */
let sseTimer = null;
let sseSource = null;
let sseBackoff = 1000;
let runInFlight = false;
let runSeq = 0;
function startSSE() {
  if (sseTimer) return;
  sseTimer = setInterval(tick, 2500);
  connectSSE();
}
function connectSSE() {
  try {
    // Never open an unauthenticated stream: an unpaired phone used to hammer
    // /events with a 401 every 3 seconds until it paired.
    if (!token) { setTimeout(() => { if (!sseSource) connectSSE(); }, 3000); return; }
    if (sseSource) { sseSource.close(); sseSource = null; }
    // EventSource can't set headers, so pass the token as a query param.
    const es = new EventSource(apiBase + "/events?token=" + encodeURIComponent(token));
    sseSource = es;
    es.onopen = () => { sseBackoff = 1000; };
    es.onmessage = (e) => {
      try { handleEvent(JSON.parse(e.data)); } catch (_) {}
    };
    es.onerror = () => {
      es.close();
      if (sseSource === es) sseSource = null;
      // Exponential backoff rather than a fixed 3s retry loop.
      setTimeout(() => { if (!sseSource) connectSSE(); }, sseBackoff);
      sseBackoff = Math.min(sseBackoff * 2, 30000);
    };
  } catch (_) {}
}
/* Is the user mid-input? The 2.5s poll must never re-render a screen the user
 * is typing into: replacing the DOM drops focus, closes the on-screen keyboard
 * and wipes whatever was typed. That is what made the pairing code impossible
 * to enter on a phone — the field reset every 2.5 seconds. */
function isUserEditing() {
  try {
    const ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA" ||
               ae.tagName === "SELECT" || ae.isContentEditable)) return true;
    const mr = document.getElementById("modal-root");
    if (mr && mr.children.length) return true;
    if (qrScannerActive) return true;
  } catch (_) {}
  return false;
}
async function tick() {
  await refreshCurrentRun();
  // Refresh the OBS status on its own, deliberately NOT behind isUserEditing():
  // clicking the Streamer Mode toggle leaves focus on a control, which made the
  // early return below skip the refresh and freeze the status. You open this tab
  // to answer "is my stream still live?", and a stale answer is worse than none.
  if (currentRoute === "obs" && state.settings && state.settings.streamer_mode) {
    try { renderOBSTab(); } catch (e) { console.error("obs-tab:", e); }
  }
  if (isUserEditing()) return;
  if (currentRoute === "dashboard" || currentRoute === "remote") render();
}
function handleEvent(ev) {
  if (ev.channel === "ritual") {
    if (ev.type === "started" || ev.type === "step_start" || ev.type === "step_done" ||
        ev.type === "step_error" || ev.type === "finished" || ev.type === "cancelling") {
      refreshCurrentRun().then(() => {
        if (currentRoute === "dashboard" || currentRoute === "remote" || currentRoute === "rituals") render();
        if (ev.type === "finished") { refreshRituals(); refreshHistory(); }
      });
    }
  } else if (ev.channel === "system" && ev.type === "history_updated") {
    refreshHistory();
  }
}

/* ============================ current run panel ========================== */
function renderCurrentRun() {
  const r = state.currentRun;
  // Only show the running panel on dashboard/remote.
  const host = $("#page-dashboard");
  if (!runIsActive(r) || !host) return;
  const stopping = isStopping(r);
  const pct = r.actions_total > 0 ? Math.round((r.actions_completed / r.actions_total) * 100) : 0;
  const steps = (r.steps || []).map((s, i) => `
    <div class="step-line">
      <span class="step-state ${esc(s.status)}">${s.status === "success" ? "✓" : s.status === "error" ? "✕" : s.status === "running" ? "●" : "○"}</span>
      <span class="step-name">${esc(s.name)}${s.detail ? `<div class="detail">${esc(s.detail)}</div>` : ""}</span>
    </div>`).join("");
  const panel = $(`#dashboard-runner`);
  if (panel) {
    panel.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div><strong>${esc(r.name)}</strong> · ${r.actions_completed}/${r.actions_total} · ${esc(runStateLabel(r))}</div>
        <button class="btn danger sm" onclick="stopRun()" ${stopping ? "disabled" : ""}>${stopping ? "Stopping…" : "■ Stop"}</button>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
      <div style="margin-top:12px">${steps}</div>`;
  }
}

async function runRitual(id, triggered = "local") {
  // A double-click (or a phone tap + the poll tick) must not start the same
  // play twice. The server only ACKNOWLEDGES the request, so the client has
  // to hold the line until the run is actually reported.
  if (state.currentRun && state.currentRun.running) {
    toast("A play is already running", "error");
    return;
  }
  // A Play that powers the PC off is one tap away from a locked machine. Ask
  // before running it, whether from the desktop or the phone.
  const r = (state.rituals || []).find((x) => x.id === id);
  const power = r && (r.actions || []).find((a) => a.type === "power");
  if (power) {
    const what = (power.params && power.params.action) || power.target || "power";
    if (!window.confirm(`This Play will ${what} your PC. Run it anyway?`)) return;
  }
  try {
    await apiPost(`/rituals/${id}/run`, { triggered_by: triggered });
    toast("Play started", "success");
    refreshCurrentRun().then(() => render());
  } catch (e) { toast(e.message, "error"); }
}
async function stopRun() {
  const r = state.currentRun;
  if (!r) return;
  try {
    const res = await apiPost(`/rituals/${r.ritual_id}/stop`);
    // The server only ACKNOWLEDGES the request — a shell command already
    // running keeps running until the backend kills it and reports the final
    // state. Show "Stopping…" (and an inert button) rather than anything that
    // reads as "stopped"; the next /run/current poll decides the real outcome.
    state.currentRun = Object.assign({}, r, {
      state: "cancelling",
      stopping: !!(res && res.stopping),
    });
    toast("Stopping…");
    render();
    refreshCurrentRun().then(() => render());
  } catch (e) { toast(e.message, "error"); }
}

/* make these callable from inline onclick */
window.runRitual = runRitual;
window.stopRun = stopRun;

/* ================================ Dashboard ============================== */
function renderDashboard() {
  const el = $("#page-dashboard");
  const r = state.currentRun;
  el.innerHTML = `
    <h1 class="page-title">Dashboard</h1>
    <p class="page-sub">Your PC's current state and any running Play.</p>
    ${runIsActive(r) ? `<div class="runner-panel" id="dashboard-runner"></div>` : ""}
    <div id="phone-quick"></div>
    <div class="grid grid-3" style="margin-bottom:24px">
      <div class="card"><div style="color:var(--muted);font-size:13px">PC Status</div>
        <div style="font-size:20px;font-weight:700;display:flex;align-items:center;gap:10px">
          <span class="dot"></span>${state.pc && state.pc.online ? "Online" : "Checking…"}</div>
        <div style="color:var(--muted);font-size:13px;margin-top:6px">CPU ${state.pc ? state.pc.cpu : "—"}% · RAM ${state.pc && state.pc.memory ? state.pc.memory.percent : "—"}%</div>
      </div>
      <div class="card"><div style="color:var(--muted);font-size:13px">Plays</div>
        <div style="font-size:20px;font-weight:700">${state.rituals.length}</div>
        <button class="btn sm" style="margin-top:10px" onclick="navigate('rituals')">Manage →</button></div>
      <div class="card"><div style="color:var(--muted);font-size:13px">Total Runs</div>
        <div style="font-size:20px;font-weight:700">${state.history.length}</div>
        <button class="btn sm" style="margin-top:10px" onclick="navigate('activity')">View →</button></div>
    </div>
    <h3 style="margin-bottom:12px">Quick Launch</h3>
    ${quickRituals()}`;
  // fill runner panel after injection
  if (runIsActive(r)) setTimeout(() => renderCurrentRun(), 0);
  renderPhoneQuick();
}
function renderPhoneQuick() {
  const el = $("#phone-quick"); if (!el) return;
  const isLocal = location.hostname === "localhost" || location.hostname === "127.0.0.1" || location.hostname === "[::1]" || location.hostname === "";
  // Only suggest phone pairing when viewing on the PC itself (loopback).
  if (!isLocal || token) { el.innerHTML = ""; return; }
  el.innerHTML = `
    <div class="card" style="margin-bottom:24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-color:var(--border-strong)">
      <div style="font-size:26px">📱</div>
      <div style="flex:1;min-width:220px">
        <div style="font-weight:700;font-size:15px">Open Easy Life on your phone</div>
        <div style="color:var(--muted);font-size:13px;margin-top:2px">Scan a QR to pair your iPhone and control the PC from anywhere in the house.</div>
      </div>
      <button class="btn primary" onclick="navigate('remote');setTimeout(startPairing,250)">📱 Generate Phone Code</button>
    </div>`;
}
function quickRituals() {
  if (!state.rituals.length) return `<div class="empty"><div class="big">▤</div>No plays yet.<br><button class="btn primary" style="margin-top:12px" onclick="newRitual()">Create your first play</button></div>`;
  return `<div class="grid grid-3">${state.rituals.slice(0, 6).map((r) => `
    <div class="ritual-card">
      <div class="top"><h3>${esc(r.name)}</h3><span class="stat-pill">${stepLabel(stepCount(r))}</span></div>
      <button class="btn green" style="margin-top:6px" onclick="runRitual('${r.id}')">▶ Run</button>
    </div>`).join("")}</div>`;
}

/* ================================ Rituals ================================ */
function renderRituals() {
  const el = $("#page-rituals");
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <div><h1 class="page-title">Plays</h1><p class="page-sub">Your automation sequences.</p></div>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="importModal()">Import</button>
        <button class="btn" onclick="exportAll()">Export</button>
        <button class="btn primary" onclick="newRitual()">+ New Play</button>
      </div>
    </div>
    ${state.rituals.length === 0 ? emptyRituals() : `
    <div class="grid grid-3" id="ritual-grid">
      ${state.rituals.map((r, i) => ritualCard(r, i)).join("")}
    </div>`}`;
  if (state.rituals.length) attachDnD();
}
function emptyRituals() {
  return `<div class="placeholder"><div class="big">▤</div><div style="margin-bottom:14px">You haven't created any plays yet.</div>
    <button class="btn primary" onclick="newRitual()">+ Create Play</button></div>`;
}
function ritualCard(r, i) {
  const cr = state.currentRun;
  const mine = !!(cr && cr.ritual_id === r.id);
  const running = mine && !isStopping(cr);
  const runLabel = mine ? (isStopping(cr) ? "Stopping…" : "Running") : "";
  // The most recent run for this Play (history is newest-first). A failed run
  // used to be invisible on the card - the Play looked healthy - so surface it
  // until the user opens the Play.
  const lastRun = (state.history || []).find((h) => h.ritual_id === r.id);
  const lastRunFailed = lastRun && lastRun.status === "failed";
  return `
  <div class="ritual-card" draggable="true" data-id="${r.id}">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="run-dot" title="Drag to reorder">⠿</span>
      <div style="flex:1"><h3>${esc(r.name)}</h3></div>
      <span class="dot ${running ? "running" : "idle"}" title="${runLabel || "Not running"}"></span>
    </div>
    ${r.description ? `<div class="desc">${esc(r.description)}</div>` : ""}
    ${lastRunFailed ? `<div class="desc" style="color:var(--red)">Last run failed${lastRun.error ? `: ${esc(lastRun.error)}` : ""}</div>` : ""}
    <div class="meta"><span class="stat-pill">${stepLabel(stepCount(r))}</span>
      ${(r.tags || []).map((t) => `<span class="stat-pill">#${esc(t)}</span>`).join("")}</div>
    <div class="actions-row">
      <button class="btn green sm" onclick="runRitual('${r.id}')">▶ Run</button>
      <button class="btn sm" onclick="editRitual('${r.id}')">Edit</button>
      <button class="icon-btn" title="Duplicate" aria-label="Duplicate ritual" onclick="dupRitual('${r.id}')">⧉</button>
      <button class="icon-btn" title="Export" aria-label="Export ritual" onclick="exportOne('${r.id}')">⇩</button>
      <button class="icon-btn" title="Delete" aria-label="Delete ritual" style="color:var(--red)" onclick="delRitual('${r.id}')">🗑</button>
    </div>
  </div>`;
}
function attachDnD() {
  const grid = $("#ritual-grid"); if (!grid) return;
  let dragging = null;
  $$(".ritual-card", grid).forEach((card) => {
    card.addEventListener("dragstart", (e) => { dragging = card.dataset.id; card.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
    card.addEventListener("dragend", () => { card.classList.remove("dragging"); $$(".ritual-card", grid).forEach((c) => c.classList.remove("drag-over")); });
    card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drag-over"); });
    card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!dragging || dragging === card.dataset.id) return;
      const from = state.rituals.findIndex((r) => r.id === dragging);
      const to = state.rituals.findIndex((r) => r.id === card.dataset.id);
      const [moved] = state.rituals.splice(from, 1);
      state.rituals.splice(to, 0, moved);
      persistReorder();
    });
  });
}
async function persistReorder() {
  render();
  try { await apiPost("/rituals/reorder", { ids: state.rituals.map((r) => r.id) }); } catch (e) { toast(e.message, "error"); }
}

async function newRitual() {
  const m = openModal(`
    <h2>New Play</h2>
    <label class="field">Name<input id="n-name" type="text" autofocus placeholder="e.g. Gaming"></label>
    <label class="field" style="margin-top:12px">Description<textarea id="n-desc" rows="2" placeholder="Optional…"></textarea></label>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn primary" id="n-save">Create</button>
    </div>`);
  m.querySelector("#n-save").addEventListener("click", async () => {
    const name = m.querySelector("#n-name").value.trim();
    if (!name) return toast("Name required", "error");
    try {
      const desc = m.querySelector("#n-desc").value.trim();
      const r = await apiPost("/rituals", { name, description: desc, actions: [], stop_on_error: true, tags: [] });
      closeModal(); await refreshRituals();
      editRitual(r.id); // open builder
    } catch (e) { toast(e.message, "error"); }
  });
}

async function editRitual(id) {
  const r = state.rituals.find((x) => x.id === id);
  if (!r) return;
  location.hash = "#/builder/" + id;
  navigate("builder", id);
}
function editRitualInBuilder(id) {
  const r = state.rituals.find((x) => x.id === id);
  if (r) state.editingRitual = JSON.parse(JSON.stringify(r));
  // Opening a Play starts clean; the previous Play's edits were either saved
  // or deliberately abandoned through the navigate guard.
  state.builderDirty = false;
}

function delRitual(id) {
  const r = state.rituals.find((x) => x.id === id);
  confirmModal("Delete Play", `Delete <strong>${esc(r ? r.name : "this ritual")}</strong>? This cannot be undone.`, "Delete").then(async (ok) => {
    if (!ok) return;
    try { await apiDel(`/rituals/${id}`); toast("Deleted", "success"); await refreshRituals(); render(); }
    catch (e) { toast(e.message, "error"); }
  });
}
async function dupRitual(id) {
  try { const r = await apiPost(`/rituals/${id}/duplicate`); toast("Duplicated", "success"); await refreshRituals(); render(); }
  catch (e) { toast(e.message, "error"); }
}
async function exportAll() {
  try { const data = await apiGet("/export"); download("rituals.json", data); toast("Exported", "success"); }
  catch (e) { toast(e.message, "error"); }
}
async function exportOne(id) {
  const r = state.rituals.find((x) => x.id === id);
  if (!r) return;
  download(`${r.name}.json`, JSON.stringify({ version: 1, rituals: [r] }, null, 2));
}

async function importModal() {
  const m = openModal(`
    <h2>Import Plays</h2>
    <p style="color:var(--muted)">Paste a valid JSON export.</p>
    <textarea id="imp-box" rows="8" style="margin-top:10px;font-family:monospace;font-size:12px"></textarea>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn primary" id="imp-do">Import</button>
    </div>`);
  m.querySelector("#imp-do").addEventListener("click", async () => {
    const text = m.querySelector("#imp-box").value.trim();
    if (!text) return toast("Nothing to import", "error");
    let payload;
    try { payload = JSON.parse(text); } catch (_) { return toast("Invalid JSON", "error"); }
    try {
      const res = await apiPost("/import", payload);
      closeModal();
      toast(`Imported ${res.imported} ritual(s)`, "success");
      if (res.errors && res.errors.length) toast(res.errors[0], "error");
      await refreshRituals(); render();
    } catch (e) { toast(e.message, "error"); }
  });
}
function download(name, content) {
  const blob = new Blob([content], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ================================ Builder ================================ */
const ACTION_TYPES = [
  { t: "app", icon: "▣", label: "Launch App", fields: ["target", "args"] },
  { t: "game", icon: "🕹", label: "Launch Game", fields: ["target"] },
  { t: "website", icon: "🌐", label: "Open Website", fields: ["target"] },
  { t: "file", icon: "📁", label: "Open File/Folder", fields: ["target"] },
  { t: "command", icon: ">_", label: "Run Command", fields: ["target"] },
  { t: "delay", icon: "⏱", label: "Delay", fields: ["seconds"] },
  { t: "close", icon: "⏹", label: "Close App", fields: ["target"] },
  { t: "power", icon: "⚡", label: "Power Control", fields: ["power"] },
  { t: "obs_scene", icon: "🎬", label: "OBS: Switch Scene", fields: ["target"] },
  { t: "obs_stream_start", icon: "🔴", label: "OBS: Start Streaming", fields: [] },
  { t: "obs_stream_stop", icon: "⏹", label: "OBS: Stop Streaming", fields: [] },
];
const TYPE_META = Object.fromEntries(ACTION_TYPES.map((a) => [a.t, a]));

async function renderOBS() {
  const el = $("#page-obs");
  if (!el) return;
  const on = !!(state.settings && state.settings.streamer_mode);

  if (!on) {
    state.obsShellBuilt = false;
    el.innerHTML = `
      <h1 class="page-title">OBS</h1>
      <p class="page-sub">Control your stream from inside Easy Life.</p>
      <div class="card" style="max-width:520px">
        <h3 style="margin-bottom:6px">Streamer Mode is off</h3>
        <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
          Turn it on to get this tab: live status, one-tap stream controls and your
          OBS scenes. Works from your phone too, including when you're out of the house.
        </p>
        <button class="btn primary" onclick="enableStreamerMode()">Turn on Streamer Mode</button>
      </div>`;
    return;
  }

  // Build the shell once and let the status card refresh on its own, so the
  // per-tick refresh only costs one status check instead of re-creating the
  // whole page. (Measured: the tick does NOT rebuild this page on its own —
  // tick() refreshes the status explicitly — so this bounds the work rather
  // than fixing a hot loop.)
  if (!state.obsShellBuilt) {
    el.innerHTML = `
      <h1 class="page-title">OBS</h1>
      <p class="page-sub">Your stream, without touching OBS.</p>
      <div id="obs-tab-status"></div>
      <h3 style="margin:22px 0 12px">Scenes</h3>
      <div id="obs-tab-scenes" class="grid grid-2"></div>`;
    state.obsShellBuilt = true;
  }
  renderOBSTab();
}
window.renderOBS = renderOBS;

async function renderOBSTab() {
  const statusHost = $("#obs-tab-status");
  const scenesHost = $("#obs-tab-scenes");
  if (!statusHost) return;

  let s = {};
  try { s = (await apiGet("/obs/status")) || {}; } catch (e) { s = { error: e.message }; }

  const live = !!s.streaming;
  statusHost.innerHTML = `
    <div class="card" style="${live ? "border-color:rgba(52,245,165,.4)" : ""}">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="width:10px;height:10px;border-radius:50%;flex:none;background:${live ? "var(--green)" : (s.reachable ? "var(--amber)" : "var(--red)")}"></span>
        <strong>${live ? (s.reconnecting ? "Reconnecting…" : "Live") : (s.reachable ? "OBS is open, not streaming" : "OBS isn't responding")}</strong>
        ${s.timecode ? `<span style="color:var(--muted);font-size:13px">${esc(s.timecode)}</span>` : ""}
      </div>
      ${s.error ? `<div style="color:var(--muted);font-size:12.5px;margin-top:8px">${esc(s.error)}</div>` : ""}
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
        <button class="btn primary" onclick="rescueStream()">${live ? "Restart the stream" : "Fix my stream"}</button>
        ${live ? `<button class="btn" onclick="stopStream()">Stop streaming</button>` : ""}
      </div>
      <div id="obs-msg" style="margin-top:10px;font-size:12.5px;color:var(--muted)"></div>
    </div>`;

  // Scenes change rarely; cache them and only re-ask OBS when it has just
  // become reachable (so opening OBS later still fills the list in).
  // An in-flight guard, because entering the tab renders twice and both
  // renders missed the empty cache — two identical requests to OBS.
  if (!state.obsTabScenes || (!state.obsTabScenes.length && s.reachable)) {
    if (!state.obsScenesLoading) {
      state.obsScenesLoading = true;
      try { state.obsTabScenes = ((await apiGet("/obs/scenes")) || {}).scenes || []; }
      catch (_) { state.obsTabScenes = state.obsTabScenes || []; }
      finally { state.obsScenesLoading = false; }
    }
  }
  const scenes = state.obsTabScenes || [];
  if (!scenes.length) {
    scenesHost.innerHTML = `<div class="empty"><span style="color:var(--muted)">No scenes — OBS isn't running, or its WebSocket is off.</span></div>`;
    return;
  }
  scenesHost.innerHTML = scenes.map((name) => `
    <div class="ritual-card">
      <div class="top"><h3>${esc(name)}</h3></div>
      <div class="actions-row"><button class="btn" data-scene="${esc(name)}">Switch to this</button></div>
    </div>`).join("");
  // Delegated listener. An inline onclick was an XSS hole: esc() turns ' into
  // &#39;, but the HTML parser decodes that back to ' when it parses the
  // attribute, so a scene name like x');alert(1)// broke out of the string.
  scenesHost.onclick = (e) => {
    const btn = e.target.closest("[data-scene]");
    if (btn) switchScene(btn.getAttribute("data-scene"));
  };
}
window.renderOBSTab = renderOBSTab;

async function switchScene(name) {
  try { await apiPost("/obs/scene", { scene: name }); toast(`Switched to ${name}`, "success"); }
  catch (e) { toast(e.message, "error"); }
}
window.switchScene = switchScene;

function urlBase64ToUint8Array(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function savePushSub(sub) {
  const json = sub.toJSON();
  await apiPost("/notify/subscribe", {
    endpoint: json.endpoint,
    p256dh: json.keys.p256dh,
    auth: json.keys.auth,
    user_agent: navigator.userAgent || "",
  });
}

async function initPush() {
  // Web Push needs a secure context (HTTPS or localhost) and a service worker.
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  if (!window.isSecureContext) return;
  // Only subscribe once the user opts in from Settings — push permission is a
  // real ask and must not happen silently on first load.
  if (!state.settings || !state.settings.notify_enabled) return;
  try {
    const reg = await navigator.serviceWorker.register("/push-sw.js");
    await navigator.serviceWorker.ready;
    const existing = await reg.pushManager.getSubscription();
    if (existing) { await savePushSub(existing); return; }
    const { public_key } = await apiGet("/notify/vapid");
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
    await savePushSub(sub);
  } catch (e) { console.error("push init:", e); }
}

async function toggleNotify(on) {
  try {
    await apiPost("/settings", { notify_enabled: !!on });
    // Re-fetch the authoritative settings rather than trusting the POST
    // response to be complete. A backend that drops a field from its response
    // would otherwise leave this toggle flipped back on the next render (the
    // flip-back bug: it looked auto-unchecked even though it was really on).
    await refreshSettings();
    if (on) {
      await initPush();
      toast("Notifications on", "success");
    } else {
      toast("Notifications off", "success");
    }
    render();
  } catch (e) {
    const cb = $("#set-notify");
    if (cb) cb.checked = !!(state.settings && state.settings.notify_enabled);
    toast(e.message, "error");
  }
}
window.toggleNotify = toggleNotify;

async function testNotify() {
  const msg = $("#notify-msg");
  if (msg) msg.textContent = "Sending…";
  try {
    const r = await apiPost("/notify/test");
    if (msg) msg.textContent = r.sent ? `Sent to ${r.sent} device(s). Check your phone.` : "No devices subscribed yet — turn on notifications on your phone first.";
  } catch (e) { if (msg) msg.textContent = e.message; }
}
window.testNotify = testNotify;

async function enableStreamerMode() {
  try {
    state.settings = await apiPost("/settings", { streamer_mode: true });
    toast("Streamer Mode on", "success");
    renderNav(); render();
  } catch (e) { toast(e.message, "error"); }
}
window.enableStreamerMode = enableStreamerMode;

function renderBuilder() {
  const el = $("#page-builder");
  const r = state.editingRitual;
  if (!r) { el.innerHTML = `<div class="empty"><div class="big">✎</div>Select a ritual to edit.</div>`; return; }
  const stopping = !!state.currentRun && state.currentRun.ritual_id === r.id && isStopping(state.currentRun);
  const running = !!state.currentRun && state.currentRun.ritual_id === r.id && runIsActive(state.currentRun);
  // Scene names for the OBS step's suggestions. Best-effort: OBS being closed
  // is normal, and the field still accepts typing.
  if (!state.obsScenes) {
    state.obsScenes = [];
    apiGet("/obs/scenes")
      .then((d) => { state.obsScenes = (d && d.scenes) || []; })
      .catch(() => { /* OBS not running — leave the list empty */ });
  }
  el.innerHTML = `
    <div class="builder-head">
      <div style="flex:1;min-width:220px">
        <input type="text" id="b-name" value="${esc(r.name)}" style="font-size:20px;font-weight:700;background:transparent;border:none;border-bottom:1px solid var(--border)" placeholder="Play name">
        <input type="text" id="b-desc" value="${esc(r.description)}" style="background:transparent;border:none;color:var(--muted);margin-top:4px" placeholder="Description (optional)">
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px">Continue on error
          <input type="checkbox" class="toggle" id="b-continue" ${r.stop_on_error ? "" : "checked"}></label>
        ${stopping
          ? `<button class="btn danger" disabled>Stopping…</button>`
          : running
            ? `<button class="btn danger" onclick="stopRun()">■ Stop</button>`
            : `<button class="btn green" onclick="saveAndRun('${r.id}')">▶ Run</button>`}
        ${state.builderDirty ? `<span id="b-dirty" title="You have changes that are not saved yet"
              style="font-size:12px;color:var(--amber);white-space:nowrap">● Unsaved</span>` : ""}
        <button class="btn primary" onclick="saveBuilder()">Save</button>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <h3 style="margin-bottom:4px">Describe it</h3>
      <p style="color:var(--muted);font-size:13px;margin-bottom:10px">
        Type what you want this Play to do and it builds the steps for you.
        You can still edit everything afterwards.
      </p>
      <textarea id="nl-box" rows="2"
        style="width:100%;font-family:inherit;font-size:14px;padding:10px;border-radius:10px;background:var(--bg-3);border:1px solid var(--border);color:var(--text);resize:vertical"
        placeholder="open obs then switch to MAIN 1 scene then start stream, name it Stream Rescue"></textarea>
      <div style="display:flex;gap:10px;margin-top:10px;align-items:center;flex-wrap:wrap">
        <button class="btn primary" onclick="buildFromText()">Build it</button>
        <span id="nl-msg" style="color:var(--muted);font-size:12.5px"></span>
      </div>
      <div id="nl-result" style="margin-top:10px"></div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:16px 0">
      ${ACTION_TYPES.map((a) => `<button class="btn sm" onclick="addAction('${a.t}')">+ ${a.label}</button>`).join("")}
    </div>
    <div id="action-list">${actionRows(r)}</div>
    ${r.actions.length === 0 ? `<div class="placeholder">Add actions above to build your Play.</div>` : ""}`;
  const cont = $("#b-continue"); if (cont) cont.addEventListener("change", () => { state.editingRitual.stop_on_error = !cont.checked; markBuilderDirty(); });
  ["#b-name", "#b-desc"].forEach((sel) => {
    const f = $(sel); if (f) f.addEventListener("input", markBuilderDirty);
  });
  attachActionDnD();
}
function actionRows(r) {
  return r.actions.map((a, i) => {
    const meta = TYPE_META[a.type] || { icon: "?", label: a.type };
    const controls = actionControls(a);
    return `
    <div class="action-row ${a.enabled ? "" : "disabled"}" data-idx="${i}" draggable="true">
      <span class="drag" title="Drag to reorder">⠿</span>
      <div class="body">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="type-tag">${meta.icon} ${meta.label}</span>
          <span style="color:var(--muted);font-size:12px">#${i + 1}</span>
        </div>
        <div class="conf">${controls}</div>
      </div>
      <div class="action-tools">
        <label title="Enabled" class="action-toggle"><input type="checkbox" class="toggle" data-field="enabled" ${a.enabled ? "checked" : ""}></label>
        <button class="icon-btn lg" title="Duplicate action" aria-label="Duplicate action" onclick="dupAction(${i})">⧉</button>
        <button class="icon-btn lg danger" title="Delete action" aria-label="Delete action" onclick="remAction(${i})">🗑</button>
      </div>
    </div>`;
  });
}
async function buildFromText() {
  const box = $("#nl-box");
  const text = box ? box.value.trim() : "";
  if (!text) { const m = $("#nl-msg"); if (m) m.textContent = "Describe what the Play should do first."; return; }
  const msg = $("#nl-msg");
  if (msg) msg.textContent = "Building…";
  try {
    const d = await apiPost("/plays/draft", { text });
    const r = state.editingRitual;
    if (!r) throw new Error("Open a Play first, then describe it.");
    // Snapshot before touching anything, so replacing is reversible.
    const before = { name: r.name, actions: JSON.parse(JSON.stringify(r.actions || [])) };
    if (d.name) r.name = d.name;
    const built = (d.steps || []).filter(Boolean);
    if (built.length) {
      // Replace rather than append: the description IS the Play, and appending
      // would double the steps on a second press. "Add to the end instead"
      // below covers the other intent.
      r.actions = JSON.parse(JSON.stringify(built));
    }
    state.nlUndo = {
      before,
      after: JSON.parse(JSON.stringify(r.actions || [])),
      changedName: !!(d.name && d.name !== before.name),
    };
    if (msg) msg.textContent = "";
    render();
    const lines = [];
    if (d.steps && d.steps.length) {
      lines.push(`<div style="font-size:12.5px;color:var(--green)">Built ${d.steps.length} step(s):</div>`);
      lines.push(...(d.understood || []).map((u) => `<div style="font-size:12.5px;color:var(--muted);margin-left:8px">• ${esc(u)}</div>`));
    }
    // Never quietly drop a request: say what could not be understood.
    if (d.unmatched && d.unmatched.length) {
      // Quote the FRAGMENT that failed, not the whole sentence, and offer
      // clickable examples. Echoing everything read as "the AI didn't get me"
      // and sent people straight to the manual buttons.
      const examples = [
        "open discord then open spotify",
        "start my stream",
        "wait 10 seconds then lock my pc",
      ];
      lines.push(`<div style="margin-top:10px;font-size:12.5px;color:var(--amber)">
        I didn't recognise: ${d.unmatched.map((u) => `<strong>&ldquo;${esc(u)}&rdquo;</strong>`).join(" ")}
        <div style="color:var(--muted);margin-top:6px;margin-bottom:5px">Try a phrase like:</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${examples.map((ex) => `<button class="btn sm" data-nl-example="${esc(ex)}">${esc(ex)}</button>`).join("")}
        </div></div>`);
    }
    lines.push(...(d.notes || []).map((n) => `<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">ⓘ ${esc(n)}</div>`));
    // Replacing someone's steps with no way back is a trap. Offer both.
    const u = state.nlUndo;
    const hadSteps = u && (u.before.actions.length > 0 || u.changedName);
    if (hadSteps && built.length) {
      lines.push(`<div style="margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12.5px;color:var(--muted)">Replaced ${u.before.actions.length} step(s).</span>
        <button class="btn" onclick="undoDescribe()">Undo</button>
        <button class="btn" onclick="addDescribeInstead()">Add to the end instead</button>
      </div>`);
    }
    const host = $("#nl-result");
    if (host) {
      host.innerHTML = lines.join("");
      // Delegated, like the OBS scene buttons: never inline onclick with
      // interpolated text.
      host.onclick = (e) => {
        const btn = e.target.closest("[data-nl-example]");
        if (btn) nlExample(btn);
      };
    }
  } catch (e) {
    if (msg) msg.textContent = e.message;
  }
}
window.buildFromText = buildFromText;

function nlExample(btn) {
  const box = $("#nl-box");
  if (!box) return;
  box.value = btn.getAttribute("data-nl-example") || btn.textContent.trim();
  box.focus();
}
window.nlExample = nlExample;

function undoDescribe() {
  const u = state.nlUndo;
  const r = state.editingRitual;
  if (!u || !r) return;
  r.name = u.before.name;
  r.actions = JSON.parse(JSON.stringify(u.before.actions));
  state.nlUndo = null;
  render();
  toast("Undone", "success");
}
window.undoDescribe = undoDescribe;

function addDescribeInstead() {
  const u = state.nlUndo;
  const r = state.editingRitual;
  if (!u || !r) return;
  r.actions = u.before.actions.concat(JSON.parse(JSON.stringify(u.after)));
  state.nlUndo = null;
  render();
  toast("Added to the end", "success");
}
window.addDescribeInstead = addDescribeInstead;

function actionControls(a) {
  switch (a.type) {
    case "app": case "game": case "close": case "file":
      const ph = { app: "App path or name", game: "steam://rungameid/570 or exe path", close: "Process name (e.g. discord)", file: "Path to file or folder" }[a.type];
      return `<div class="row">
        <label class="field" style="flex:2">Path<input type="text" value="${esc(a.target)}" data-field="target" placeholder="${ph}"></label>
        ${a.type === "app" ? `<label class="field">Args<input type="text" value="${esc(a.args.join(" "))}" data-field="args" placeholder="--flag"></label>` : ""}
        <label class="field">Name<input type="text" value="${esc(a.name)}" data-field="name" placeholder="Label"></label>
        <label class="field">Timeout (s)<input type="number" value="${a.timeout ?? ""}" data-field="timeout" placeholder="default"></label>
      </div>`;
    case "website":
      return `<div class="row">
        <label class="field" style="flex:2">URL<input type="url" value="${esc(a.target)}" data-field="target" placeholder="https://example.com"></label>
        <label class="field">Name<input type="text" value="${esc(a.name)}" data-field="name"></label>
        <label class="field">Timeout (s)<input type="number" value="${a.timeout ?? ""}" data-field="timeout"></label>
      </div>`;
    case "command":
      return `<div class="row">
        <label class="field" style="flex:2">Command<input type="text" value="${esc(a.target)}" data-field="target" placeholder="e.g. wallpaper-switcher.exe --day"></label>
        <label class="field">Timeout (s)<input type="number" value="${a.timeout ?? 30}" data-field="timeout" placeholder="30"></label>
        <label class="field">Retries<input type="number" value="${a.params.retries ?? 0}" data-field="retries" placeholder="0"></label>
      </div>`;
    case "obs_scene":
      // Offer the real scene names when OBS is reachable, so nobody has to
      // guess at "MAIN 1" vs "Main 1".
      return `<div class="row">
        <label class="field" style="flex:2">Scene name<input type="text" list="obs-scene-names" value="${esc(a.target)}" data-field="target" placeholder="e.g. MAIN 1"></label>
        <label class="field">Name<input type="text" value="${esc(a.name)}" data-field="name"></label>
        <datalist id="obs-scene-names">${(state.obsScenes || []).map((s) => `<option value="${esc(s)}"></option>`).join("")}</datalist>
      </div>`;
    case "obs_stream_start":
    case "obs_stream_stop":
      return `<label class="field">Name<input type="text" value="${esc(a.name)}" data-field="name"></label>`;
    case "delay":
      return `<div class="row">
        <label class="field">Seconds<input type="number" step="0.1" value="${a.params.seconds ?? 1}" data-field="seconds"></label>
        <label class="field">Name<input type="text" value="${esc(a.name)}" data-field="name"></label>
      </div>`;
    case "power":
      return `<div class="row">
        <label class="field">Action
          <select data-field="power">
            ${["lock", "sleep", "restart", "shutdown"].map((p) => `<option value="${p}" ${(a.params.action || "lock") === p ? "selected" : ""}>${p.toUpperCase()}</option>`).join("")}
          </select></label>
        <label class="field">Name<input type="text" value="${esc(a.name)}" data-field="name"></label>
      </div>`;
    default: return "";
  }
}
function attachActionDnD() {
  const list = $("#action-list"); if (!list) return;
  let dragIdx = null;
  $$(".action-row", list).forEach((row) => {
    row.addEventListener("dragstart", (e) => { dragIdx = +row.dataset.idx; row.classList.add("dragging"); });
    row.addEventListener("dragend", () => { row.classList.remove("dragging"); clearOver(); });
    row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("drag-over"); });
    row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
    row.addEventListener("drop", (e) => {
      e.preventDefault(); clearOver();
      if (dragIdx == null || dragIdx === +row.dataset.idx) return;
      const r = state.editingRitual;
      const [a] = r.actions.splice(dragIdx, 1);
      const to = +row.dataset.idx;
      r.actions.splice(to, 0, a);
      dragIdx = null;
      renderBuilder();
    });
  });
  function clearOver() { $$(".action-row", list).forEach((c) => c.classList.remove("drag-over")); }
  // live edits
  list.addEventListener("change", (e) => { onActionEdit(e); });
  list.addEventListener("input", debounce((e) => onActionEdit(e), 250));
}
let debounceTimer;
function debounce(fn, ms) { return (...args) => { clearTimeout(debounceTimer); debounceTimer = setTimeout(() => fn(...args), ms); }; }
/* Building a Play is the most effort a user puts in. Leaving with unsaved edits
 * used to discard them silently and the Plays list then showed a
 * saved-looking card. Returns false when the user chose to stay.
 *
 * This has to run in the nav click handler too, not only in navigate(): the
 * handler sets location.hash directly, and that hashchange re-routes on its
 * own, which walked straight past a guard that lived only in navigate(). */
function canLeaveBuilder(route) {
  if (!state.builderDirty) return true;
  if (currentRoute !== "builder" || route === "builder") return true;
  if (!window.confirm("You have unsaved changes to this Play. Leave without saving?")) return false;
  state.builderDirty = false;
  return true;
}

/* Mark the builder dirty WITHOUT re-rendering: a full render while the user is
 * typing steals focus and wipes what they were entering (the same class of bug
 * as the phone pairing input). So the badge is inserted in place instead. */
function markBuilderDirty() {
  if (state.builderDirty) return;
  state.builderDirty = true;
  const save = [...document.querySelectorAll("#page-builder button")]
    .find((b) => b.textContent.trim() === "Save");
  if (!save || $("#b-dirty")) return;
  const tag = document.createElement("span");
  tag.id = "b-dirty";
  tag.textContent = "● Unsaved";
  tag.title = "You have changes that are not saved yet";
  tag.style.cssText = "font-size:12px;color:var(--amber);white-space:nowrap";
  save.parentNode.insertBefore(tag, save);
}

function onActionEdit(e) {
  markBuilderDirty();
  const row = e.target.closest(".action-row"); if (!row) return;
  const idx = +row.dataset.idx;
  const field = e.target.dataset.field;
  const a = state.editingRitual.actions[idx]; if (!a) return;
  const val = e.target.value;
  switch (field) {
    case "target": a.target = val;
      if (!a.name || a.name.startsWith("Open ") || a.name.startsWith("Launch")) a.name = val.split(/[\\/]/).pop() || a.name; break;
    case "args": a.args = val.split(/\s+/).filter(Boolean); break;
    case "name": a.name = val; break;
    case "timeout": a.timeout = val === "" ? null : (parseFloat(val) || null); break;
    case "seconds": a.params.seconds = parseFloat(val) || 1; break;
    case "retries": a.params.retries = parseInt(val) || 0; break;
    case "power": a.params.action = val; a.name = `Power: ${val}`; break;
    case "enabled": a.enabled = e.target.checked; break;
  }
}
function addAction(type) {
  const r = state.editingRitual; if (!r) return;
  const base = { app: "C:\\path\\to\\app.exe", game: "steam://rungameid/570", website: "https://example.com", file: "C:\\path\\to\\folder", command: "", close: "app.exe", delay: "", power: "" };
  r.actions.push(newAction(type, base[type]));
  renderBuilder();
}
function newAction(type, target) {
  const id = uid();
  const base = { id, type, target, args: [], params: {}, enabled: true, continue_on_error: null };
  if (type === "delay") base.params = { seconds: 1 };
  if (type === "power") base.params = { action: "lock" };
  if (type === "website") base.name = `Open ${target}`;
  if (type === "app" || type === "game") base.name = "Launch";
  if (type === "close") base.name = "Close";
  base.name = base.name || "";
  return base;
}
function dupAction(idx) {
  const r = state.editingRitual;
  const copy = JSON.parse(JSON.stringify(r.actions[idx]));
  copy.id = uid();
  r.actions.splice(idx + 1, 0, copy);
  renderBuilder();
}
function remAction(idx) {
  const r = state.editingRitual;
  r.actions.splice(idx, 1);
  renderBuilder();
}
async function saveBuilder() {
  const r = state.editingRitual; if (!r) return;
  const nameEl = $("#b-name"); if (nameEl) r.name = nameEl.value.trim();
  const desc = $("#b-desc"); if (desc) r.description = desc.value.trim();
  const cont = $("#b-continue"); if (cont) r.stop_on_error = !cont.checked;
  if (!r.name) return toast("Play name required", "error");
  try {
    await apiPut(`/rituals/${r.id}`, { name: r.name, description: r.description, actions: r.actions, stop_on_error: r.stop_on_error });
    state.builderDirty = false;
    toast("Saved", "success");
    await refreshRituals();
    const fresh = state.rituals.find((x) => x.id === r.id);
    if (fresh) state.editingRitual = JSON.parse(JSON.stringify(fresh));
  } catch (e) { toast(e.message, "error"); }
}
async function saveAndRun(id) {
  // Save first, then start the run (so the latest edits take effect).
  await saveBuilder();
  if (!state.editingRitual || !state.editingRitual.name) return;
  runRitual(id);
}

/* Builder funcs are callable from inline onclick because they are top-level
 * `function` declarations (hence already on `window`). A former
 * `[...].forEach(f => { window[f] = window[f]; })` no-op sat here; it did
 * nothing and misled readers, so it is gone. */

/* ================================ Deck (Stream Deck) ===================== */
const DECK_KINDS = [
  { k: "app", label: "App", ph: "C:\\path\\to\\app.exe or name" },
  { k: "game", label: "Game", ph: "steam://rungameid/570" },
  { k: "website", label: "Website", ph: "https://example.com" },
  { k: "file", label: "File/Folder", ph: "C:\\Users\\you\\Documents" },
  { k: "command", label: "Command", ph: "command to run" },
  { k: "ritual", label: "Play", ph: "pick a ritual" },
  { k: "media", label: "Media", ph: "play_pause / next / previous" },
  { k: "power", label: "Power", ph: "lock / sleep / restart / shutdown" },
];

function renderDeck() {
  const el = $("#page-deck");
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <div><h1 class="page-title">Deck</h1><p class="page-sub">Your control surface. Tap a button to fire it.</p></div>
      <button class="btn primary" onclick="deckEditor()">+ Add Button</button>
    </div>
    ${mediaPlayerHtml()}
    ${state.deck.length === 0 ? deckEmpty() : `<div class="deck-grid" id="deck-grid">${state.deck.map(deckButtonHtml).join("")}</div>`}`;
  attachDeckDnD();
}
function deckEmpty() {
  return `<div class="placeholder"><div class="big">▩</div>
    <div style="margin-bottom:6px">Your deck is empty.</div>
    <div style="color:var(--muted);font-size:13px;margin-bottom:14px">Add buttons for your games, apps and sites — icons fill in automatically.</div>
    <button class="btn primary" onclick="deckEditor()">+ Add your first button</button></div>`;
}
function deckButtonHtml(b) {
  const icon = (b.icon || "").trim();
  const isImg = /^(https?:)?\/\//.test(icon);
  let iconHtml;
  if (!icon) {
    // Neutral empty-state glyph (never a stark white blob).
    iconHtml = `<span class="deck-icon-empty">＋</span>`;
  } else if (isImg) {
    // If the remote icon fails to load, fall back to a neutral glyph.
    iconHtml = `<img class="deck-icon-img" src="${esc(icon)}" alt="" loading="lazy"
      onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'deck-icon-empty',textContent:'＋'}))">`;
  } else {
    iconHtml = `<span class="deck-icon">${esc(icon)}</span>`;
  }
  const label = b.label || b.target || b.kind;
  return `
    <div class="deck-btn" draggable="true" data-id="${b.id}" title="${esc(label)}"
         style="${b.color ? `--deck-tint:${esc(b.color)};` : ""}"
         onclick="pressDeck('${b.id}')">
      <div class="deck-btn-icon">${iconHtml}</div>
      <div class="deck-btn-label">${esc(label)}</div>
      <button class="deck-edit" title="Edit" onclick="event.stopPropagation();deckEditor('${b.id}')">⋯</button>
    </div>`;
}
async function pressDeck(id) {
  const btn = document.querySelector(`.deck-btn[data-id="${id}"]`);
  if (btn) { btn.classList.add("pressing"); setTimeout(() => btn.classList.remove("pressing"), 220); }
  try {
    await apiPost(`/deck/${id}/press`);
  } catch (e) { toast(e.message, "error"); }
}
function attachDeckDnD() {
  const grid = $("#deck-grid"); if (!grid) return;
  let dragId = null;
  $$(".deck-btn", grid).forEach((card) => {
    card.addEventListener("dragstart", (e) => { dragId = card.dataset.id; card.classList.add("dragging"); });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drag-over"); });
    card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
    card.addEventListener("drop", (e) => {
      e.preventDefault(); card.classList.remove("drag-over");
      if (!dragId || dragId === card.dataset.id) return;
      const from = state.deck.findIndex((b) => b.id === dragId);
      const to = state.deck.findIndex((b) => b.id === card.dataset.id);
      const [moved] = state.deck.splice(from, 1);
      state.deck.splice(to, 0, moved);
      renderDeck();
      apiPost("/deck/reorder", { ids: state.deck.map((b) => b.id) }).catch(() => {});
    });
  });
}
function deckEditor(id) {
  const b = id ? state.deck.find((x) => x.id === id) : { kind: "app", target: "", label: "", icon: "", color: "" };
  const kinds = DECK_KINDS.map((k) => `<option value="${k.k}" ${b.kind === k.k ? "selected" : ""}>${k.label}</option>`).join("");
  const m = openModal(`
    <h2>${id ? "Edit" : "Add"} Deck Button</h2>
    <label class="field">Type
      <select id="dk-kind">${kinds}</select>
    </label>
    <label class="field" style="margin-top:12px">Target
      <input type="text" id="dk-target" value="${esc(b.target || "")}" placeholder="path, URL, or value" autofocus>
    </label>
    <label class="field" style="margin-top:12px">Label (optional)
      <input type="text" id="dk-label" value="${esc(b.label || "")}" placeholder="shown under the button">
    </label>
    <label class="field" style="margin-top:12px">Icon (emoji or image URL — leave blank to auto-detect)
      <input type="text" id="dk-icon" value="${esc(b.icon || "")}" placeholder="🎮  or  https://…/icon.png">
    </label>
    <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
      <span style="color:var(--muted);font-size:13px">Tint</span>
      <input type="color" id="dk-color" value="${esc(b.color || "#4d9fff")}" style="width:48px;height:32px;padding:0;border:none;background:none">
      <label style="display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px">Preview:
        <span id="dk-preview" style="font-size:24px"></span></label>
    </div>
    <div class="modal-actions">
      ${id ? `<button class="btn danger" id="dk-del">Delete</button>` : ""}
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn primary" id="dk-save">${id ? "Save" : "Add"}</button>
    </div>`);
  const refreshPreview = async () => {
    const kind = m.querySelector("#dk-kind").value;
    const target = m.querySelector("#dk-target").value.trim();
    const icon = m.querySelector("#dk-icon").value.trim();
    const pv = m.querySelector("#dk-preview");
    if (icon) { pv.innerHTML = /^(https?:)?\/\//.test(icon) ? `<img src="${esc(icon)}" style="width:28px;height:28px;border-radius:6px">` : esc(icon); return; }
    try { const r = await apiGet(`/deck/icon?kind=${encodeURIComponent(kind)}&target=${encodeURIComponent(target)}`);
      pv.innerHTML = r.type === "url" ? `<img src="${esc(r.value)}" style="width:28px;height:28px;border-radius:6px">` : esc(r.value);
    } catch (_) {}
  };
  m.querySelector("#dk-target").addEventListener("input", refreshPreview);
  m.querySelector("#dk-kind").addEventListener("change", refreshPreview);
  m.querySelector("#dk-icon").addEventListener("input", refreshPreview);
  refreshPreview();
  m.querySelector("#dk-save").addEventListener("click", async () => {
    const payload = {
      kind: m.querySelector("#dk-kind").value,
      target: m.querySelector("#dk-target").value.trim(),
      label: m.querySelector("#dk-label").value.trim(),
      icon: m.querySelector("#dk-icon").value.trim(),
      color: m.querySelector("#dk-color").value,
    };
    if (!payload.target) return toast("Target required", "error");
    if (id) payload.id = id;
    try {
      if (id) await apiPut(`/deck/${id}`, payload); else await apiPost("/deck", payload);
      closeModal(); await refreshDeck(); renderDeck(); toast(id ? "Saved" : "Added", "success");
    } catch (e) { toast(e.message, "error"); }
  });
  const del = m.querySelector("#dk-del");
  if (del) del.addEventListener("click", async () => {
    try { await apiDel(`/deck/${id}`); closeModal(); await refreshDeck(); renderDeck(); toast("Deleted"); }
    catch (e) { toast(e.message, "error"); }
  });
}

/* ---------- Media player (Apple Music style) ---------- */
function mediaPlayerHtml() {
  const m = state.media || {};
  const hasTrack = !!(m.display || m.title);
  const artwork = (m.artwork || "").trim();
  const title = hasTrack ? (m.title || m.display) : "Nothing Playing";
  const artist = hasTrack ? (m.artist || m.source || "Now Playing") : "Open Spotify and press play";
  // Real position/duration only when the backend provides them (Spotify Web API).
  const pos = Number(m.position || 0), dur = Number(m.duration || 0);
  const haveTime = dur > 0;
  const pct = haveTime ? Math.min(100, Math.round((pos / dur) * 100)) : 0;
  const fmt = (s) => { s = Math.max(0, Math.floor(s)); return `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`; };
  // Album art when we have it, otherwise a stylised gradient tile.
  const artInner = artwork
    ? `<img class="am-art-img" src="${esc(artwork)}" alt=""
         onerror="this.style.display='none';this.parentElement.classList.add('no-art')">
       <div class="am-art-glow"></div><span class="am-art-glyph">♪</span>`
    : `<div class="am-art-glow"></div><span class="am-art-glyph">${hasTrack ? "♪" : "♫"}</span>`;
  return `
    <div class="am-player ${hasTrack ? "has-track" : ""} ${artwork ? "has-art" : ""}" id="media-player">
      <div class="am-art">${artInner}</div>
      <div class="am-body">
        <div class="am-head">
          <div class="am-track">
            <div class="am-title">${esc(title)}</div>
            <div class="am-artist">${esc(artist)}</div>
          </div>
          <div class="am-visualizer ${hasTrack ? "on" : ""}" title="Now playing">
            <span></span><span></span><span></span><span></span>
          </div>
        </div>
        <div class="am-progress">
          <span class="am-time am-time-elapsed">${haveTime ? fmt(pos) : ""}</span>
          <div class="am-bar" data-seek-seconds="${haveTime ? dur : 0}" role="slider"
               aria-label="Playback position" aria-valuemin="0"
               aria-valuemax="${haveTime ? Math.floor(dur) : 0}"
               aria-valuenow="${haveTime ? Math.floor(pos) : 0}"
               title="${haveTime ? "Click to seek" : ""}">
            <div class="am-bar-fill" style="width:${pct}%"></div>
            <div class="am-bar-thumb" style="left:${pct}%"></div>
          </div>
          <span class="am-time am-time-total">${haveTime ? "-" + fmt(dur - pos) : ""}</span>
        </div>
        <div class="am-controls">
          <button class="am-pill am-mini ${m.shuffle ? "on" : ""}" title="Shuffle"
                  onclick="mediaControl('shuffle', ${m.shuffle ? "false" : "true"})">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M17 3l4 4-4 4V8h-2.5l-2.2 3-1.4-1.9L13.5 6H17V3zM3 6h3.5l3 4-1.4 1.9L5.9 9H3V6zm14 9h-3.5l-1.6-2.1 1.4-1.9L15.5 13H17v-3l4 4-4 4v-3zM3 15h2.9l1.2-1.6 1.4 1.9-1.6 2.1L5 18H3v-3z"/></svg>
          </button>
          <button class="am-pill" title="Previous" onclick="mediaControl('previous')">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>
          </button>
          <button class="am-pill am-play" title="${m.is_playing ? "Pause" : "Play"}" onclick="mediaControl('play_pause')">
            ${m.is_playing
              ? `<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor"><path d="M7 5h4v14H7zm6 0h4v14h-4z"/></svg>`
              : `<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`}
          </button>
          <button class="am-pill" title="Next" onclick="mediaControl('next')">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M16 6h2v12h-2zm-1.5 6L6 18V6z"/></svg>
          </button>
          <button class="am-pill am-mini ${m.repeat && m.repeat !== "off" ? "on" : ""}" title="Repeat"
                  onclick="mediaControl('repeat', '${m.repeat === "off" ? "context" : (m.repeat === "context" ? "track" : "off")}')">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>
          </button>
        </div>
      </div>
    </div>`;
}
async function mediaControl(action, value) {
  try {
    await apiPost("/media/control", { action, value });
    setTimeout(() => refreshMedia().then(() => { if (currentRoute === "deck") renderDeck(); }), 300);
  } catch (e) { toast(e.message, "error"); }
}
window.mediaControl = mediaControl;
window.pressDeck = pressDeck;
window.deckEditor = deckEditor;

/* ================================ Phone Remote =========================== */
// Holds the last generated pairing data so page re-renders don't wipe it.
let activePairing = null;

async function renderRemote() {
  const el = $("#page-remote");
  if (!el) return;
  const isLocal = await isLoopback();       // true = this IS the PC
  const pairedPhone = !isLocal && !!token;  // a phone with a device token
  // Which shape of screen is needed. Re-rendering the SAME shape is skipped
  // entirely, so the poll tick can never destroy the pairing <input> while the
  // user is typing into it (second line of defence behind isUserEditing()).
  const mode = isLocal ? "local" : (pairedPhone ? "paired" : "entry");

  let head = "";
  if (isLocal) {
    // On the PC we always offer to pair a phone — regardless of being logged in.
    // Deliberately NO "enter code manually" here: a PC redeeming its own code
    // would write a *device* token over the logged-in *session* token, silently
    // turning the PC into a paired device and losing its account session.
    head = `${remoteAccessCard()}${tlsCertCard()}
      <div class="builder-head" style="margin-bottom:16px">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" onclick="startPairing()">Generate Pairing Code</button>
      </div></div>
      <div id="pair-area">${activePairing ? pairAreaHtml(activePairing) : ""}</div>`;
  } else if (!pairedPhone) {
    // A phone that isn't paired yet -> code entry screen.
    head = pairEntryHtml();
  }

  const sub = isLocal
    ? "Pair your iPhone to control this PC."
    : (pairedPhone ? "Connected to your PC." : "Enter the pairing code shown on your PC.");

  if (el.dataset.mode !== mode) {
    el.innerHTML = `
      <h1 class="page-title">Phone Remote</h1>
      <p class="page-sub">${sub}</p>
      <div id="pair-entry">${head}</div>
      <div id="remote-body">${pairedPhone ? remoteBody() : ""}</div>`;
    el.dataset.mode = mode;
  } else {
    // Same screen: update only the free-text line. Input values are left alone.
    const p = el.querySelector(".page-sub");
    if (p) p.textContent = sub;
  }
  if (pairedPhone) renderRemoteBody();
}

// The pairing screen shown ON THE PHONE (no token, not local).
function pairEntryHtml() {
  return `
    <div class="card" style="max-width:420px;margin:0 auto">
      <h3 style="margin-bottom:6px">Connect this phone</h3>
      <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
        On your PC, open <strong>Phone Remote → Generate Pairing Code</strong>, then scan the QR code — or type the 6-character code below.
      </p>
      <label class="field">Pairing code
        <input type="text" id="ph-code" inputmode="text" autocapitalize="characters" autocomplete="off"
               placeholder="e.g. A2D51F" maxlength="8"
               style="text-transform:uppercase;letter-spacing:.3em;font-size:22px;text-align:center;font-weight:700">
      </label>
      <label class="field" style="margin-top:12px">Device name
        <input type="text" id="ph-name" value="iPhone" maxlength="40">
      </label>
      <button class="btn primary" style="width:100%;margin-top:16px;justify-content:center" id="ph-go">Pair this phone</button>
      <button class="btn" style="width:100%;margin-top:8px;justify-content:center" id="ph-scan" onclick="openQRScanner()">Scan QR code with camera</button>
      <div id="ph-msg" style="margin-top:10px;font-size:13px;text-align:center"></div>
    </div>`;
}

async function submitPhonePairing() {
  const codeEl = $("#ph-code");
  const msg = $("#ph-msg");
  const code = (codeEl ? codeEl.value : "").trim().toUpperCase();
  if (!code) { if (msg) msg.innerHTML = `<span style="color:var(--red)">Enter the code from your PC.</span>`; return; }
  const btn = $("#ph-go"); if (btn) { btn.disabled = true; btn.textContent = "Pairing…"; }
  try {
    const res = await apiPost("/pair/complete", { code, device_name: ($("#ph-name") || {}).value || "iPhone" });
    if (res && res.token) {
      setToken(res.token);
      connectSSE();   // open the authenticated stream immediately, don't wait for a retry
      toast("Paired! You can now control your PC.", "success");
      await refresh();
      render();
    } else {
      throw new Error("pairing failed");
    }
  } catch (e) {
    if (msg) msg.innerHTML = `<span style="color:var(--red)">${esc(e.message || "Invalid or expired code. Generate a new one on your PC.")}</span>`;
    if (btn) { btn.disabled = false; btn.textContent = "Pair this phone"; }
  }
}
window.submitPhonePairing = submitPhonePairing;

/* ---- in-app QR scanner ------------------------------------------------
 * Safari has no BarcodeDetector, so jsQR (vendored, MIT — see vendor/jsqr.js)
 * decodes frames straight off the camera.
 *
 * The camera is only reachable in a SECURE CONTEXT. Served over plain http on a
 * LAN IP, Safari does not expose navigator.mediaDevices at all, so instead of a
 * button that silently does nothing we detect that up front and say what to do
 * instead (the iPhone Camera app scans the same QR and opens Easy Life already
 * paired, because the QR carries a short-lived pairing code in its URL).
 */
let qrScannerActive = false;
let qrStream = null;
let qrRAF = 0;
// Small browsers (and the jsdom test harness) may not provide rAF. Falling back
// to a timer keeps the decode loop working instead of throwing on the first frame.
const raf = (cb) => (typeof requestAnimationFrame === "function"
  ? requestAnimationFrame(cb) : setTimeout(() => cb(Date.now()), 100));
const caf = (h) => { if (!h) return;
  if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(h); else clearTimeout(h); };

function qrScannerBlockedReason() {
  if (typeof window.isSecureContext !== "undefined" && !window.isSecureContext) {
    return "iOS only allows the camera on https:// addresses. Use your iPhone Camera app to scan the QR on your PC — it opens Easy Life already paired.";
  }
  if (!window.jsQR) return "The QR reader did not load. Reload the page and try again.";
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
    return "This browser blocks the camera here. Use your iPhone Camera app to scan the QR on your PC instead.";
  }
  return "";
}

/* Accepts either a full pairing URL (…/#/remote?code=A2D51F) or a bare code. */
function extractPairCode(text) {
  if (!text) return "";
  const s = String(text).trim();
  const m = s.match(/[?&]code=([A-Za-z0-9]+)/);
  if (m) return m[1].toUpperCase();
  const bare = s.toUpperCase();
  return /^[A-Z0-9]{4,12}$/.test(bare) ? bare : "";
}
window.extractPairCode = extractPairCode;

async function openQRScanner() {
  const blocked = qrScannerBlockedReason();
  if (blocked) { toast(blocked, "error"); return; }
  if (qrScannerActive) return;

  const wrap = document.createElement("div");
  wrap.className = "modal-backdrop";
  wrap.innerHTML = `
    <div class="modal qr-scan-modal">
      <h2>Scan the QR on your PC</h2>
      <p class="qr-scan-hint">Point your phone at the code on your PC's Phone Remote screen.</p>
      <div class="qr-video-wrap">
        <video id="qr-video" playsinline muted autoplay></video>
        <div class="qr-reticle" aria-hidden="true"></div>
      </div>
      <div id="qr-scan-msg" class="qr-scan-msg">Looking for a code…</div>
      <div class="modal-actions"><button class="btn" id="qr-cancel">Cancel</button></div>
    </div>`;
  document.getElementById("modal-root").appendChild(wrap);
  qrScannerActive = true;

  const video = wrap.querySelector("#qr-video");
  const msg = wrap.querySelector("#qr-scan-msg");
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext && canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) {   // no 2D canvas: decoding is impossible, say so rather than loop on null
    msg.textContent = "This browser can't read the camera image. You can type the code instead.";
    stop();
    return;
  }

  const stop = () => {
    qrScannerActive = false;
    if (qrRAF) { caf(qrRAF); qrRAF = 0; }
    if (qrStream) { try { qrStream.getTracks().forEach((t) => t.stop()); } catch (_) {} qrStream = null; }
    wrap.remove();
  };
  wrap.querySelector("#qr-cancel").addEventListener("click", stop);
  wrap.addEventListener("click", (e) => { if (e.target === wrap) stop(); });

  try {
    qrStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } }, audio: false,
    });
  } catch (e) {
    msg.innerHTML = `<span style="color:var(--red)">Camera unavailable (${esc((e && e.message) || "permission denied")}). You can type the code instead.</span>`;
    stop();
    return;
  }
  video.srcObject = qrStream;
  try { await video.play(); } catch (_) {}

  let handling = false;
  const scan = () => {
    if (!qrScannerActive) return;
    // readyState 2 (HAVE_CURRENT_DATA) is enough to draw a frame, and unlike the
    // HAVE_ENOUGH_DATA constant it is present on every implementation.
    if (video.readyState >= 2) {
      const w = video.videoWidth, h = video.videoHeight;
      if (w && h) {
        canvas.width = w; canvas.height = h;
        ctx.drawImage(video, 0, 0, w, h);
        let code = "";
        try {
          const img = ctx.getImageData(0, 0, w, h);
          const found = window.jsQR(img.data, w, h, { inversionAttempts: "dontInvert" });
          if (found && found.data) code = extractPairCode(found.data);
        } catch (_) {}
        if (code && !handling) {
          handling = true;
          msg.textContent = "Code found — pairing…";
          const name = ($("#ph-name") || {}).value || "iPhone";
          apiPost("/pair/complete", { code, device_name: name })
            .then((res) => {
              if (!res || !res.token) throw new Error("pairing failed");
              setToken(res.token);
              connectSSE();
              stop();
              toast("Paired! You can now control your PC.", "success");
              return refresh();
            })
            .then(() => render())
            .catch((e) => {
              handling = false;
              msg.innerHTML = `<span style="color:var(--red)">${esc((e && e.message) || "Invalid or expired code.")}</span>`;
            });
          return;
        }
      }
    }
    qrRAF = raf(scan);
  };
  qrRAF = raf(scan);
}
window.openQRScanner = openQRScanner;

/* ---- on-screen keyboard / portrait -------------------------------------
 * iOS Safari does not resize the layout viewport for the keyboard, so a focused
 * field could sit underneath the fixed bottom tab bar with no way to see what
 * was typed. Track the visual viewport and lift the page clear while typing.
 */
function initKeyboardHandling() {
  const vv = window.visualViewport;
  if (vv) {
    const sync = () => {
      const covered = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      document.body.classList.toggle("kb-open", covered > 120);
    };
    vv.addEventListener("resize", sync);
    vv.addEventListener("scroll", sync);
  }
  document.addEventListener("focusin", (e) => {
    const t = e.target;
    if (!t || (t.tagName !== "INPUT" && t.tagName !== "TEXTAREA")) return;
    setTimeout(() => { try { t.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {} }, 280);
  });
}
window.initKeyboardHandling = initKeyboardHandling;
/* Exposed so the regression suite can prove the poll tick refuses to re-render
 * a screen the user is typing into. */
window.isUserEditing = isUserEditing;

// Attach the phone-pairing button/enter-key handlers after the screen renders.
document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "ph-go") submitPhonePairing();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target && e.target.id === "ph-code") submitPhonePairing();
});
async function isLoopback() {
  try { return location.hostname === "localhost" || location.hostname === "127.0.0.1" || location.hostname === "[::1]" || location.hostname === ""; }
  catch (_) { return false; }
}
/* The Stream Rescue card on the phone belongs only to someone who turned on
 * Streamer Mode. Without this, every paired phone showed an OBS rescue card
 * for a user who has never opened OBS. */
function streamerModeOn() {
  return !!(state.settings && state.settings.streamer_mode);
}

function renderRemoteBody() {
  // Deliberately synchronous: it awaits nothing, and as an async function any
  // throw became an unhandled promise rejection that bypassed render()'s error
  // isolation (blank page, one rejection per 2.5s poll tick). Synchronous means
  // the callers' try/catch and render()'s promise guard both catch it.
  const el = $("#remote-body"); if (!el) return;
  el.innerHTML = remoteBody();
  const body = $("#remote-body-inner"); if (body && state.currentRun) renderCurrentRunRemote(body);
  // The rescue card is the reason a phone is useful away from home, so it
  // loads on every render of the remote screen.
  if ($("#obs-box")) renderOBSBox();
}
function remoteBody() {
  const connected = !!token;
  return `<div class="remote-status" style="background:${connected ? "var(--bg-3)" : "var(--bg-2)"};border:1px solid var(--border)">
    <span class="dot ${connected ? "" : "offline"}"></span>
    <div><strong>${connected ? "Connected" : "Not paired"}</strong><br>
      <span style="color:var(--muted);font-size:13px">${connected ? `Controlling this PC` : `Open Safari, go to this PC's IP, then pair.`}</span></div>
    ${connected ? `<button class="icon-btn" style="margin-left:auto" title="Forget this device" aria-label="Forget this device" onclick="unpair()">⏻</button>` : ""}
  </div>
  <div id="remote-body-inner">
    ${connected ? `
    ${streamerModeOn() ? `
    <div class="card" id="obs-card" style="margin-bottom:16px;border-color:rgba(255,93,93,.32)">
      <h3 style="margin-bottom:6px">Stream rescue</h3>
      <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
        OBS crashed or the stream dropped? This gets it back without you touching the PC.
      </p>
      <div id="obs-box"><div style="color:var(--muted);font-size:13px">Checking OBS…</div></div>
    </div>` : ""}
    <div class="card" style="margin-bottom:16px">
      <h3 style="margin-bottom:12px">PC Controls</h3>
      <div class="power-grid">
        <button class="btn" onclick="powerControl('lock')">🔒 Lock</button>
        <button class="btn" onclick="powerControl('sleep')">💤 Sleep</button>
        <button class="btn danger" onclick="powerControl('restart')">↻ Restart</button>
        <button class="btn danger" onclick="powerControl('shutdown')">⏻ Shutdown</button>
      </div>
    </div>
    <h3 style="margin-bottom:12px">Plays</h3>
    <div class="grid grid-2">${state.rituals.map((r) => `
      <div class="ritual-card">
        <div class="top"><h3>${esc(r.name)}</h3><span class="stat-pill">${stepLabel(stepCount(r))}</span></div>
        <div class="actions-row"><button class="btn green" onclick="runRitual('${r.id}','phone')">▶ Run</button></div>
      </div>`).join("") || `<div class="empty"><span style="color:var(--muted)">No plays yet.</span></div>`}
    </div>` : `<div class="empty"><div class="big">◉</div>Pair this device to control your PC.</div>`}
  </div>`;
}
async function renderOBSBox() {
  const el = $("#obs-box");
  if (!el) return;
  let s = {};
  try { s = (await apiGet("/obs/status")) || {}; }
  catch (e) { el.innerHTML = `<div style="color:var(--muted);font-size:13px">${esc(e.message)}</div>`; return; }

  const dot = s.streaming ? "var(--green)" : (s.reachable ? "var(--amber)" : "var(--red)");
  const label = s.streaming
    ? (s.reconnecting ? "Reconnecting…" : "Live")
    : (s.reachable ? "OBS is open but not streaming" : "OBS isn't responding");

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:9px;margin-bottom:12px">
      <span style="width:9px;height:9px;border-radius:50%;background:${dot};flex:none"></span>
      <strong>${esc(label)}</strong>
      ${s.streaming && s.timecode ? `<span style="margin-left:auto;color:var(--muted);font-size:12px">${esc(s.timecode)}</span>` : ""}
    </div>
    ${s.error ? `<div style="color:var(--muted);font-size:12px;margin-bottom:12px">${esc(s.error)}</div>` : ""}
    <button class="btn primary" style="width:100%;justify-content:center" onclick="rescueStream()">
      ${s.streaming ? "Restart the stream" : "Fix my stream"}
    </button>
    ${s.streaming ? `<button class="btn" style="width:100%;justify-content:center;margin-top:8px" onclick="stopStream()">Stop the stream</button>` : ""}
    <div id="obs-msg" style="margin-top:10px;font-size:12.5px;color:var(--muted)"></div>`;
}
window.renderOBSBox = renderOBSBox;

async function rescueStream() {
  const msg = $("#obs-msg");
  if (msg) msg.textContent = "Working… if OBS has to restart this can take up to a minute.";
  try {
    const r = await apiPost("/obs/rescue", {});
    if (msg) msg.textContent = (r && r.message) || "Done.";
    if (r && r.ok) toast("Stream is back", "success");
  } catch (e) {
    if (msg) msg.textContent = e.message;
    toast(e.message, "error");
  }
  renderOBSBox();
}
window.rescueStream = rescueStream;
async function stopStream() {
  const msg = $("#obs-msg");
  try { await apiPost("/obs/stream/stop", {}); if (msg) msg.textContent = "Stream stopped."; }
  catch (e) { if (msg) msg.textContent = e.message; }
  renderOBSBox();
}
window.stopStream = stopStream;

function renderCurrentRunRemote(host) {
  const r = state.currentRun;
  if (!runIsActive(r)) { return; }
  const stopping = isStopping(r);
  const pct = r.actions_total > 0 ? Math.round((r.actions_completed / r.actions_total) * 100) : 0;
  host.innerHTML = `<div style="background:var(--bg-2);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px">
    <div style="display:flex;justify-content:space-between;align-items:center"><strong>${esc(r.name)}</strong>
      <button class="btn danger sm" onclick="stopRun()" ${stopping ? "disabled" : ""}>${stopping ? "Stopping…" : "■ Stop"}</button></div>
    <div class="progress-track" style="margin-top:10px"><div class="progress-fill" style="width:${pct}%"></div></div>
    <div style="margin-top:8px;color:var(--muted)">${r.actions_completed}/${r.actions_total}${stopping ? " · Stopping…" : ""}</div></div>` + host.innerHTML;
}

async function startPairing() {
  try {
    const res = await apiPost("/pair/start");
    activePairing = res;
    const area = $("#pair-area");
    if (area) area.innerHTML = pairAreaHtml(res);
  } catch (e) { toast(e.message, "error"); }
}
// When HTTPS is on, the phone must trust the self-signed certificate before
// iOS will let the camera work. Show the one-time install step on the PC so the
// user doesn't have to transfer a file by hand.
function tlsCertCard() {
  const t = state.pairing && state.pairing.tls;
  if (!t || !t.enabled) return "";
  const url = t.cert_url || "";
  return `<div class="card" style="margin-bottom:16px;border-color:rgba(56,225,240,.35)">
    <h3 style="margin-bottom:6px">Step 1 — install the certificate on your phone</h3>
    <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
      HTTPS is on, but your iPhone only lets the camera work once it trusts the certificate.
      On your phone, open this address and tap <strong>Install Profile</strong>:
    </p>
    <code style="display:block;background:var(--bg-3);padding:10px;border-radius:8px;word-break:break-all;color:var(--accent)">${esc(url || "http://<your-pc-ip>:8765/cert")}</code>
    <p style="color:var(--muted);font-size:12px;margin-top:10px">
      Then: Settings → General → About → Certificate Trust Settings → enable full trust for “Easy Life”.
    </p>
  </div>`;
}

// Away-from-home access. The app can only *see* the tunnel, it cannot install
// it — so when Tailscale is missing this explains the one thing the user has to
// do rather than showing a dead address.
function remoteAccessCard() {
  const r = state.pairing && state.pairing.remote;
  if (!r) return "";
  if (r.ready) {
    return `<div class="card" style="margin-bottom:16px;border-color:rgba(52,245,165,.35)">
      <h3 style="margin-bottom:6px">✓ Remote access is on</h3>
      <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
        Your phone can reach this PC from anywhere — not just at home. Pair using this address:
      </p>
      <code style="display:block;background:var(--bg-3);padding:10px;border-radius:8px;word-break:break-all;color:var(--green)">${esc(r.away_url)}</code>
      <p style="color:var(--muted);font-size:12px;margin-top:10px">
        Nothing to install on your phone — just open that address. Add it to your
        Home Screen so it is there when you need it. At home it still works the same way.
      </p>
    </div>`;
  }
  return `<div class="card" style="margin-bottom:16px">
    <h3 style="margin-bottom:6px">Control your PC from anywhere</h3>
    <p style="color:var(--muted);font-size:13px;margin-bottom:10px">
      <strong>At home</strong> your phone reaches this PC over your Wi-Fi — scan the QR
      below and you are done. No account, no extra software.
    </p>
    <p style="color:var(--muted);font-size:12px">
      <strong>From anywhere else</strong> (mobile data, a hotel), open
      <em>Settings → Use your phone from anywhere</em>. It needs one free install on
      <em>this PC</em> and nothing at all on your phone.
    </p>
  </div>`;
}

function pairAreaHtml(res) {
  const lanUrl = res.lan_url || "";
  let qrHtml = "";
  if (res.qr_png) {
    // Server data, so it goes through esc() like every other interpolated field.
    // (It is a server-generated data: URL today; escaping it keeps this sink
    // harmless if the field ever carries anything else.)
    qrHtml = `<div class="qr-box"><img src="${esc(res.qr_png)}" alt="Pairing QR" width="200" height="200"></div>`;
  }
  return `
    <div class="card" style="text-align:center;max-width:380px;margin:0 auto">
      <h3 style="margin-bottom:4px">Scan with your iPhone</h3>
      <p style="color:var(--muted);font-size:13px">Open Safari, scan this QR, and pair.</p>
      ${qrHtml || `<p style="color:var(--muted);font-size:13px">QR unavailable — use the code below.</p>`}
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px">or enter code:</div>
      <div class="pair-code">${esc(res.code)}</div>
      <div style="color:var(--muted);font-size:12px;margin-top:10px">Expires in ${esc(res.expires_in)}s</div>
      <div style="text-align:left;margin-top:14px;background:var(--bg-3);padding:10px;border-radius:8px;font-size:12px">
      <div style="color:var(--muted);margin-bottom:4px">On your phone (same Wi-Fi), open:</div>
      <code id="pair-lan-url" style="color:var(--accent);word-break:break-all">${lanUrl ? esc(lanUrl) : "http://<your-pc-ip>:8765"}</code><br>
      <span style="color:var(--muted)">then pair with code <strong>${esc(res.code)}</strong></span></div>
    </div>`;
}
function manualPairModal() {
  const m = openModal(`
    <h2>Pair Device</h2>
    <label class="field">Pairing Code<input type="text" id="mp-code" autofocus placeholder="e.g. 3F9A2B" style="text-transform:uppercase"></label>
    <label class="field" style="margin-top:12px">Device Name<input type="text" id="mp-name" value="iPhone" placeholder="This phone"></label>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn primary" id="mp-do">Pair</button>
    </div>`);
  m.querySelector("#mp-do").addEventListener("click", async () => {
    const code = m.querySelector("#mp-code").value.trim();
    if (!code) return toast("Enter pairing code", "error");
    try {
      const res = await apiPost("/pair/complete", { code, device_name: m.querySelector("#mp-name").value.trim() || "iPhone" });
      if (!res || !res.token) throw new Error("pairing failed");
      // Never replace an account session with a device token. On the PC this
      // modal is not offered, but guard it anyway: the two token kinds share one
      // storage key, and overwriting it silently signs the PC out of its account.
      if (await isLoopback() && token) {
        toast("This PC is already signed in — pair your phone instead.", "error");
        closeModal();
        return;
      }
      setToken(res.token);
      closeModal(); toast("Paired! You can control this PC.", "success");
      refresh().then(render);
    } catch (e) { toast(e.message, "error"); }
  });
}
function unpair() {
  confirmModal("Forget Device", "Remove this device's access? You'll need to pair again.", "Forget").then((ok) => {
    if (ok) { setToken(""); toast("Unpaired"); refresh().then(render); }
  });
}
async function powerControl(action) {
  if (action === "restart" || action === "shutdown") {
    const ok = await confirmModal(action.toUpperCase(), `Are you sure you want to ${action === "restart" ? "restart" : "shut down"} this PC?`, action.toUpperCase(), "danger");
    if (!ok) return;
  }
  try { await apiPost("/power", { action, confirm: action === "restart" || action === "shutdown" ? true : true }); toast(`${action} initiated`, "success"); }
  catch (e) { toast(e.message, "error"); }
}

window.startPairing = startPairing;
window.manualPairModal = manualPairModal;
window.unpair = unpair;
window.powerControl = powerControl;

/* ================================ Activity =============================== */
function renderActivity() {
  const el = $("#page-activity");
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <div><h1 class="page-title">Activity</h1><p class="page-sub">Execution history.</p></div>
      <button class="btn danger" onclick="clearHistory()">Clear History</button>
    </div>
    <div id="act-filter" style="margin:16px 0"><input type="text" id="act-search" placeholder="Search rituals…" style="max-width:320px"></div>
    <div id="act-list"></div>`;
  renderActivityList();
  const s = $("#act-search");
  s.addEventListener("input", () => renderActivityList(s.value.trim().toLowerCase()));
}
function renderActivityList(filter = "") {
  const el = $("#act-list"); if (!el) return;
  const items = state.history.filter((h) => !filter || (h.ritual_name || "").toLowerCase().includes(filter) || (h.error || "").toLowerCase().includes(filter));
  if (!items.length) { el.innerHTML = `<div class="empty"><div class="big">≣</div>No activity yet.</div>`; return; }
  el.innerHTML = items.map((h) => {
    const badge = { completed: "success", failed: "failed", stopped: "stopped", running: "running" }[h.status] || "";
    const dur = h.duration_ms != null ? (h.duration_ms / 1000).toFixed(1) + "s" : "—";
    return `<div class="history-item">
      <div>
        <div style="font-weight:600">${esc(h.ritual_name)}</div>
        <div style="color:var(--muted);font-size:12px">${esc(fmtTime(h.started_at))} · ${h.actions_completed}/${h.actions_total} steps · ${dur} · via ${esc(h.triggered_by || "local")}</div>
        ${h.error ? `<div style="color:var(--red);font-size:12px;margin-top:4px">${esc(h.error)}</div>` : ""}
      </div>
      <span class="badge ${badge}">${esc(h.status)}</span>
    </div>`;
  }).join("");
}
async function clearHistory() {
  const ok = await confirmModal("Clear History", "Delete all execution history?", "Clear");
  if (!ok) return;
  try { await apiDel("/history"); await refreshHistory(); render(); toast("History cleared"); }
  catch (e) { toast(e.message, "error"); }
}
function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* ================================ Integrations =========================== */
let integrationData = null;   // null = not loaded (retry); [] = loaded-empty
let integrationLoading = false;
let integrationError = null;

async function renderIntegrations() {
  const el = $("#page-integrations");
  // The sentinel stays `null` on failure so the next visit retries. A failed
  // load previously stored `[]`, which is indistinguishable from a successful
  // empty result, so the page never tried again.
  // No timer/backoff is needed: the poll tick only re-renders dashboard and
  // remote, so this only re-runs when the user comes back to the page.
  if (integrationData === null && !integrationLoading) {
    integrationLoading = true;
    try {
      const d = await apiGet("/integrations");
      integrationData = Array.isArray(d) ? d : [];
      integrationError = null;
    } catch (e) {
      integrationError = (e && e.message) || "request failed";
    } finally {
      integrationLoading = false;
    }
  }
  // Never assume the data is loaded — this runs during the fetch too.
  const list = Array.isArray(integrationData) ? integrationData : [];
  el.innerHTML = `
    <h1 class="page-title">Integrations</h1>
    <p class="page-sub">Applications you can control inside Plays. Detected live on this machine.</p>
    <button class="btn sm" style="margin-bottom:16px" onclick="refreshIntegrationData()">↻ Re-scan</button>
    <div class="grid grid-3">
      ${list.length
        ? list.map((i) => integrationCard(i)).join("")
        : `<div class="card" style="grid-column:1/-1;color:var(--muted)">${
            integrationError
              ? `Couldn't load integrations (${esc(integrationError)}). Retrying…`
              : (integrationLoading ? "Detecting…" : "No integrations detected. Click <strong>Re-scan</strong>.")
          }</div>`}
    </div>
    <div class="card" style="margin-top:20px">
      <strong>How to use integrations</strong>
      <p style="color:var(--muted);margin-top:8px">Add a <strong>Launch Game</strong> action and use each store's URL scheme:
        <code style="display:block;background:var(--bg-3);padding:10px;border-radius:8px;margin-top:8px;font-size:12px">
        Steam → steam://rungameid/<span style="color:var(--accent)">570</span><br>
        Epic → com.epicgames.launcher://apps/<span style="color:var(--accent)">APPID</span>?action=launch<br>
        Battle.net → battle.net://<span style="color:var(--accent)">play/blizzard-app</span><br>
        Riot → riot://<span style="color:var(--accent)">launch/product_name</span>
        </code></p>
    </div>`;
}
function integrationCard(i) {
  const kindIcon = i.kind === "rgb" ? "⌬" : i.kind === "game" ? "🕹" : i.kind === "app" ? "▣" : "⚿";
  let badge;
  if (i.installed === null) badge = `<span class="badge" style="background:#3a3014;color:#fcd34d">Windows detection</span>`;
  else if (i.installed) badge = `<span class="badge success" style="background:#143a23;color:#6ee7a7">Detected ✓</span>`;
  else badge = `<span class="badge" style="background:#2a2f3a;color:#94a0b8">Not detected</span>`;
  return `<div class="card" style="display:flex;flex-direction:column;gap:8px">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:22px">${kindIcon}</span>
      <strong style="font-size:16px">${esc(i.name)}</strong></div>
    <p style="color:var(--muted);font-size:13px;flex:1">
      ${i.detected_on === "windows only" ? "Detected on Windows hosts only." : (i.installed ? `Located at <code style="font-size:11px">${esc(i.path || "—")}</code>` : "Not found in standard install locations.")}</p>
    ${badge}
    ${i.scheme ? `<code style="font-size:11px;color:var(--muted)">${esc(i.scheme)}</code>` : ""}
  </div>`;
}
async function refreshIntegrationData() {
  integrationData = null;          // force a fresh fetch
  integrationError = null;
  try {
    const d = await apiGet("/integrations");
    integrationData = Array.isArray(d) ? d : [];
  } catch (e) {
    integrationError = (e && e.message) || "request failed";
    toast(e.message, "error");
  }
  renderIntegrations();
}
window.refreshIntegrationData = refreshIntegrationData;

/* ================================ Settings =============================== */
function renderSettings() {
  const el = $("#page-settings");
  // Read live from Windows (see startup.py) — the installer writes the same
  // value, so this reflects a "run at login" choice made during install too.
  const login = (state.settings && state.settings.run_at_login) || {};
  const loginSupported = !!login.supported;
  const loginOn = !!login.enabled;
  const streamerOn = !!(state.settings && state.settings.streamer_mode);
  const tunnelProvider = "cloudflare";
  const tunnelAutostart = !!(state.settings && state.settings.tunnel_autostart);
  const notifyOn = !!(state.settings && state.settings.notify_enabled);
  const obsCfg = (state.settings && state.settings.obs) || {};
  const obsHasPass = !!obsCfg.has_password;
  const obsExe = obsCfg.exe || "";
  const loginMsg = !loginSupported
    ? "Only available on Windows."
    : (loginOn
        ? "Easy Life starts automatically when you sign in, without showing a window."
        : "Off — your phone can only reach this PC while Easy Life is running.");
  el.innerHTML = `
    <h1 class="page-title">Settings</h1>
    <p class="page-sub">Application settings and paired devices.</p>
    <h2 class="settings-section">Everyday</h2>
    <div class="grid grid-2">
      <div class="card">
        <h3>Paired Devices</h3>
        <p style="color:var(--muted);font-size:13px;margin:6px 0 12px">Devices that can control this PC.</p>
        <div id="dev-list">${state.devices.length === 0 ? `<div style="color:var(--muted)">No paired devices.</div>` : state.devices.map((d) => `
          <div class="device-row">
            <div><div style="font-weight:600">${esc(d.name)} ${d.is_local ? "(local)" : ""}
              ${d.revoked ? `<span class="badge failed" style="margin-left:6px">Revoked</span>` : ""}</div>
              <div style="color:var(--muted);font-size:12px">Last seen: ${esc(fmtTime(d.last_seen))}</div></div>
            ${(d.is_local || d.revoked) ? "" : `<button class="btn danger sm" onclick="revokeDevice('${d.id}')">Revoke</button>`}
          </div>`).join("")}</div>
        <div style="margin-top:14px;display:flex;gap:8px">
          <button class="btn" onclick="newPairingFromSettings()">+ Pair New Device</button>
          <button class="btn danger" onclick="revokeAll()">Revoke All</button>
        </div>
      </div>
      <div class="card">
        <h3>Network</h3>
        <div style="margin-top:12px">
          <label class="field">This PC's address (for phone remote)
            <input type="text" id="net-addr" placeholder="Your local IP address" value="">
          </label>
        </div>
        <p style="color:var(--muted);font-size:12px;margin-top:10px">Your phone connects to this PC over your local Wi-Fi. The API listens on
        <strong>${esc(location.host)}</strong>. Don't forward this port on your router —
        use the remote-access card below instead, which doesn't expose your PC.</p>
      </div>
      <div class="card">
        <h3>This app</h3>
        <p style="color:var(--muted);font-size:13px;margin:6px 0 14px">
          Easy Life keeps running in the background so your phone can reach this PC.
          Closing this window doesn't stop it — use Quit below for that.
        </p>
        <label style="display:flex;align-items:center;gap:11px;cursor:${loginSupported ? "pointer" : "default"}">
          <input type="checkbox" id="set-autostart" ${loginOn ? "checked" : ""}
                 ${loginSupported ? "" : "disabled"} onchange="toggleRunAtLogin(this.checked)">
          <span>Start automatically when I sign in to Windows</span>
        </label>
        <div id="autostart-msg" style="margin-top:10px;font-size:12.5px;color:var(--muted)">${loginMsg}</div>
        <button class="btn danger" style="margin-top:16px" onclick="quitApp()">Quit Easy Life</button>
      </div>
      <div class="card">
        <h3>Streamer Mode</h3>
        <p style="color:var(--muted);font-size:13px;margin:6px 0 14px">
          Adds an <strong>OBS</strong> tab with live status, one-tap stream controls and
          your scenes — available from your phone as well, so you can rescue a stream
          while you're out.
        </p>
        <label style="display:flex;align-items:center;gap:11px;cursor:pointer">
          <input type="checkbox" id="set-streamer" ${streamerOn ? "checked" : ""}
                 onchange="toggleStreamerMode(this.checked)">
          <span>Show the OBS tab</span>
        </label>
        <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border)">
          <p style="color:var(--muted);font-size:12.5px;margin-bottom:10px">
            Easy Life talks to OBS through its WebSocket. In OBS: <strong>Tools → WebSocket
            Server Settings</strong>, tick <em>Enable WebSocket server</em>, and copy the
            password here.
          </p>
          <label class="field">OBS password
            <input type="password" id="obs-pass" value="" placeholder="${obsHasPass ? "saved — type to change" : "from OBS WebSocket settings"}">
          </label>
          <label class="field" style="margin-top:10px">OBS location (only if it can't be found)
            <input type="text" id="obs-exe" value="${esc(obsExe)}" placeholder="C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe">
          </label>
          <button class="btn" style="margin-top:12px" onclick="saveOBSSettings()">Save OBS settings</button>
          <div id="obs-set-msg" style="margin-top:8px;font-size:12.5px;color:var(--muted)"></div>
        </div>
      </div>
      <div class="card">
        <h3>Notifications</h3>
        <p style="color:var(--muted);font-size:13px;margin:6px 0 14px">
          Get a push on your phone when OBS's stream drops, so you know to rescue it
          even when you're not looking. Works on an installed home-screen app over HTTPS.
        </p>
        <label style="display:flex;align-items:center;gap:11px;cursor:pointer">
          <input type="checkbox" id="set-notify" ${notifyOn ? "checked" : ""} onchange="toggleNotify(this.checked)">
          <span>Notify me when the stream drops</span>
        </label>
        <div style="margin-top:14px">
          <button class="btn" onclick="testNotify()">Send a test notification</button>
          <div id="notify-msg" style="margin-top:8px;font-size:12.5px;color:var(--muted)"></div>
        </div>
      </div>
      <div class="card">
        <h3>Use your phone from anywhere</h3>
        <p style="color:var(--muted);font-size:13px;margin:6px 0 14px">
          Publishes this PC on a public address so your phone can reach it outside
          your home Wi-Fi. Nothing to install on the phone.
        </p>
        <p style="color:var(--muted);font-size:13px">Uses <strong>Cloudflare</strong> — a public address your phone opens in Safari. Nothing to install on the phone.</p>
        <label style="display:flex;align-items:center;gap:11px;margin-top:12px;cursor:pointer">
          <input type="checkbox" id="tunnel-autostart" ${tunnelAutostart ? "checked" : ""} onchange="setTunnelAutostart(this.checked)">
          <span>Start automatically when Easy Life opens</span>
        </label>
        <div id="tunnel-box" style="margin-top:12px"><div style="color:var(--muted);font-size:13px">Checking…</div></div>
        <p style="color:var(--muted);font-size:12px;margin-top:14px">
          ⚠ This makes Easy Life reachable from the internet. Anyone who learns the
          address can reach the sign-in page, so use a strong password.
          The address changes every time you turn it on — if your PC restarts, turn remote access back on to get a fresh link.
        </p>
      </div>
      <div class="card">
        <h3>Data</h3>
        <div style="display:flex;flex-direction:column;gap:10px;margin-top:12px">
          <button class="btn" onclick="exportAll()">Export All Plays (JSON)</button>
          <button class="btn" onclick="importModal()">Import Plays</button>
          <button class="btn danger" onclick="clearHistory()">Clear Activity History</button>
        </div>
      </div>
      <div class="card">
        <h3>Wake-on-LAN</h3>
        <p style="color:var(--muted);font-size:13px;margin-top:6px">Send a Magic Packet to wake a PC on your network. The target PC must have Wake-on-LAN enabled in its NIC / BIOS.</p>
        <div style="display:flex;gap:8px;margin-top:12px">
          <label class="field" style="flex:2">MAC Address<input type="text" id="wol-mac" placeholder="AA:BB:CC:DD:EE:FF"></label>
          <button class="btn" style="margin-top:20px" onclick="wolWake()">Wake</button>
        </div>
      </div>
      <div class="card">
        <h3>Voice Control</h3>
        <p style="color:var(--muted);font-size:13px;margin-top:6px">Hands-free control. Try phrases like:
        <code style="display:block;background:var(--bg-3);padding:10px;border-radius:8px;margin-top:8px;font-size:12px">
        “start gaming” · “stop streaming” · “lock the pc” · “put my pc to sleep”
        </code></p>
        <div style="display:flex;gap:8px;margin-top:12px">
          <label class="field" style="flex:2"><input type="text" id="voice-phrase" placeholder="Type a voice command to test…"></label>
          <button class="btn" style="margin-top:20px" onclick="voiceTest()">Run</button>
        </div>
        <div id="voice-result" style="margin-top:8px;font-size:13px"></div>
      </div>
      <div class="card">
        <h3>Account</h3>
        <p style="color:var(--muted);font-size:13px;margin-top:6px">
          Signed in${state.username ? " as <strong>" + esc(state.username) + "</strong>" : ""}.
        </p>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button class="btn" onclick="startTour()">Show tutorial</button>
          <button class="btn danger" onclick="signOut()">Sign out</button>
        </div>
      </div>
      <div class="card">
        <h3>About</h3>
        <p style="color:var(--muted);margin-top:12px;font-size:14px">Easy Life — Windows automation, locally controlled.</p>
        <p style="color:var(--muted);font-size:13px;margin-top:6px">Version 0.1.0 · No cloud required.</p>
      </div>
      <div class="card">
        <h3>Spotify</h3>
        <p style="color:var(--muted);font-size:13px;margin-top:6px">
          Connect Spotify for <strong>exact album art</strong>, real progress and full playback control.
          ${state.spotify && state.spotify.connected
            ? `<span class="badge success" style="margin-left:6px">Connected${state.spotify.user ? " as " + esc(state.spotify.user) : ""}</span>`
            : `<span class="badge" style="margin-left:6px">Not connected</span>`}
        </p>
        ${state.spotify && state.spotify.connected
          ? `<div style="display:flex;gap:8px;margin-top:12px">
               <button class="btn" onclick="spotifyConnect()">Reconnect</button>
               <button class="btn danger" onclick="spotifyDisconnect()">Disconnect</button>
             </div>`
          : `<div style="margin-top:12px">
               <label class="field">Client ID<input type="text" id="sp-id" value="${esc((state.spotify && state.spotify.client_id) || "")}" placeholder="from developer.spotify.com/dashboard"></label>
               <label class="field" style="margin-top:10px">Client Secret<input type="password" id="sp-secret" placeholder="stored locally, never sent anywhere else"></label>
               <label class="field" style="margin-top:10px">Redirect URI (must match your Spotify app)<input type="text" id="sp-redirect" value="${esc((state.spotify && state.spotify.redirect_uri) || "http://127.0.0.1:8765/callback")}"></label>
               <div style="display:flex;gap:8px;margin-top:12px">
                 <button class="btn" onclick="saveSpotifyConfig()">Save</button>
                 <button class="btn primary" onclick="spotifyConnect()">Connect Spotify</button>
               </div>
               <div style="color:var(--muted);font-size:12px;margin-top:12px;line-height:1.6">
                 <strong>Setup (one time):</strong><br>
                 1. Create a free app at <code>developer.spotify.com/dashboard</code><br>
                 2. Add redirect URI <code>${esc((state.spotify && state.spotify.redirect_uri) || "http://127.0.0.1:8765/callback")}</code><br>
                 3. Paste the Client ID + Secret above, click Save, then Connect.<br>
                 Your credentials are stored only on this PC.
               </div>
             </div>`}
      </div>
      <div class="card">
        <h3>Updates</h3>
        <p style="color:var(--muted);font-size:13px;margin-top:6px">Easy Life checks for updates on startup. Set a manifest URL to enable the in-app Update button.</p>
        <label class="field" style="margin-top:10px">Update manifest URL
          <input type="text" id="upd-url" placeholder="https://example.com/update.json" value="${esc(state.settings ? (state.settings.update_url || "") : "")}">
        </label>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn sm" onclick="saveSettings()">Save Update URL</button>
          <button class="btn sm" onclick="checkForUpdate()">Check Now</button>
        </div>
        <div id="update-status-line" style="margin-top:8px;font-size:13px;color:var(--muted)"></div>
      </div>
      <div class="card" style="max-height:320px;display:flex;flex-direction:column">
        <h3>Backups</h3>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn green" onclick="createBackup()">+ Create Backup</button>
          <button class="btn" onclick="refreshBackups()">↻ Refresh</button>
        </div>
        <div id="backup-list" style="margin-top:12px;overflow-y:auto;flex:1">${renderBackups()}</div>
      </div>
      ${syncCard()}
    </div>`;
  // Split the developer-facing cards behind an "Advanced" disclosure so a new
  // user sees the everyday stuff first. The cards are interleaved in the
  // template, so tag them by heading text, then move them into a <details>.
  const ADVANCED = ["Network", "This app", "Data", "Wake-on-LAN", "Voice Control",
                    "About", "Spotify", "Updates", "Backups", "Sync across PCs"];
  el.querySelectorAll(".card").forEach((card) => {
    const h = card.querySelector("h3");
    if (h && ADVANCED.some((t) => h.textContent.startsWith(t))) card.classList.add("adv");
  });
  const adv = document.createElement("details");
  adv.className = "settings-advanced";
  adv.innerHTML = "<summary>Advanced</summary>";
  el.querySelectorAll(".card.adv").forEach((c) => adv.appendChild(c));
  el.appendChild(adv);

  // show local IP suggestions
  fetchIPHint(); renderBackups(); refreshSyncStatus(); renderTunnelBox();
}

/* ============================ optional sync ============================== */
let syncState = null;
function syncCard() {
  const s = syncState || {};
  const connected = !!s.configured;
  const when = s.last_sync ? fmtTime(s.last_sync) : "never";
  // "adv" keeps this card behind the Advanced disclosure even when
  // refreshSyncStatus() re-renders it after renderSettings() has already
  // moved the advanced cards into the <details>.
  return `<div class="card adv">
    <h3>Sync across PCs <span class="badge" style="background:#2a2f3a;color:#94a0b8">optional</span></h3>
    <p style="color:var(--muted);font-size:13px;margin-top:6px">
      Keep your Plays on more than one computer. This is <strong>off until you connect</strong> —
      nothing leaves this PC otherwise. You run the sync server yourself; it is not a cloud service.
      See <code>SYNC.md</code> for the one-line command to start it.
    </p>
    ${connected ? `
      <div style="margin-top:12px;font-size:13px">
        <div><strong>${esc(s.username || "")}</strong> at <code>${esc(s.url || "")}</code></div>
        <div style="color:var(--muted);font-size:12px;margin-top:4px">Last sync: ${esc(when)} · revision ${esc(String(s.revision || 0))}</div>
        ${s.last_error ? `<div style="color:var(--red);font-size:12px;margin-top:4px">${esc(s.last_error)}</div>` : ""}
      </div>
      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <button class="btn primary sm" onclick="syncPush()">⬆ Push this PC</button>
        <button class="btn sm" onclick="syncPull('merge')">⬇ Pull (merge)</button>
        <button class="btn sm" onclick="syncPull('replace')">⬇ Pull (replace)</button>
        <button class="btn sm" onclick="syncDisconnect()">Disconnect</button>
      </div>
      <div style="color:var(--muted);font-size:12px;margin-top:10px">
        <strong>Push</strong> uploads this PC's rituals. <strong>Pull</strong> downloads them.
        Merge keeps both; replace overwrites this PC.
      </div>` : `
      <label class="field" style="margin-top:10px">Sync server address
        <input type="text" id="sy-url" placeholder="http://192.168.1.20:8788" value="${esc(s.url || "")}">
      </label>
      <label class="field" style="margin-top:10px">Username
        <input type="text" id="sy-user" value="${esc(s.username || "")}">
      </label>
      <label class="field" style="margin-top:10px">Password
        <input type="password" id="sy-pass" placeholder="at least 8 characters on the server">
      </label>
      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <button class="btn primary sm" onclick="syncConnect(false)">Connect</button>
        <button class="btn sm" onclick="syncConnect(true)">Create account</button>
      </div>`}
  </div>`;
}
async function refreshSyncStatus() {
  try {
    syncState = await apiGet("/sync/status");
    const host = $("#page-settings");
    // Re-render just the sync card so the poll cannot loop over the whole page.
    if (host && currentRoute === "settings") {
      const cards = host.querySelectorAll(".card");
      const last = cards[cards.length - 1];
      if (last && last.textContent.includes("Sync across PCs")) last.outerHTML = syncCard();
    }
  } catch (_) {}
}
async function syncConnect(register) {
  const url = (($("#sy-url") || {}).value || "").trim();
  const username = (($("#sy-user") || {}).value || "").trim();
  const password = ($("#sy-pass") || {}).value || "";
  if (!url) return toast("Enter the sync server address", "error");
  if (!username || !password) return toast("Enter a username and password", "error");
  try {
    syncState = await apiPost("/sync/connect", { url, username, password, register });
    toast(register ? "Sync account created" : "Connected", "success");
    renderSettings();
  } catch (e) { toast(e.message, "error"); }
}
async function syncPush() {
  try {
    const r = await apiPost("/sync/push", {});
    toast(`Pushed ${r.pushed.rituals} ritual(s)`, "success");
    await refreshSyncStatus();
  } catch (e) {
    // 409 -> the server has newer work. Offer to overwrite rather than silently
    // discarding whatever the other machine wrote.
    if (/newer data/i.test(e.message || "")) {
      const ok = await confirmModal("Server has newer data",
        "Another computer synced after this one. Overwrite the server with this PC's rituals?",
        "Overwrite", "danger");
      if (!ok) return;
      try {
        const r = await apiPost("/sync/push", { force: true });
        toast(`Overwrote the server with ${r.pushed.rituals} ritual(s)`, "success");
        await refreshSyncStatus();
      } catch (e2) { toast(e2.message, "error"); }
      return;
    }
    toast(e.message, "error");
  }
}
async function syncPull(mode) {
  if (mode === "replace") {
    const ok = await confirmModal("Replace this PC's rituals?",
      "Your current Plays on this PC will be removed and replaced with the server's copy.",
      "Replace", "danger");
    if (!ok) return;
  }
  try {
    const r = await apiPost("/sync/pull", { mode });
    toast(`Pulled: ${r.applied.rituals} ritual(s), ${r.applied.deck} button(s)`, "success");
    await refresh();
    render();
  } catch (e) { toast(e.message, "error"); }
}
async function syncDisconnect() {
  const ok = await confirmModal("Disconnect sync",
    "Stop syncing on this PC? Your Plays stay here, and the server copy is left alone.", "Disconnect");
  if (!ok) return;
  try { syncState = await apiPost("/sync/disconnect", {}); toast("Disconnected"); renderSettings(); }
  catch (e) { toast(e.message, "error"); }
}
window.syncConnect = syncConnect;
window.syncPush = syncPush;
window.syncPull = syncPull;
window.syncDisconnect = syncDisconnect;
window.refreshSyncStatus = refreshSyncStatus;
function renderBackups() {
  const el = $("#backup-list"); if (!el) return;
  if (!state.backups || !state.backups.length) {
    el.innerHTML = `<div style="color:var(--muted);font-size:13px">No backups yet.</div>`;
    return;
  }
  el.innerHTML = state.backups.map((b) => `
    <div class="device-row" style="align-items:center">
      <div><div style="font-weight:600;font-size:13px">${esc(b.name)}</div>
        <div style="color:var(--muted);font-size:12px">${esc(b.created)} · ${esc(b.files || "?")} files</div></div>
      <div style="display:flex;gap:6px">
        <button class="btn sm" data-restore="${esc(b.name)}">Restore</button>
        <button class="icon-btn" style="color:var(--red)" aria-label="Delete backup" data-del="${esc(b.name)}">🗑</button>
      </div>
    </div>`).join("");
}

// Backup names are data, not code: pass them through a dataset and read them in
// a handler. Interpolating an HTML-escaped name into an inline onclick was wrong
// because the HTML parser decodes &#39; back to ' before the JS runs, so a name
// containing an apostrophe produced a syntax error and a dead button.
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.dataset) return;
  if (t.dataset.restore !== undefined) restoreBackup(t.dataset.restore);
  else if (t.dataset.del !== undefined) deleteBackup(t.dataset.del);
});

// Click-to-seek on the media progress bar. The bar already looked draggable, so
// make the affordance real. Seek only works through Spotify (the OS media keys
// have no seek); the backend reports an error otherwise rather than pretending.
document.addEventListener("click", (e) => {
  const bar = e.target && e.target.closest ? e.target.closest(".am-bar") : null;
  if (!bar) return;
  const seconds = Number(bar.dataset.seekSeconds || 0);
  if (!seconds) return;
  const rect = bar.getBoundingClientRect();
  if (!rect.width) return;
  const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
  // The bar works in seconds; Spotify's seek API takes milliseconds.
  mediaControl("seek", Math.round(frac * seconds * 1000));
});
async function refreshBackups() {
  // Same boundary rule as every other list: a 200 whose body is not an array of
  // objects (an HTML captive-portal page, `{}`, junk entries) used to land in
  // state and make the Settings page throw `state.backups.map is not a function`.
  try {
    const d = await apiGet("/backups");
    state.backups = objList(d);
  } catch (_) { state.backups = []; }
  renderBackups();
}
async function createBackup() {
  try { const r = await apiPost("/backups", { label: "manual" }); toast(`Backup created (${r.files} files)`, "success"); await refreshBackups(); }
  catch (e) { toast(e.message, "error"); }
}
async function restoreBackup(name) {
  const ok = await confirmModal("Restore Backup", `Restore "${name}"? This overwrites your current Plays and settings.`, "Restore");
  if (!ok) return;
  try { await apiPost(`/backups/${encodeURIComponent(name)}/restore`); toast("Backup restored — reloading…", "success"); setTimeout(() => location.reload(), 800); }
  catch (e) { toast(e.message, "error"); }
}
async function deleteBackup(name) {
  const ok = await confirmModal("Delete Backup", `Delete backup "${name}"?`, "Delete");
  if (!ok) return;
  try { await apiDel(`/backups/${encodeURIComponent(name)}`); toast("Deleted"); await refreshBackups(); }
  catch (e) { toast(e.message, "error"); }
}
window.createBackup = createBackup; window.refreshBackups = refreshBackups;
window.restoreBackup = restoreBackup; window.deleteBackup = deleteBackup;
async function wolWake() {
  const mac = (($("#wol-mac") || {}).value || "").trim();
  if (!mac) return toast("Enter a MAC address", "error");
  try { const r = await apiPost("/wol/wake", { mac }); toast(`Magic packet sent to ${mac}`, "success"); }
  catch (e) { toast(e.message, "error"); }
}
window.wolWake = wolWake;
async function voiceTest() {
  const phrase = (($("#voice-phrase") || {}).value || "").trim();
  if (!phrase) return toast("Enter a phrase", "error");
  const out = $("#voice-result");
  try {
    let r = await apiPost("/voice", { phrase });
    if (r.intent && r.needs_confirmation) {
      const ok = await confirmModal(r.intent.toUpperCase(), `Voice command "${phrase}" will ${r.intent} this PC. Confirm?`, r.intent.toUpperCase(), "danger");
      if (!ok) { out.innerHTML = "<span style=color:var(--amber)>Cancelled.</span>"; return; }
      r = await apiPost("/voice", { phrase, confirm: true });
    }
    out.innerHTML = r.intent
      ? `<span style="color:${r.executed ? "var(--green)" : "var(--amber)"}">${esc(r.intent)}${r.ritual_name ? " → " + esc(r.ritual_name) : ""} · ${r.executed ? "executed" : (r.message || "pending")}</span>`
      : `<span style="color:var(--muted)">No matching command.</span>`;
  } catch (e) { out.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}
window.voiceTest = voiceTest;
async function fetchIPHint() {
  try {
    const peers = await apiGet("/status");
    const addr = location.hostname === "" ? (location.protocol + "//" + location.host) : location.host;
    const inp = $("#net-addr");
    if (inp) inp.value = addr;
  } catch (_) {}
}
async function newPairingFromSettings() {
  try { await apiPost("/pair/start"); toast("Pairing code generated — see Phone Remote tab"); navigate("remote"); startPairing(); }
  catch (e) { toast(e.message, "error"); }
}
async function revokeDevice(id) {
  try { await apiPost(`/devices/${id}/revoke`); await refreshDevices(); render(); toast("Device revoked"); }
  catch (e) { toast(e.message, "error"); }
}
async function revokeAll() {
  const ok = await confirmModal("Revoke All", "Revoke access for all paired devices?", "Revoke All");
  if (!ok) return;
  try { await apiPost("/devices/revoke-all"); await refreshDevices(); render(); toast("All devices revoked"); }
  catch (e) { toast(e.message, "error"); }
}

window.revokeDevice = revokeDevice;
window.revokeAll = revokeAll;
window.newPairingFromSettings = newPairingFromSettings;

/* ================================ init =================================== */
/* Returns the parsed status object, or null when the answer is UNUSABLE — a
 * transport failure, an HTML/plain-text body (captive portal, proxy error page,
 * `{}`), or a shape with no explicit verdict. Callers must treat null as
 * "unknown", not as "signed out": a status check that failed to parse is not
 * evidence that the stored session is invalid. */
async function fetchAuthStatus() {
  let d = null;
  try { d = await apiGet("/auth/status"); } catch (_) { return null; }
  if (!isPlainObject(d)) return null;
  if (d.authenticated === true || d.authenticated === false || d.needs_setup === true) return d;
  return null;
}

// Bounded retry for an "unknown" status: a few attempts with growing delays,
// then stop. Never an infinite loop, and never a sign-out.
const AUTH_RETRY_DELAYS_MS = [2000, 4000, 8000, 15000];
let authRetryIndex = 0;

async function init() {
  // Gate the app behind the local login — but only on the PC itself. A phone
  // goes to the pairing screen (pairing is how it earns access).
  const isPC = isLoopbackSync();
  const st = await fetchAuthStatus();

  if (st) {
    if (st.authenticated) {
      state.username = st.username;
      await initApp();
      // First time for this account: show the guided tour.
      if (!st.onboarded) setTimeout(startTour, 400);
      return;
    }
    // Only an EXPLICIT verdict from the server moves the user to the sign-in
    // screen. Anything ambiguous keeps the session we already have.
    if (isPC && st.needs_setup) { renderAuthScreen("setup"); return; }
    if (isPC && st.authenticated === false) { renderAuthScreen("login"); return; }
    return initApp();
  }

  // Unknown status. With a stored token, DO NOT fall back to sign-in — that
  // signed the user out (recoverable only by reload) whenever a proxy returned
  // a body we could not parse, while the token was still perfectly valid. Keep
  // the session, start the app, and re-check the status in the background.
  if (token || !isPC) {
    await initApp();
    if (token) authStatusRetry(isPC);
    return;
  }
  renderAuthScreen("login");
}

function authStatusRetry(isPC) {
  const delay = AUTH_RETRY_DELAYS_MS[authRetryIndex];
  if (delay === undefined) return;           // bounded: give up, stay signed in
  authRetryIndex++;
  setTimeout(async () => {
    const st = await fetchAuthStatus();
    if (!st) { authStatusRetry(isPC); return; }
    authRetryIndex = 0;
    if (st.authenticated) return;            // already running; nothing to do
    // The server has now explicitly denied the stored session (or wants first-run
    // setup): that is a real verdict, so show the sign-in screen.
    if (isPC && (st.needs_setup || st.authenticated === false)) {
      renderAuthScreen(st.needs_setup ? "setup" : "login");
    }
  }, delay);
}

async function initApp() {
  const onPhone = !isLoopbackSync();
  // A phone's whole purpose is the remote control surface, so land it there —
  // whether or not it is already paired. Previously only an UNPAIRED phone was
  // redirected, so a paired phone opened on the Dashboard and the controls were
  // hidden behind a tab tap.
  if (onPhone && !location.hash) {
    location.hash = "#/remote";
  }
  renderNav();
  startSSE();
  hashRoute();
  await refresh();
  render();
  setInterval(refreshPc, 10000);
  initKeyboardHandling();
  autoPairFromQR();
  checkForUpdate();
  initPush();
}

/* ---------- login / first-run setup screen ---------- */
function renderAuthScreen(mode) {
  const isSetup = mode === "setup";
  document.getElementById("app").innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="auth-logo" aria-hidden="true"></div>
        <h1 class="auth-title">${isSetup ? "Create your account" : "Welcome back"}</h1>
        <p class="auth-sub">${isSetup
          ? "Easy Life is private to this PC. Set a username and password to protect your plays."
          : "Sign in to see your plays."}</p>
        <label class="field">Username
          <input type="text" id="au-user" autocomplete="username" autocapitalize="none" autofocus>
        </label>
        <label class="field" style="margin-top:12px">Password
          <input type="password" id="au-pass" autocomplete="${isSetup ? "new-password" : "current-password"}">
        </label>
        ${isSetup ? `<label class="field" style="margin-top:12px">Confirm password
          <input type="password" id="au-pass2" autocomplete="new-password">
        </label>` : ""}
        <button class="btn primary auth-btn" id="au-go">${isSetup ? "Create account" : "Sign in"}</button>
        <div class="auth-msg" id="au-msg"></div>
        <div class="auth-note">Stored only on this PC · your password is hashed, never stored in plain text</div>
      </div>
    </div>`;

  const submit = async () => {
    const u = ($("#au-user").value || "").trim();
    const p = $("#au-pass").value || "";
    const msg = $("#au-msg");
    if (!u) { msg.innerHTML = `<span style="color:var(--red)">Enter a username</span>`; return; }
    if (isSetup) {
      const p2 = $("#au-pass2").value || "";
      if (p !== p2) { msg.innerHTML = `<span style="color:var(--red)">Passwords don't match</span>`; return; }
      if (p.length < 4) { msg.innerHTML = `<span style="color:var(--red)">Password must be at least 4 characters</span>`; return; }
    }
    const btn = $("#au-go"); btn.disabled = true; btn.textContent = isSetup ? "Creating…" : "Signing in…";
    try {
      const res = await apiPost(isSetup ? "/auth/setup" : "/auth/login", { username: u, password: p });
      if (res && res.token) {
        setToken(res.token);
        toast(isSetup ? "Account created" : "Signed in", "success");
        await init();
      } else {
        throw new Error("no token returned");
      }
    } catch (e) {
      msg.innerHTML = `<span style="color:var(--red)">${esc(e.message || "Failed")}</span>`;
      btn.disabled = false; btn.textContent = isSetup ? "Create account" : "Sign in";
    }
  };
  $("#au-go").addEventListener("click", submit);
  ["au-user", "au-pass", "au-pass2"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  });
}

/* ============================ onboarding tour ============================ */
const TOUR_STEPS = [
  { icon: "👋", title: "Welcome to Easy Life",
    body: "Easy Life runs multi-step automations on your PC — called <strong>Plays</strong>. This quick tour shows you where everything is. You can skip it anytime." },
  { icon: "🏠", title: "Dashboard",
    body: "Your home screen. See PC status, jump to your Plays, and watch a running Play's progress live." },
  { icon: "▦", title: "Plays",
    body: "Create, edit, duplicate and run your Plays here. A Play is an ordered list of steps — open Discord, launch a game, open a website, wait, and so on." },
  { icon: "✎", title: "Builder",
    body: "Where you build a Play. Type what you want in the <strong>Describe it</strong> box and it builds the steps for you — or add them by hand, drag to reorder, and set delays and timeouts." },
  { icon: "▩", title: "Deck",
    body: "Your quick-fire control surface. Add buttons for games, apps and sites — icons are filled in automatically. There's also a music player with album art." },
  { icon: "◉", title: "Phone Remote",
    body: "Control this PC from your iPhone on the same Wi-Fi. Generate a pairing code here, scan it on your phone, and add it to your Home Screen." },
  { icon: "⚙", title: "Settings",
    body: "Where you turn on the extras. <strong>Streamer Mode</strong> adds an OBS tab to control your stream from your phone. <strong>Notifications</strong> pings your phone when the stream drops. <strong>Use your phone from anywhere</strong> reaches this PC outside your home. Everything stays on this PC." },
  { icon: "🎬", title: "Streaming",
    body: "If you stream, turn on <strong>Streamer Mode</strong> in Settings. It adds an OBS tab with live status, one-tap stream rescue and your scenes — and it works from your phone, so you can fix a dead stream while you're out. <strong>Notifications</strong> tells your phone when the stream drops." },
  { icon: "🚀", title: "You're all set",
    body: "Create your first play to get started — or explore on your own. You can replay this tour from <strong>Settings → Show tutorial</strong>." },
];

let tourIndex = 0;
function startTour() {
  tourIndex = 0;
  renderTour();
}
function renderTour() {
  const step = TOUR_STEPS[tourIndex];
  const last = tourIndex === TOUR_STEPS.length - 1;
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal tour-card">
        <div class="tour-icon">${step.icon}</div>
        <h2 class="tour-title">${esc(step.title)}</h2>
        <p class="tour-body">${step.body}</p>
        <div class="tour-dots">
          ${TOUR_STEPS.map((_, i) => `<span class="tour-dot ${i === tourIndex ? "on" : ""}"></span>`).join("")}
        </div>
        <div class="tour-actions">
          <button class="btn" id="tour-skip">Skip tour</button>
          <div style="flex:1"></div>
          ${tourIndex > 0 ? `<button class="btn" id="tour-back">Back</button>` : ""}
          ${last
            ? `<button class="btn primary" id="tour-done">Create my first Play</button>`
            : `<button class="btn primary" id="tour-next">Next</button>`}
        </div>
      </div>
    </div>`;
  const close = (then) => {
    $("#modal-root").innerHTML = "";
    apiPost("/auth/onboarded").catch(() => {});
    if (then) then();
  };
  $("#tour-skip").addEventListener("click", () => close());
  const next = $("#tour-next");
  if (next) next.addEventListener("click", () => { tourIndex++; renderTour(); });
  const back = $("#tour-back");
  if (back) back.addEventListener("click", () => { tourIndex--; renderTour(); });
  const done = $("#tour-done");
  if (done) done.addEventListener("click", () => close(() => {
    navigate("rituals");
    setTimeout(() => { if (typeof newRitual === "function") newRitual(); }, 250);
  }));
}
window.startTour = startTour;

async function signOut() {
  const ok = await confirmModal("Sign out", "Sign out of Easy Life?", "Sign out");
  if (!ok) return;
  try { await apiPost("/auth/logout"); } catch (_) {}
  setToken("");
  Store.remove("pcrituals.token");
  location.reload();
}
window.signOut = signOut;

function isLoopbackSync() {
  const h = location.hostname;
  return h === "localhost" || h === "127.0.0.1" || h === "[::1]" || h === "";
}

/* ============================ in-app updater ============================= */
let updateInfo = null;
async function checkForUpdate() {
  const line = $("#update-status-line");
  if (line) line.textContent = "Checking…";
  try {
    const r = await apiGet("/update/status");
    if (r && r.available && r.available.version) {
      updateInfo = r.available;
      if (line) line.textContent = `Update available: v${r.available.version}`;
    } else {
      updateInfo = null;
      if (line) {
        line.textContent = (r && r.configured === false)
          ? "No update source configured yet."
          : "You're on the latest version.";
      }
    }
  } catch (e) {
    updateInfo = null;
    if (line) line.textContent = `Couldn't check: ${e.message || "unknown error"}`;
  }
  renderUpdateBanner();
}
function renderUpdateBanner() {
  const slot = $("#update-banner-slot");
  if (!slot) return;
  if (!updateInfo) { slot.innerHTML = ""; return; }
  slot.innerHTML = `
    <div class="update-banner" role="status">
      <div class="update-msg">
        <span style="font-size:18px">⬆️</span>
        <span><span class="badge-update">Update available</span> — version ${esc(updateInfo.version)} is ready.
        ${updateInfo.notes ? `<span style="color:var(--muted)">${esc(updateInfo.notes)}</span>` : ""}</span>
      </div>
      <div class="update-actions">
        <button class="btn sm" onclick="dismissUpdate()">Later</button>
        <button class="btn primary sm" onclick="applyUpdate()">Update Now</button>
      </div>
    </div>`;
}
function dismissUpdate() {
  updateInfo = null; renderUpdateBanner();
}
async function applyUpdate() {
  try {
    const btn = $("#update-banner-slot .update-actions .primary");
    if (btn) { btn.disabled = true; btn.textContent = "Updating…"; }
    const r = await apiPost("/update/apply");
    toast(`Update ${r.version} applied. Restarting…`, "success");
    updateInfo = null; renderUpdateBanner();
    setTimeout(() => location.reload(), 1200);
  } catch (e) {
    toast(e.message || "Update failed", "error");
    const btn = $("#update-banner-slot .update-actions .primary");
    if (btn) { btn.disabled = false; btn.textContent = "Update Now"; }
  }
}
window.dismissUpdate = dismissUpdate;
window.applyUpdate = applyUpdate;

function autoPairFromQR() {
  const m = location.hash.match(/[?&]code=([A-Za-z0-9]+)/);
  if (!m || token) return;
  const code = m[1];
  // Clear the code from the URL so refresh doesn't re-pair.
  history.replaceState(null, "", location.pathname + location.hash.split("?")[0]);
  apiPost("/pair/complete", { code, device_name: "iPhone" }).then((res) => {
    if (res && res.token) {
      setToken(res.token);
      toast("Paired! Controlling your PC.", "success");
      refresh().then(render);
    }
  }).catch(() => {
    toast("Pairing code expired — generate a new one.", "error");
  });
}
window.autoPairFromQR = autoPairFromQR;
document.addEventListener("DOMContentLoaded", init);