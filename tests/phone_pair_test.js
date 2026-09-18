const { JSDOM } = require("jsdom");
const fs = require("fs");
// Simulate a PHONE: hostname is a LAN IP, not localhost.
const dom = new JSDOM(`<body><div id="app"></div><div id="toast"></div><div id="modal-root"></div></body>`,
  { url: "http://192.168.1.42:8765/", runScripts: "outside-only" });
const w = dom.window; w.scrollTo=()=>{}; w.confirm=()=>true; w.EventSource=class{};
let paired=false;
w.fetch=async (url,o={})=>{
  const m=(o.method||"GET").toUpperCase(); const path=String(url).replace(/^\//,"");
  if(m==="POST" && path==="api/pair/complete"){
    const body=JSON.parse(o.body||"{}");
    if((body.code||"").toUpperCase()==="A2D51F"){ paired=true; return {ok:true,status:200,headers:{get:()=>"application/json"},json:async()=>({device_id:"d1",token:"pcritTESTTOKEN",name:"iPhone"}),text:async()=>"" }; }
    return {ok:false,status:401,headers:{get:()=>"application/json"},json:async()=>({detail:"invalid or expired pairing code"}),text:async()=>""};
  }
  // Unpaired phone: all reads 401
  return {ok:false,status:401,headers:{get:()=>"application/json"},json:async()=>({detail:"authentication required"}),text:async()=>""};
};
let errs=[]; w.addEventListener("error",e=>errs.push("JSERR:"+e.message));
w.eval(fs.readFileSync("pcrituals/web/app.js","utf8"));
dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
setTimeout(async ()=>{
  const doc=dom.window.document;
  console.log("landed on hash:", w.location.hash);
  const codeInput=doc.getElementById("ph-code");
  console.log("phone pairing code input present:", !!codeInput);
  const goBtn=doc.getElementById("ph-go");
  console.log("pair button present:", !!goBtn);
  // wrong code
  if(codeInput){ codeInput.value="WRONG"; await w.submitPhonePairing(); await new Promise(r=>setTimeout(r,80)); }
  console.log("after wrong code, msg:", (doc.getElementById("ph-msg")||{}).textContent||"(none)");
  // right code
  if(codeInput){ codeInput.value="a2d51f"; await w.submitPhonePairing(); await new Promise(r=>setTimeout(r,120)); }
  console.log("after correct code, token stored:", !!(w.localStorage.getItem("pcrituals.token")));
  console.log("errors:", errs.length?errs:"none");
  process.exit(0);
}, 400);
