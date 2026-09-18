/* Boss-loop deep test: log in, visit EVERY page, assert each renders + no errors. */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const BASE = "http://127.0.0.1:8765/";
const dom = new JSDOM(`<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
  { url: BASE, runScripts: "outside-only" });
const w = dom.window; w.scrollTo=()=>{}; w.confirm=()=>true;
w.EventSource = class { constructor(){} close(){} };
const failed = [];
const seq = [];
w.fetch = async (url, opts={}) => {
  const abs = new URL(String(url), BASE).toString();
  const auth = (opts.headers && (opts.headers.Authorization||opts.headers.authorization)) || "";
  const tlen = auth ? auth.replace("Bearer ","").length : 0;
  const res = await fetch(abs, opts);
  seq.push(`${res.status} ${(opts.method||"GET")} ${abs.replace(BASE,'')} tokenLen=${tlen}`);
  if (res.status >= 400) failed.push(`${res.status} ${(opts.method||"GET")} ${abs.replace(BASE,'')} tokenLen=${tlen}`);
  return res;
};
const errs = [];
w.addEventListener("error", e => errs.push("JSERR:"+e.message));
w.addEventListener("unhandledrejection", e => errs.push("REJECT:"+(e.reason&&e.reason.message||e.reason)));
w.eval(fs.readFileSync("pcrituals/web/app.js","utf8"));
dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
const sleep = ms => new Promise(r=>setTimeout(r,ms));

(async () => {
  await sleep(700);
  const doc = w.document;
  // ensure signed in
  if (doc.querySelector("#au-go")) {
    const isSetup = !!doc.querySelector("#au-pass2");
    doc.querySelector("#au-user").value = "deepuser";
    doc.querySelector("#au-pass").value = "secret123";
    if (isSetup) doc.querySelector("#au-pass2").value = "secret123";
    doc.querySelector("#au-go").click();
    await sleep(1200);
  }
  if (doc.querySelector("#tour-skip")) { doc.querySelector("#tour-skip").click(); await sleep(200); }
  console.log("signed in. nav:", doc.querySelectorAll(".nav-item").length);

  const routes = ["dashboard","rituals","builder","deck","remote","activity","integrations","settings"];
  for (const r of routes) {
    const btn = [...doc.querySelectorAll(".nav-item")].find(n => n.dataset.route === r);
    if (!btn) { console.log(`  ${r.padEnd(13)} NAV MISSING`); continue; }
    btn.click();
    await sleep(450);
    const page = doc.getElementById("page-"+r);
    const text = (page ? page.textContent : "").replace(/\s+/g," ").trim();
    const cards = page ? page.querySelectorAll(".card, .ritual-card, .deck-btn, .history-item, .placeholder").length : 0;
    const short = text.length < 30 ? "  <<< EMPTY/TOO SHORT" : "";
    console.log(`  ${r.padEnd(13)} chars=${String(text.length).padStart(5)} elements=${String(cards).padStart(3)}${short}`);
    if (r === "rituals" || r === "builder" || r === "remote") console.log("      TEXT: " + text.slice(0,220));
  }
  // Phone Remote must show the Generate button even while logged in
  const rr = doc.getElementById("page-remote");
  const hasGen = !!(rr && [...rr.querySelectorAll("button")].find(b=>b.textContent.includes("Generate Pairing Code")));
  console.log("  PhoneRemote has Generate Pairing Code:", hasGen, hasGen ? "" : "  <<< BUG");
  // Integrations must show tiles (on Linux they show as 'Windows detection' cards)
  const ir = doc.getElementById("page-integrations");
  const tiles = ir ? ir.querySelectorAll(".card").length : 0;
  console.log("  Integrations cards:", tiles, tiles > 1 ? "" : "  <<< BUG (no tiles)");
  console.log("FAILED REQUESTS:", failed.length ? failed : "none");
  console.log("--- request sequence (last 20) ---");
  seq.slice(-20).forEach(x=>console.log("   "+x));
  console.log("JS ERRORS:", errs.length ? errs : "none");
  process.exit(0);
})();
