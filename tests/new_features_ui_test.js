/* Frontend test for the two features added for the streaming use case:
 * the "Describe it" natural-language builder (with undo) and the OBS tab.
 * jsdom + mocked fetch, matching the smoke_frontend.js harness. */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const html = `<html><body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body></html>`;

const RITUAL = {
  id: "r1", name: "Old name", description: "", stop_on_error: true,
  actions: [
    { id: "a1", type: "app", name: "Open steam", target: "steam", args: [], params: {}, enabled: true },
  ],
};

const DRAFT = {
  name: "Stream Rescue",
  steps: [
    { id: "d1", type: "app", name: "Open obs", target: "obs64", args: [], params: {}, enabled: true, notes: "" },
    { id: "d2", type: "obs_scene", name: "OBS: switch to MAIN 1", target: "MAIN 1", args: [], params: {}, enabled: true, notes: "" },
    { id: "d3", type: "obs_stream_start", name: "OBS: start streaming", target: "", args: [], params: {}, enabled: true, notes: "" },
  ],
  understood: ["open obs → Open obs", "switch to MAIN 1 scene → OBS: switch to MAIN 1", "start stream → OBS: start streaming"],
  unmatched: [],
  notes: [],
};

const SETTINGS_ON = { update_url: "", streamer_mode: true, obs: { port: 4455, has_password: false, exe: "" } };

const MOCK = {
  "GET api/status": { online: true, platform: "generic", cpu: 12.0, memory: { percent: 40 }, running_ritual: null },
  "GET api/rituals": [RITUAL],
  "GET api/run/current": { running: false },
  "GET api/history?limit=50": [],
  "GET api/devices": [],
  "GET api/pair/state": { active_code: null, expires_in: null },
  "GET api/deck": [],
  "GET api/media/now": {},
  "GET api/settings": { update_url: "", streamer_mode: false, obs: { port: 4455, has_password: false, exe: "" } },
  "POST api/settings": SETTINGS_ON,
  "GET api/update/status": { version: "0.1.0", available: null },
  "GET api/spotify/status": { configured: false, connected: false, redirect_uri: "http://127.0.0.1:8765/callback" },
  "GET api/auth/status": { needs_setup: false, authenticated: true, username: "tester", onboarded: true },
  "GET api/obs/status": { reachable: false, streaming: false, recording: false, reconnecting: false, timecode: "", congestion: 0.0, exe_found: false, error: "OBS is not answering" },
  "GET api/obs/scenes": { scenes: ["MAIN 1", "BRB", "x');alert(1)//"] },
  "POST api/plays/draft": DRAFT,
};

async function main() {
  const watchdog = setTimeout(() => {
    console.log("WATCHDOG TIMEOUT — results so far:", JSON.stringify(results));
    process.exit(2);
  }, 20000);
  const dom = new JSDOM(html, { url: "http://127.0.0.1:8765/", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.scrollTo = () => {};
  window.confirm = () => true;

  window.fetch = async (url, opts = {}) => {
    const method = (opts.method || "GET").toUpperCase();
    const key = `${method} ${String(url).replace(/^\//, "")}`;
    let body = MOCK[key];
    if (body === undefined && method === "GET" && String(url).includes("/api/rituals/")) {
      body = RITUAL;
    }
    // input-aware draft: "wait 5 seconds" is a single delay step
    if (key === "POST api/plays/draft") {
      const text = JSON.parse(opts.body || "{}").text || "";
      if (/wait|delay/.test(text)) {
        body = { name: "", steps: [{ id: "w1", type: "delay", name: "Wait 5s", target: "", args: [], params: { seconds: 5 }, enabled: true, notes: "" }], understood: ["wait 5 seconds → Wait 5s"], unmatched: [], notes: [] };
      }
    }
    if (body === undefined) body = {};
    const payload = JSON.stringify(body);
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body, text: async () => payload };
  };

  let jsErrors = [];
  window.addEventListener("error", (e) => jsErrors.push(e.message));
  class ESStub { constructor(){} close(){} }
  window.EventSource = ESStub;

  const appjs = fs.readFileSync(path.join(__dirname, "../pcrituals/web/app.js"), "utf8");
  window.eval(appjs);
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  await new Promise((r) => setTimeout(r, 400));

  const doc = dom.window.document;
  const results = {};
  const fail = (k, why) => {
    clearTimeout(watchdog);
    results[k] = "FAIL: " + why;
    console.log(JSON.stringify(results, null, 2));
    console.log("NEW FEATURES UI: FAIL");
    process.exit(1);   // the app's setInterval keeps Node alive otherwise
  };
  const ok = (k, v) => { results[k] = "ok" + (v !== undefined ? " (" + v + ")" : ""); };
  const rows = () => doc.querySelectorAll(".action-row").length;
  const nameVal = () => (doc.querySelector("#b-name") || {}).value;

  // ---- open a ritual in the builder via the real hash route ----
  window.location.hash = "#/builder/r1";
  await new Promise((r) => setTimeout(r, 300));

  const box = doc.querySelector("#nl-box");
  if (!box) return fail("describe-box", "no #nl-box in the builder");
  ok("describe-box", "present");
  const buildBtn = [...doc.querySelectorAll("button")].find((b) => b.textContent.trim() === "Build it");
  if (!buildBtn) return fail("build-button", "no 'Build it' button");
  ok("build-button", "present");

  // ---- Describe it: type + build ----
  box.value = "open obs then switch to MAIN 1 scene then start stream, name it Stream Rescue";
  window.buildFromText();
  await new Promise((r) => setTimeout(r, 300));

  if (nameVal() !== "Stream Rescue") return fail("describe-name", "name not applied: " + nameVal());
  ok("describe-name", nameVal());
  const resultText = (doc.querySelector("#nl-result") || {}).textContent || "";
  if (!resultText.includes("Built 3 step(s)")) return fail("describe-result", "no 'Built 3 step(s)'");
  ok("describe-result", "3 steps");
  if (rows() !== 3) return fail("describe-rows", "expected 3 action rows, got " + rows());
  ok("describe-rows", "3 action rows");

  const undoBtn = [...doc.querySelectorAll("button")].find((b) => b.textContent.trim() === "Undo");
  if (!undoBtn) return fail("undo-button", "no Undo button after replacing steps");
  ok("undo-button", "present");

  // ---- Undo restores the previous steps (inline onclick doesn't fire in
  // jsdom, so call the exposed function — the button's existence is checked
  // above, and its handler is the same one) ----
  window.undoDescribe();
  await new Promise((r) => setTimeout(r, 200));
  if (nameVal() !== "Old name") return fail("undo-name", "name not reverted: " + nameVal());
  if (rows() !== 1) return fail("undo-rows", "expected 1 action row after undo, got " + rows());
  ok("undo-restores", "name reverted, 1 step back");

  // ---- Add-to-end instead (re-query: render() rebuilt the DOM) ----
  const box2 = doc.querySelector("#nl-box");
  if (!box2) return fail("add-box", "no #nl-box after undo render");
  box2.value = "wait 5 seconds";
  window.buildFromText();
  await new Promise((r) => setTimeout(r, 300));
  const addBtn = [...doc.querySelectorAll("button")].find((b) => b.textContent.trim() === "Add to the end instead");
  if (!addBtn) return fail("add-button", "no 'Add to the end instead' button");
  window.addDescribeInstead();
  await new Promise((r) => setTimeout(r, 200));
  if (rows() !== 2) return fail("add-rows", "expected 2 action rows after add, got " + rows());
  ok("add-appends", "steam + delay = 2 rows");

  // ---- OBS tab appears only when Streamer Mode is on ----
  const navBefore = [...doc.querySelectorAll(".nav-item")].map((n) => n.dataset.route);
  if (navBefore.includes("obs")) return fail("obs-hidden", "OBS tab visible with streamer_mode off");
  ok("obs-hidden", "no OBS tab when off");

  window.toggleStreamerMode(true);
  await new Promise((r) => setTimeout(r, 300));
  const navAfter = [...doc.querySelectorAll(".nav-item")].map((n) => n.dataset.route);
  if (!navAfter.includes("obs")) return fail("obs-shown", "OBS tab missing after enabling streamer mode");
  ok("obs-shown", "OBS tab appears when on");

  const navObs = [...doc.querySelectorAll(".nav-item")].find((n) => n.dataset.route === "obs");
  navObs.click();
  await new Promise((r) => setTimeout(r, 300));
  const obsStatus = doc.querySelector("#obs-tab-status");
  if (!obsStatus) return fail("obs-tab", "no #obs-tab-status after opening the OBS tab");
  ok("obs-tab", "status card rendered");
  const obsText = obsStatus.textContent || "";
  if (!/OBS isn't responding|Fix my stream/.test(obsText)) return fail("obs-content", "unexpected OBS tab content: " + obsText.slice(0, 80));
  ok("obs-content", "shows not-responding + Fix my stream");

  // XSS regression: a scene name with quotes must NOT end up in an inline
  // onclick (esc() turns ' into &#39;, which the HTML parser decodes back to '
  // inside the attribute — the old inline onclick was exploitable).
  const obsHtml = (doc.querySelector("#page-obs") || {}).innerHTML || "";
  if (/onclick="switchScene/.test(obsHtml)) return fail("xss", "scene name leaked into an inline onclick");
  ok("xss", "scene buttons use data-scene, no inline onclick");
  const sceneBtns = [...doc.querySelectorAll("[data-scene]")];
  const malicious = sceneBtns.find((b) => b.getAttribute("data-scene") === "x');alert(1)//");
  if (!malicious) return fail("xss-data", "malicious scene not rendered as a data-scene button");
  ok("xss-data", "malicious scene rendered safely");

  if (jsErrors.length) return fail("js-errors", jsErrors.join(" | "));
  ok("js-errors", "none");

  clearTimeout(watchdog);
  const pass = Object.values(results).every((v) => !String(v).startsWith("FAIL"));
  console.log(JSON.stringify(results, null, 2));
  console.log(pass ? "NEW FEATURES UI: PASS" : "NEW FEATURES UI: FAIL");
  process.exit(pass ? 0 : 1);
}
main().catch((e) => { console.error("FATAL", e); process.exit(1); });
