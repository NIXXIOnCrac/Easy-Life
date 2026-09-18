/* Phone end-to-end: simulate a real iPhone (non-loopback origin) pairing against
   the real server, then confirm the PC can revoke it.

   jsdom's origin is set to a LAN IP so the app takes its "phone" branch, while
   the fetch wrapper rewrites traffic to the local server. */
const { JSDOM } = require("jsdom");
const fs = require("fs");

const LAN = "http://192.168.1.50:8765/";
const REAL = "http://127.0.0.1:8765/";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeClient(origin) {
  const dom = new JSDOM(
    `<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
    { url: origin, runScripts: "outside-only" }
  );
  const w = dom.window;
  w.scrollTo = () => {};
  w.confirm = () => true;
  w.EventSource = class { constructor() {} close() {} };
  const failed = [];
  w.fetch = async (url, opts = {}) => {
    const abs = new URL(String(url), REAL).toString();
    const res = await fetch(abs, opts);
    if (res.status >= 400) failed.push(`${res.status} ${(opts.method || "GET")} ${String(url)}`);
    return res;
  };
  w.eval(fs.readFileSync("pcrituals/web/app.js", "utf8"));
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  return { dom, w, failed, doc: w.document };
}

async function pcClient() {
  const c = makeClient("http://127.0.0.1:8765/");
  await sleep(700);
  const doc = c.doc;
  if (doc.querySelector("#au-go")) {
    const isSetup = !!doc.querySelector("#au-pass2");
    doc.querySelector("#au-user").value = "deepuser";
    doc.querySelector("#au-pass").value = "secret123";
    if (isSetup) doc.querySelector("#au-pass2").value = "secret123";
    doc.querySelector("#au-go").click();
    await sleep(1200);
  }
  if (doc.querySelector("#tour-skip")) { doc.querySelector("#tour-skip").click(); await sleep(200); }
  return c;
}

(async () => {
  let ok = true;
  const check = (label, cond, extra = "") => {
    console.log(`  ${cond ? "PASS" : "FAIL"}  ${label}${extra ? "  " + extra : ""}`);
    if (!cond) ok = false;
  };

  console.log("== PC ==");
  const pc = await pcClient();

  // 1. The PC can actually request a pairing code.
  const r = await pc.w.fetch("api/pair/start", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + (pc.w.localStorage.getItem("pcrituals.token") || "").replace(/"/g, "") },
    body: "{}",
  });
  const pairing = await r.json();
  check("PC can generate a pairing code", r.status === 200 && !!pairing.code, `code=${pairing.code}`);
  check("QR image is provided", typeof pairing.qr_png === "string" && pairing.qr_png.startsWith("data:image"));
  const code = pairing.code;

  console.log("== PHONE (unpaired) ==");
  const ph = makeClient(LAN);
  await sleep(800);
  const pdoc = ph.doc;
  check("phone shows a code-entry screen (not the PC login)", !!pdoc.querySelector("#ph-code"));
  check("phone does NOT show a PC password field", !pdoc.querySelector("#au-pass"));

  // 2. Pair using the code.
  pdoc.querySelector("#ph-code").value = code;
  pdoc.querySelector("#ph-name").value = "Test iPhone";
  pdoc.querySelector("#ph-go").click();
  await sleep(1400);
  const tok = (ph.w.localStorage.getItem("pcrituals.token") || "").replace(/"/g, "");
  check("phone received a device token", tok.length > 20);
  check("phone token is NOT the PC session token", tok !== (pairing.token || ""));

  // 3. The paired phone can read rituals / status.
  const st = await ph.w.fetch("api/status", { headers: { Authorization: "Bearer " + tok } });
  check("paired phone can read PC status", st.status === 200);
  const rr = await ph.w.fetch("api/rituals", { headers: { Authorization: "Bearer " + tok } });
  check("paired phone can read rituals", rr.status === 200);
  check("phone landed on the remote view", !!pdoc.querySelector("#page-remote"));

  // 4. An unpaired client must be rejected.
  const bad = await ph.w.fetch("api/status", { headers: { Authorization: "Bearer not-a-real-token" }});
  check("bad token is rejected (401)", bad.status === 401);
  const none = await ph.w.fetch("api/status", {});
  check("no token is rejected (401)", none.status === 401);

  console.log("== PC REVOKES THE PHONE ==");
  const hdr = { Authorization: "Bearer " + (pc.w.localStorage.getItem("pcrituals.token") || "").replace(/"/g, "") };
  const devs = await (await pc.w.fetch("api/devices", { headers: hdr })).json();
  const dev = (Array.isArray(devs) ? devs : []).find((d) => !d.revoked && !d.is_local);
  check("paired phone appears in the device list", !!dev, dev ? dev.name : "none found");
  const rv = await pc.w.fetch(`api/devices/${dev.id}/revoke`, { method: "POST", headers: hdr, body: "{}" });
  check("revoke succeeds", rv.status === 200);
  const after = await ph.w.fetch("api/status", { headers: { Authorization: "Bearer " + tok } });
  check("revoked phone is locked out (401)", after.status === 401);

  console.log("\nFAILED HTTP CALLS:", ph.failed.length ? ph.failed : "none");
  console.log(ok ? "\nALL PHONE FLOW CHECKS PASSED" : "\nSOME CHECKS FAILED");
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error("TEST ERROR:", e); process.exit(2); });
