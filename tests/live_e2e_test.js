/* Deep E2E: run the REAL app.js against the REAL server using real fetch. */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const BASE = "http://127.0.0.1:8765/";

const dom = new JSDOM(`<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
  { url: BASE, runScripts: "outside-only" });
const w = dom.window;
w.scrollTo = ()=>{}; w.confirm = ()=>true;
class ES { constructor(){} close(){} }
w.EventSource = ES;
// Real fetch, but resolve the app's relative URLs against the server.
const badCalls = [];
w.fetch = async (url, opts={}) => {
  const abs = new URL(String(url), BASE).toString();
  const res = await fetch(abs, opts);
  if (res.status >= 400) badCalls.push(`${res.status} ${(opts.method||"GET")} ${abs.replace(BASE,"")}`);
  return res;
};
const errs = [];
w.addEventListener("error", e => errs.push("JSERR:"+e.message));

w.eval(fs.readFileSync("pcrituals/web/app.js","utf8"));
dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));

const sleep = ms => new Promise(r=>setTimeout(r,ms));
(async () => {
  await sleep(700);
  const doc = w.document;
  console.log("1. screen:", doc.querySelector(".auth-title")?.textContent || "(app, no auth screen)");
  const u = doc.querySelector("#au-user");
  if (!u) { console.log("   no setup form; app HTML:", doc.getElementById("app").innerHTML.slice(0,150)); process.exit(1); }
  const isSetup = !!doc.querySelector("#au-pass2");
  u.value = isSetup ? "liveuser" : "alex";
  doc.querySelector("#au-pass").value = "secret123";
  if (isSetup) doc.querySelector("#au-pass2").value = "secret123";
  doc.querySelector("#au-go").click();
  await sleep(1200);
  console.log("2. after signup, nav items:", doc.querySelectorAll(".nav-item").length);
  console.log("3. stored token present:", !!w.localStorage.getItem("pcrituals.token"));
  console.log("4. tour:", doc.querySelector(".tour-title")?.textContent || "(none)");
  if (doc.querySelector("#tour-skip")) { doc.querySelector("#tour-skip").click(); await sleep(200); }
  // Now exercise real actions that were reported broken:
  const nav = (r) => { const b=[...doc.querySelectorAll(".nav-item")].find(n=>n.dataset.route===r); if(b) b.click(); };
  nav("remote"); await sleep(400);
  const gen = [...doc.querySelectorAll("button")].find(b=>b.textContent.includes("Generate Pairing Code"));
  console.log("5. Generate Pairing Code button present:", !!gen);
  if (gen) {
    gen.click(); await sleep(900);
    const code = doc.querySelector(".pair-code")?.textContent;
    console.log("6. pairing code shown:", code || "(NONE - BUG)");
    console.log("7. QR image rendered:", !!doc.querySelector(".qr-box img"));
  }
  nav("settings"); await sleep(500);
  console.log("8. settings rendered:", !!doc.querySelector("#dev-list") || doc.body.textContent.includes("Paired Devices"));
  // Check for a persistent 'login required' error toast
  const toastText = doc.getElementById("toast")?.textContent || "";
  console.log("9. toast text:", JSON.stringify(toastText));
  console.log("10. JS errors:", errs.length ? errs : "none");
  console.log("11. FAILED REQUESTS (>=400):", badCalls.length ? badCalls : "none");
  console.log("12. token still stored:", !!w.localStorage.getItem("pcrituals.token"));
  process.exit(0);
})();
