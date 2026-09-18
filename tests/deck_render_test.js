const { JSDOM } = require("jsdom");
const fs = require("fs");
const dom = new JSDOM(`<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
  { url: "http://127.0.0.1:8765/", runScripts: "outside-only" });
const w = dom.window; w.scrollTo=()=>{}; w.confirm=()=>true; w.EventSource=class{};
const DECK=[
  {id:"b1",kind:"game",target:"steam://rungameid/570",label:"Warzone",icon:"https://www.google.com/s2/favicons?sz=128&domain=callofduty.com",color:""},
  {id:"b2",kind:"website",target:"https://twitch.tv",label:"Twitch",icon:"https://www.google.com/s2/favicons?sz=128&domain=twitch.tv",color:""},
  {id:"b3",kind:"media",target:"play_pause",label:"Play",icon:"🎵",color:""},
];
const MOCK={
 "GET api/deck":DECK,
 "GET api/media/now":{title:"Blinding Lights",artist:"The Weeknd",source:"spotify",display:"The Weeknd — Blinding Lights",playing:true},
 "GET api/status":{online:true,cpu:5,memory:{percent:40},running_ritual:null},
 "GET api/rituals":[],"GET api/run/current":{running:false},"GET api/history?limit=50":[],
 "GET api/devices":[],"GET api/pair/state":{},"GET api/backups":[],"GET api/settings":{update_url:""},
 "GET api/update/status":{version:"0.1.0",available:null},
 "GET api/auth/status":{needs_setup:false,authenticated:true,username:"tester",onboarded:true},
};
w.fetch=async (url,o={})=>{const m=(o.method||"GET").toUpperCase();const key=`${m} ${String(url).replace(/^\//,"")}`;let body=MOCK[key];if(body===undefined)body={};return{ok:true,status:200,headers:{get:()=>"application/json"},json:async()=>body,text:async()=>JSON.stringify(body)}};
let errs=[];w.addEventListener("error",e=>errs.push("JSERR:"+e.message));
w.eval(fs.readFileSync("pcrituals/web/app.js","utf8"));
dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
setTimeout(async ()=>{
  const doc=dom.window.document;
  const nav=[...doc.querySelectorAll(".nav-item")].find(n=>n.dataset.route==="deck");
  console.log("deck nav exists:", !!nav);
  nav.click(); await new Promise(r=>setTimeout(r,150));
  const page=doc.getElementById("page-deck");
  console.log("deck buttons rendered:", doc.querySelectorAll(".deck-btn").length);
  console.log("deck labels:", [...doc.querySelectorAll(".deck-btn-label")].map(e=>e.textContent).join(", "));
  const imgs=doc.querySelectorAll(".deck-icon-img");
  console.log("icon <img> count:", imgs.length);
  const media=doc.querySelector(".am-player");
  console.log("media player present:", !!media);
  console.log("media title:", (doc.querySelector(".am-title")||{}).textContent);
  console.log("media controls:", doc.querySelectorAll(".am-pill").length);
  console.log("errors:", errs.length?errs:"none");
  process.exit(0);
}, 400);
