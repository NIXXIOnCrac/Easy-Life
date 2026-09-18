/* Regression tests for the phone pairing / portrait / PWA bugs.
 *
 * Reported by the user:
 *   1. "whenever I open it on my phone I can't type in the code, it auto-removes
 *      my keyboard and resets every 2.5 seconds"
 *   2. "add a QR code scanner in the app itself"
 *   3. "make it work when I have my phone vertically"
 *   4. "if I scan the QR and then add the site to my home screen it isn't paired
 *      on the home screen, but when I open Safari it is paired"
 *
 * Run: node tests/phone_pairing_test.js     (exit 0 = pass)
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const APP = fs.readFileSync(path.join(__dirname, "..", "pcrituals/web/app.js"), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PHONE = "http://192.168.1.50:8765/";
const PC = "http://127.0.0.1:8765/";

let failures = 0;
function check(label, cond, extra) {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${label}${extra ? "  :: " + extra : ""}`);
  if (!cond) failures++;
}

function boot({ origin = PHONE, overrides = {}, token = "", cookie = "" } = {}) {
  const dom = new JSDOM(
    `<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
    { url: origin, runScripts: "outside-only" }
  );
  const w = dom.window;
  w.Response = Response;
  w.scrollTo = () => {};
  w.confirm = () => true;
  w.EventSource = class { constructor(u) { this.u = u; } close() {} addEventListener() {} };

  const defaults = {
    "auth/status": { needs_setup: false, authenticated: true, username: "alex", onboarded: true },
    rituals: [{ id: "r1", name: "Gaming Mode", actions: [], action_count: 2 }],
    "history?limit=50": [],
    devices: [],
    "pair/state": { active: null },
    backups: [],
    deck: [],
    "media/now": {},
    "spotify/status": { connected: false },
    settings: { update_url: "" },
    "update/status": { available: null, configured: true },
    "run/current": { running: false },
    integrations: [],
  };
  const calls = {};
  w.fetch = async (url, opts = {}) => {
    const u = String(url);
    calls[u] = (calls[u] || 0) + 1;
    let payload = defaults;
    for (const k of Object.keys(defaults)) if (u.includes(k)) payload = defaults[k];
    for (const k of Object.keys(overrides)) if (u.includes(k)) payload = overrides[k];
    let body = typeof payload === "function" ? payload(u, opts) : payload;
    return new w.Response(JSON.stringify(body === undefined ? {} : body), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };

  if (token) w.localStorage.setItem("pcrituals.token", JSON.stringify(token));
  if (cookie) w.document.cookie = cookie;
  w.eval(APP);
  w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
  return { dom, w, doc: w.document, calls };
}

async function goRemote(b) {
  b.w.location.hash = "#/remote";
  b.w.dispatchEvent(new b.w.Event("hashchange"));
  await sleep(120);
}

(async () => {
  // ───────────────────────────────────────────────────────────────────────
  console.log("== 1. The poll tick must NOT wipe the pairing code the user is typing ==");
  {
    const b = boot();
    await goRemote(b);
    await sleep(150);

    const input = b.doc.querySelector("#ph-code");
    check("pairing screen shows a code input", !!input);
    if (input) {
      input.focus();
      input.value = "A2D51F";
      check("isUserEditing() is true while the field has focus", b.w.isUserEditing() === true);

      // The reported symptom: everything reset every 2.5s. Wait past a tick.
      await sleep(3000);

      // The tick really did run during that wait — it calls /run/current — so
      // this is a genuine test of the poll, not a no-op sleep.
      const tickRan = (b.calls["api/run/current"] || 0) >= 1;
      check("the 2.5s poll tick actually fired during the wait", tickRan,
            "run/current calls=" + (b.calls["api/run/current"] || 0));

      const after = b.doc.querySelector("#ph-code");
      check("input node is still the same element (page was not re-rendered)", after === input);
      check("typed code survived the 2.5s poll tick", after && after.value === "A2D51F",
            after ? `value=${JSON.stringify(after.value)}` : "input gone");
      check("input still has focus (keyboard stays up)", b.doc.activeElement === after);
    }
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 2. A re-render of the SAME screen must preserve typed input ==");
  {
    const b = boot();
    await goRemote(b);
    await sleep(150);
    const input = b.doc.querySelector("#ph-code");
    input.focus();
    input.value = "BEEF42";
    // Force the kind of full render the tick used to perform. The listener is
    // on `window` (see app.js), so the event must be dispatched there — a
    // synthetic event on `document` never reaches it and the test would pass
    // without exercising anything.
    b.w.dispatchEvent(new b.w.Event("hashchange"));
    await sleep(150);
    const after = b.doc.querySelector("#ph-code");
    check("same input node after a forced re-render", after === input);
    check("value survived", after && after.value === "BEEF42",
          after ? `value=${JSON.stringify(after.value)}` : "gone");
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 3. Scanning the QR must not lose the pairing code ==");
  {
    const b = boot({ overrides: { "pair/complete": { token: "devtok123", device_name: "iPhone" } } });
    // The QR encodes this exact shape (see app.py: f"{base}/#/remote?code={code}").
    b.w.location.hash = "#/remote?code=A2D51F";
    b.w.dispatchEvent(new b.w.Event("hashchange"));
    await sleep(400);
    const pairedReq = Object.keys(b.calls).find((u) => u.includes("pair/complete"));
    check("app POSTed /pair/complete straight from the scanned QR", !!pairedReq, pairedReq || "");
    check("device token was stored from the QR pair", b.w.localStorage.getItem("pcrituals.token") !== null);
    check("the code was stripped from the URL (no re-pair on refresh)",
          !String(b.w.location.hash).includes("code="), String(b.w.location.hash));
    check("route is 'remote', not 'remote?code=…'",
          b.doc.querySelector("#page-remote") &&
          b.doc.querySelector("#page-remote").classList.contains("active"));
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 4. extractPairCode accepts a URL or a bare code, rejects junk ==");
  {
    const b = boot();
    const f = b.w.extractPairCode;
    check("URL form", f("http://192.168.1.50:8765/#/remote?code=A2D51F") === "A2D51F");
    check("bare code", f("a2d51f") === "A2D51F");
    check("whitespace tolerated", f("  A2D51F ") === "A2D51F");
    check("random URL rejected", f("https://example.com/hello") === "");
    check("empty rejected", f("") === "");
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 5. Pairing must persist into a home-screen app (separate storage) ==");
  {
    // Pair in Safari -> the token must ALSO land in a cookie, because that is
    // what iOS carries over when an icon is created.
    const b = boot({ overrides: { "pair/complete": { token: "devtok999" } } });
    b.w.location.hash = "#/remote?code=AAAA11";
    b.w.dispatchEvent(new b.w.Event("hashchange"));
    await sleep(350);
    check("token written to localStorage", b.w.localStorage.getItem("pcrituals.token") !== null);
    check("token ALSO written to a first-party cookie",
          b.w.document.cookie.includes("pcrituals_device="), b.w.document.cookie);

    // Now simulate the home-screen container: no localStorage at all, only the
    // cookie that iOS copied over. The app must still consider itself paired.
    const b2 = boot({ cookie: "pcrituals_device=" + "devtok999" });
    await goRemote(b2);
    await sleep(250);
    check("home-screen app treats the cookie token as paired",
          !b2.doc.querySelector("#ph-code"));
    check("home-screen app shows the connected controls",
          String(b2.doc.querySelector("#page-remote").innerHTML).includes("PC Controls"));
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 6. Portrait / keyboard: the input is reachable, tab bar yields ==");
  {
    const b = boot();
    await goRemote(b);
    await sleep(150);
    // Emulate the keyboard covering most of the visual viewport.
    b.w.visualViewport = {
      height: 300, offsetTop: 0,
      addEventListener(ev, fn) { this["on_" + ev] = fn; },
    };
    b.w.initKeyboardHandling();
    b.w.visualViewport.on_resize();
    check("body gets .kb-open while the keyboard covers the viewport",
          b.doc.body.classList.contains("kb-open"));
    const input = b.doc.querySelector("#ph-code");
    input.focus();
    check("isUserEditing() true with the field focused", b.w.isUserEditing() === true);
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 7. The scanner degrades honestly when the camera is blocked ==");
  {
    const b = boot();
    // Insecure context (plain http on a LAN IP) is exactly the real phone case.
    Object.defineProperty(b.w, "isSecureContext", { value: false, configurable: true });
    // The app reads navigator.mediaDevices, not window.mediaDevices.
    Object.defineProperty(b.w.navigator, "mediaDevices", { value: undefined, configurable: true });
    b.w.jsQR = undefined;
    let toasted = "";
    b.w.toast = (m) => { toasted = String(m); };
    await b.w.openQRScanner();
    check("no crash when the camera is unavailable", true);
    check("the modal is NOT opened", !b.doc.querySelector(".qr-scan-modal"));
  }

  // ───────────────────────────────────────────────────────────────────────
  console.log("\n== 8. The scanner pairs when the camera IS available ==");
  {
    const b = boot({ overrides: { "pair/complete": { token: "scantok42" } } });
    Object.defineProperty(b.w, "isSecureContext", { value: true, configurable: true });
    // A QR carrying the same URL shape the PC generates.
    b.w.jsQR = () => ({ data: "http://192.168.1.50:8765/#/remote?code=ZZ99ZZ" });
    Object.defineProperty(b.w.navigator, "mediaDevices", {
      value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
      configurable: true,
    });
    Object.defineProperty(b.w.HTMLVideoElement.prototype, "readyState", { get: () => 4, configurable: true });
    Object.defineProperty(b.w.HTMLVideoElement.prototype, "videoWidth", { get: () => 640, configurable: true });
    Object.defineProperty(b.w.HTMLVideoElement.prototype, "videoHeight", { get: () => 480, configurable: true });
    b.w.HTMLCanvasElement.prototype.getContext = () => ({
      drawImage() {}, getImageData: () => ({ data: new Uint8ClampedArray(4) }),
    });

    await b.w.openQRScanner();
    check("scanner modal opened", !!b.doc.querySelector(".qr-scan-modal"));
    await sleep(400);
    const req = Object.keys(b.calls).find((u) => u.includes("pair/complete"));
    check("camera scan POSTed /pair/complete", !!req, req || "");
    check("device token stored from the scan", b.w.localStorage.getItem("pcrituals.token") !== null);
    check("scanner closed itself after pairing", !b.doc.querySelector(".qr-scan-modal"));
  }

  console.log(`\n${failures === 0 ? "ALL PASS" : failures + " FAILURE(S)"}`);
  process.exit(failures === 0 ? 0 : 1);
})();
