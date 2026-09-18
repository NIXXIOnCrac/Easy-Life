/* Smoke-test the app.js frontend in jsdom to catch runtime/reference errors. */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const html = `<html><body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body></html>`;

// Mock API responses keyed by (method, path).
const RITUALS = [
  { id: "r1", name: "Gaming", description: "Comms + game", action_count: 4, tags: ["games"], updated_at: "2026-01-01T00:00:00Z" },
  { id: "r2", name: "Work", description: "Dev setup", action_count: 3, tags: ["work"], updated_at: "2026-01-01T00:00:00Z" },
];
const MOCK = {
  "GET api/status": { online: true, platform: "generic", cpu: 12.0, memory: { percent: 40 }, running_ritual: null },
  "GET api/rituals": RITUALS,
  "GET api/run/current": { running: false },
  "GET api/history?limit=50": [],
  "GET api/devices": [],
  "GET api/pair/state": { active_code: null, expires_in: null },
  "GET api/deck": [],
  "GET api/media/now": {},
  "GET api/settings": { update_url: "" },
  "GET api/update/status": { version: "0.1.0", available: null },
  "GET api/spotify/status": { configured: false, connected: false, redirect_uri: "http://127.0.0.1:8765/callback" },
  "GET api/auth/status": { needs_setup: false, authenticated: true, username: "tester", onboarded: true },
};

async function main() {
  const dom = new JSDOM(html, { url: "http://127.0.0.1:8765/", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.scrollTo = () => {};
  window.confirm = () => true;

  window.fetch = async (url, opts = {}) => {
    const method = (opts.method || "GET").toUpperCase();
    // Normalize: app uses apiBase="api" so URLs look like "api/status"; keys below use "api/...".
    const key = `${method} ${String(url).replace(/^\//, "")}`;
    let body = MOCK[key];
    if (body === undefined && method === "GET" && String(url).includes("/api/rituals/")) {
      const id = String(url).split("/").pop();
      body = RITUALS.find((r) => r.id === id) || null;
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

  // Trigger DOMContentLoaded then wait for async init.
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  await new Promise((r) => setTimeout(r, 400));

  const doc = dom.window.document;
  const textMaybe = () => (doc.body.textContent || "");
  const bodyText = textMaybe();
  const navCount = doc.querySelectorAll(".nav-item").length;
  const hasGaming = bodyText.includes("Gaming");
  const hasWork = bodyText.includes("Work");
  const hasDashboard = bodyText.includes("Dashboard");
  const hasQuickLaunch = bodyText.includes("Quick Launch");

  console.log("JS ERRORS:", jsErrors.length ? jsErrors : "none");
  console.log("NAV ITEMS:", navCount);
  console.log("DASHBOARD TITLE:", hasDashboard);
  console.log("QUICK LAUNCH SECTION:", hasQuickLaunch);
  console.log("RITUAL 'Gaming' RENDERED:", hasGaming);
  console.log("RITUAL 'Work' RENDERED:", hasWork);

  // Navigate to Rituals view.
  const navRituals = [...doc.querySelectorAll(".nav-item")].find((n) => n.dataset.route === "rituals");
  navRituals.click();
  await new Promise((r) => setTimeout(r, 120));
  const ritualsBody = textMaybe();
  // The UI was renamed Rituals -> Plays in the Easy Life rebrand; this
  // assertion was stale and had been failing silently (the JS suites are not
  // part of the pytest run).
  console.log("PLAYS PAGE HAS 'New Play':", ritualsBody.includes("New Play"));
  console.log("RITUALS PAGE HAS 'Import':", ritualsBody.includes("Import"));
  console.log("RITUALS PAGE SHOWS GAMING CARD:", ritualsBody.includes("Gaming"));

  // 7, not 8: the Integrations tab is hidden on non-Windows hosts (the mock
  // reports platform "generic"), so it is not in the nav.
  const pass = jsErrors.length === 0 && navCount === 7 && hasGaming && hasWork && ritualsBody.includes("New Play");
  console.log(pass ? "FRONTEND SMOKE: PASS" : "FRONTEND SMOKE: FAIL");
  process.exit(pass ? 0 : 1);
}
main().catch((e) => { console.error("FATAL", e); process.exit(1); });