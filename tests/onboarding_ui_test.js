const { JSDOM } = require("jsdom");
const fs = require("fs");
const dom = new JSDOM(`<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
  { url: "http://127.0.0.1:8765/", runScripts: "outside-only" });
const w = dom.window; w.scrollTo=()=>{}; w.confirm=()=>true; w.EventSource=class{};
let authed = false, onboarded = false;

w.fetch = async (url, o={}) => {
  const m = (o.method||"GET").toUpperCase();
  const path = String(url).replace(/^\//, "");
  let body = {};
  if (m==="GET" && path==="api/auth/status")
    body = { needs_setup: !authed, authenticated: authed, username: authed?"brother":null, onboarded };
  else if (m==="POST" && path==="api/auth/setup") { authed = true; body = { token:"SESS1", username:"brother" }; }
  else if (m==="POST" && path==="api/auth/login") { authed = true; body = { token:"SESS2", username:"brother" }; }
  else if (m==="POST" && path==="api/auth/onboarded") { onboarded = true; body = { ok:true }; }
  else if (m==="GET" && path==="api/status") body = { online:true, cpu:3, memory:{percent:30} };
  else if (m==="GET" && path==="api/run/current") body = { running:false };
  else if (m.startsWith("GET") && /api\/(rituals|history|devices|backups|history\?)/.test(path)) body = [];
  else if (m==="GET" && path==="api/deck") body = [];
  else if (m==="GET" && path==="api/media/now") body = {};
  else if (m==="GET" && path==="api/settings") body = { update_url:"" };
  else if (m==="GET" && path==="api/update/status") body = { version:"0.1.0", available:null };
  else if (m==="GET" && path==="api/spotify/status") body = { configured:false, connected:false };
  else body = {};
  return { ok:true, status:200, headers:{get:()=>"application/json"}, json:async()=>body, text:async()=>JSON.stringify(body) };
};
let errs=[]; w.addEventListener("error", e=>errs.push("JSERR:"+e.message));
w.eval(fs.readFileSync("pcrituals/web/app.js","utf8"));
dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
setTimeout(async () => {
  const doc = dom.window.document;
  const has = (sel)=>!!doc.querySelector(sel);
  console.log("A. setup screen:", has(".auth-card"), "| title:", (doc.querySelector(".auth-title")||{}).textContent);
  if (!has("#au-user")) { console.log("APP HTML:", doc.getElementById("app").innerHTML.slice(0,200)); process.exit(1); }
  doc.querySelector("#au-user").value = "brother";
  doc.querySelector("#au-pass").value = "letmein1";
  doc.querySelector("#au-pass2").value = "letmein1";
  doc.querySelector("#au-go").click();
  await new Promise(r=>setTimeout(r,700));
  console.log("B. app nav items:", doc.querySelectorAll(".nav-item").length);
  console.log("C. tour shown:", has(".tour-card"), "| step:", (doc.querySelector(".tour-title")||{}).textContent);
  const next = doc.querySelector("#tour-next");
  if (next) { next.click(); await new Promise(r=>setTimeout(r,80)); }
  console.log("D. after Next:", (doc.querySelector(".tour-title")||{}).textContent, "| dots:", doc.querySelectorAll(".tour-dot").length);
  if (doc.querySelector("#tour-skip")) doc.querySelector("#tour-skip").click();
  await new Promise(r=>setTimeout(r,100));
  console.log("E. dismissed:", !has(".tour-card"), "| onboarded flag sent:", onboarded);
  console.log("errors:", errs.length?errs:"none");
  process.exit(0);
}, 500);
