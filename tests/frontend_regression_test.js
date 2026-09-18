/* Regression tests for the frontend audit findings (F-01..F-04, F-08).
 *
 * Each of these was a real defect found by an audit pass. The API is mocked so a
 * malformed payload can be injected deterministically.
 *
 * Run: node tests/frontend_regression_test.js     (exit 0 = pass)
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const APP = fs.readFileSync(path.join(__dirname, "..", "pcrituals/web/app.js"), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
function check(label, cond, extra) {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${label}${extra ? "  :: " + extra : ""}`);
  if (!cond) failures++;
}

/** Boot the app in jsdom with a mock API.
 *  `overrides` maps a path fragment -> value (or function) to return.
 */
function boot({ origin = "http://127.0.0.1:8765/", overrides = {}, token = "" } = {}) {
  const dom = new JSDOM(
    `<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
    { url: origin, runScripts: "outside-only" }
  );
  const w = dom.window;
  // jsdom does not implement Response; Node does. Without this the mock fetch
  // throws and every request fails, which looks like an app bug but is not.
  w.Response = Response;
  w.scrollTo = () => {};
  w.confirm = () => true;
  const sseUrls = [];
  w.EventSource = class { constructor(u) { sseUrls.push(String(u)); } close() {} addEventListener() {} };
  const calls = {};
  const rejections = [];
  w.addEventListener("unhandledrejection", (e) => {
    rejections.push(String((e.reason && e.reason.message) || e.reason));
  });
  w.addEventListener("error", (e) => rejections.push("JSERR:" + e.message));

  const defaults = {
    "auth/status": { needs_setup: false, authenticated: true, username: "alex", onboarded: true },
    rituals: [{ id: "r1", name: "Gaming Mode", actions: [], steps: 1 }],
    "history?limit=50": [],
    devices: [],
    "pair/state": { active: null },
    "backups": [],
    deck: [],
    "media/now": {},
    "spotify/status": { connected: false },
    settings: { update_url: "" },
    "update/status": { available: null, configured: true },
    "run/current": { running: false },
    integrations: [{ id: "steam", name: "Steam", kind: "game", installed: true }],
  };

  w.fetch = async (url, opts = {}) => {
    const u = String(url);
    calls[u] = (calls[u] || 0) + 1;
    let payload = defaults;
    for (const k of Object.keys(defaults)) if (u.includes(k)) payload = defaults[k];
    for (const k of Object.keys(overrides)) if (u.includes(k)) payload = overrides[k];
    let body = payload;
    if (typeof payload === "function") body = payload(u, opts);
    if (body && body.__status) {
      return new w.Response(body.__body !== undefined ? JSON.stringify(body.__body) : "{}", {
        status: body.__status, headers: { "Content-Type": "application/json" },
      });
    }
    // `{ __raw: "<html>…", __ctype: "text/html" }` returns a NON-JSON body, so a
    // captive portal / proxy error page can be simulated (the app sees text).
    if (body && body.__raw !== undefined) {
      return new w.Response(body.__raw, {
        status: body.__status || 200,
        headers: { "Content-Type": body.__ctype || "text/html" },
      });
    }
    return new w.Response(JSON.stringify(body === undefined ? {} : body), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };

  if (token) w.localStorage.setItem("pcrituals.token", JSON.stringify(token));
  w.eval(APP);
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  return { dom, w, doc: w.document, calls, rejections, sseUrls };
}

(async () => {
  console.log("== F-01: a throw AFTER an await in an async page must show the error card ==");
  {
    // An array containing null is a realistic malformed payload: it passes the
    // Array.isArray guard but blows up inside the async renderer, AFTER its
    // await — the exact case a synchronous try/catch cannot catch.
    const c = boot({ overrides: { integrations: [null] } });
    await sleep(900);
    c.w.location.hash = "#/integrations";
    await sleep(800);
    const page = c.doc.getElementById("page-integrations");
    const text = (page ? page.textContent : "").trim();
    check("page is not silently blank", text.length > 0, `len=${text.length}`);
    check("error card is shown for a failure after await", /hit a problem/i.test(text),
      text.slice(0, 70).replace(/\s+/g, " "));
    check("no unhandled rejection escaped", c.rejections.length === 0,
      c.rejections.slice(0, 2).join("; "));
  }

  console.log("== F-02: a non-array 200 must not poison module state ==");
  {
    const c = boot({ overrides: { rituals: { nope: true }, deck: { nope: true }, devices: { nope: true } } });
    await sleep(1200);
    const text = (c.doc.getElementById("page-dashboard") || { textContent: "" }).textContent;
    check("dashboard still renders", text.trim().length > 0);
    check("no unhandled rejections from wrong-typed payloads", c.rejections.length === 0,
      c.rejections.slice(0, 2).join("; "));
  }

  console.log("== F-03: integrations retries after a failed fetch (sentinel kept) ==");
  {
    let fail = true;
    const c = boot({
      overrides: {
        integrations: () => (fail ? { __status: 500, __body: { detail: "boom" } } : [{ id: "x", name: "X", kind: "app", installed: true }]),
      },
    });
    await sleep(900);
    c.w.location.hash = "#/integrations";
    await sleep(700);
    const first = c.calls["api/integrations"] || 0;
    const failedText = (c.doc.getElementById("page-integrations") || { textContent: "" }).textContent;
    check("a failed load is reported, not shown as empty", /Couldn't load/i.test(failedText));
    // The whole point of the fix: a failure must not be cached as a successful
    // empty result, so coming back to the page must try again.
    c.w.location.hash = "#/dashboard";
    await sleep(150);
    c.w.location.hash = "#/integrations";
    await sleep(700);
    const second = c.calls["api/integrations"] || 0;
    check("revisiting the page retries after a failure", second > first,
      `first visit=${first} second visit=${second}`);

    // And the explicit Re-scan must recover.
    fail = false;
    c.w.refreshIntegrationData();
    await sleep(600);
    const after = c.calls["api/integrations"] || 0;
    check("Re-scan triggers a fresh fetch", after > second, `before=${second} after=${after}`);
    const okText = (c.doc.getElementById("page-integrations") || { textContent: "" }).textContent;
    check("recovered data is rendered", /X/.test(okText) && !/Couldn't load/i.test(okText));
  }

  console.log("== F-04: the PC must not offer a code entry that steals its session ==");
  {
    const c = boot({ token: "session-token" });   // loopback origin = the PC
    await sleep(1000);
    c.w.location.hash = "#/remote";
    await sleep(500);
    const page = c.doc.getElementById("page-remote");
    const html = page ? page.innerHTML : "";
    check("no 'Enter Code Manually' on the PC", !/manualPairModal/.test(html));
    check("'Generate Pairing Code' IS offered on the PC", /startPairing/.test(html));
    const stored = c.w.localStorage.getItem("pcrituals.token");
    check("PC session token untouched", stored === JSON.stringify("session-token"), String(stored));
  }

  console.log("== F-08: a paired phone must land on Phone Remote, not the Dashboard ==");
  {
    const c = boot({ origin: "http://192.168.1.50:8765/", token: "devicetoken" });
    await sleep(1200);
    check("paired phone lands on #/remote", c.w.location.hash === "#/remote", c.w.location.hash);
  }

  console.log("== F-11: an unauthenticated client must not open an event stream ==");
  {
    const c = boot({ origin: "http://192.168.1.50:8765/" });   // no token
    await sleep(900);
    const withNoToken = c.sseUrls.filter((u) => !u.includes("token="));
    check("no unauthenticated /events stream opened", withNoToken.length === 0, withNoToken.join(","));
  }

  console.log("== Sync card: connected vs disconnected states ==");
  {
    // Disconnected: the setup form must be shown, and it must say nothing leaves
    // the PC until you connect.
    const c1 = boot({ overrides: { "sync/status": { configured: false, url: "", username: "" } } });
    await sleep(900);
    c1.w.location.hash = "#/settings";
    await sleep(600);
    const t1 = (c1.doc.getElementById("page-settings") || { textContent: "" }).textContent;
    check("sync card is present in Settings", /Sync across PCs/i.test(t1));
    check("disconnected state offers Connect + Create account", /Connect/.test(t1) && /Create account/.test(t1));
    check("it states nothing leaves the PC until connected", /off until you connect/i.test(t1));

    // Connected: push/pull controls instead of the form.
    const c2 = boot({ overrides: { "sync/status": { configured: true, url: "http://x:8788", username: "alex", revision: 3, last_sync: "2026-09-16T10:00:00+00:00" } } });
    await sleep(900);
    c2.w.location.hash = "#/settings";
    await sleep(700);
    const t2 = (c2.doc.getElementById("page-settings") || { textContent: "" }).textContent;
    check("connected state shows Push and Pull", /Push this PC/.test(t2) && /Pull \(merge\)/.test(t2));
    check("connected state shows the server and username", /alex/.test(t2) && /8788/.test(t2));
    check("the sync token is never rendered into the page", !/token/i.test(t2.replace(/Sync across PCs/i, "")), "");
  }

  console.log("== F-17: a malformed 200 must not replace a page with the error card ==");
  {
    // An HTML body on /api/backups (captive portal, proxy error page). It used to
    // land in state verbatim, so renderBackups threw `state.backups.map is not a
    // function` and the error card replaced Settings.
    const c = boot({ overrides: { backups: { __raw: "<html><body>Captive portal</body></html>" } } });
    await sleep(1000);
    c.w.location.hash = "#/settings";
    await sleep(700);
    const t = (c.doc.getElementById("page-settings") || { textContent: "" }).textContent;
    check("Settings survives an HTML body on /backups", !/hit a problem/i.test(t),
      t.slice(0, 60).replace(/\s+/g, " "));
    check("Settings still shows its own content", /Paired Devices/.test(t) && /No backups yet/.test(t));
    check("no unhandled rejection from the HTML body", c.rejections.length === 0,
      c.rejections.slice(0, 2).join("; "));
  }
  {
    // `history` containing null used to throw
    // `Cannot read properties of null (reading 'status')` on Activity. The same
    // payload carries a hostile `status` to prove that field is escaped too (F-20).
    const c = boot({
      overrides: {
        "history?limit=50": [
          null,
          { ritual_name: "OkRun", status: '"><img src=x onerror=alert(1)>',
            started_at: "2026-08-01T10:00:00+00:00", actions_completed: 1, actions_total: 1 },
        ],
      },
    });
    await sleep(1000);
    c.w.location.hash = "#/activity";
    await sleep(700);
    const page = c.doc.getElementById("page-activity");
    const at = page ? page.textContent : "";
    check("Activity survives a null entry in history", !/hit a problem/i.test(at),
      at.slice(0, 60).replace(/\s+/g, " "));
    check("the valid history entry still renders", /OkRun/.test(at));
    check("a hostile history status injects no markup",
      page.querySelectorAll("img").length === 0 && page.querySelectorAll("[onerror]").length === 0);
  }
  {
    // Same treatment one level up: a null ritual / deck entry must not break a page.
    const c = boot({
      overrides: {
        rituals: [null, { id: "r1", name: "Gaming Mode", actions: [], action_count: 1 }],
        deck: [null],
      },
    });
    await sleep(1000);
    c.w.location.hash = "#/rituals";
    await sleep(600);
    const rt = (c.doc.getElementById("page-rituals") || { textContent: "" }).textContent;
    check("Rituals survives a null entry", !/hit a problem/i.test(rt) && /Gaming Mode/.test(rt),
      rt.slice(0, 60).replace(/\s+/g, " "));
    c.w.location.hash = "#/deck";
    await sleep(600);
    const dt2 = (c.doc.getElementById("page-deck") || { textContent: "" }).textContent;
    check("Deck survives a null entry", !/hit a problem/i.test(dt2));
  }

  console.log("== F-17b: an unparseable /auth/status must not sign the user out ==");
  {
    const c = boot({
      token: "session-token",
      overrides: { "auth/status": { __raw: "<html>Security check required</html>" } },
    });
    await sleep(1200);
    check("the stored token is kept", c.w.localStorage.getItem("pcrituals.token") === JSON.stringify("session-token"),
      String(c.w.localStorage.getItem("pcrituals.token")));
    check("no sign-in screen is shown", !c.doc.getElementById("au-go"));
    check("the app still starts (session kept)", c.doc.querySelectorAll("#app .page").length > 0);
    // Bounded retry: "unknown" is re-checked, but never in an endless loop.
    await sleep(2500);
    const asks = c.calls["api/auth/status"] || 0;
    check("the status re-check is bounded (no retry loop)", asks <= 6, `requests=${asks}`);
    check("still signed in after the re-checks",
      !c.doc.getElementById("au-go") && c.w.localStorage.getItem("pcrituals.token") === JSON.stringify("session-token"));
  }
  {
    // Control: an EXPLICIT denial is a real verdict and must still sign the user out.
    const c = boot({
      token: "session-token",
      overrides: { "auth/status": { authenticated: false, needs_setup: false } },
    });
    await sleep(900);
    check("an explicit authenticated:false still shows the sign-in screen", !!c.doc.getElementById("au-go"));
  }

  console.log("== F-2 (UI): a stop request reads as 'Stopping…', never as 'stopped' ==");
  {
    // The backend reports state "cancelling" until the action in flight is
    // actually dead; the UI must show that, not a finished-looking state.
    const c = boot({
      token: "t",
      overrides: {
        "run/current": { running: true, state: "cancelling", ritual_id: "r1", name: "Gaming",
                         actions_total: 3, actions_completed: 1,
                         steps: [{ name: "Open Steam", status: "running" }] },
      },
    });
    await sleep(3200);                       // wait for the 2.5s run poll
    c.w.location.hash = "#/dashboard";
    await sleep(900);
    const panel = c.doc.getElementById("dashboard-runner");
    const mini = c.doc.getElementById("mini-pc");
    const pt = panel ? panel.textContent : "";
    check("the run panel stays up while cancelling", !!panel);
    check("the panel says Stopping…", /Stopping…/.test(pt), pt.replace(/\s+/g, " ").slice(0, 60));
    check("the Stop button is inert while stopping", !!c.doc.querySelector("#dashboard-runner button[disabled]"));
    check("the mini PC strip says Stopping…", /Stopping…/.test(mini ? mini.textContent : ""),
      mini ? mini.textContent.replace(/\s+/g, " ").slice(0, 50) : "");
    check("nothing claims the run already stopped",
      !/stopped/i.test((c.doc.getElementById("page-dashboard") || { textContent: "" }).textContent));
    check("no unhandled rejections while cancelling", c.rejections.length === 0,
      c.rejections.slice(0, 2).join("; "));
  }
  {
    // Reacting to the stop request itself must say "Stopping…" — the response is
    // an acknowledgement, not an outcome.
    const c = boot({
      token: "t",
      overrides: {
        "run/current": { running: true, state: "running", ritual_id: "r1", name: "Gaming",
                         actions_total: 3, actions_completed: 1, steps: [] },
        stop: { stopping: true },
      },
    });
    await sleep(3200);
    c.w.location.hash = "#/dashboard";
    await sleep(300);
    await c.w.stopRun();
    await sleep(50);
    const toast = c.doc.getElementById("toast").textContent;
    check("the toast reports the request as Stopping…", /Stopping…/.test(toast), JSON.stringify(toast));
    check("the toast never reports the run as stopped", !/stopped/i.test(toast), JSON.stringify(toast));
  }

  console.log("== F-20: server strings in the pairing card are escaped ==");
  {
    const c = boot({
      token: "t",
      overrides: {
        "pair/start": { qr_png: '" onerror="alert(1)', code: '"><b>pwn</b>',
                        lan_url: 'http://x" onx="y', expires_in: 60 },
      },
    });
    await sleep(1200);
    c.w.location.hash = "#/remote";
    await sleep(600);
    await c.w.startPairing();
    await sleep(200);
    const area = c.doc.getElementById("pair-area");
    const img = area && area.querySelector("img");
    check("the QR image is rendered", !!img);
    check("qr_png cannot inject an event handler",
      !!img && !img.hasAttribute("onerror") && img.getAttribute("src") === '" onerror="alert(1)',
      img ? String(img.getAttribute("src")) : "");
    check("code/lan_url cannot inject markup",
      area.querySelectorAll("b").length === 0 && area.querySelectorAll("[onx]").length === 0);
    check("the pairing code is still shown as text", /pwn/.test(area.textContent));
  }

  console.log(failures ? `\n${failures} CHECK(S) FAILED` : "\nALL FRONTEND REGRESSION CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("TEST ERROR:", e); process.exit(2); });
