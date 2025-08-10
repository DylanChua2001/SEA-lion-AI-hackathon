// popup.js
const API = "http://127.0.0.1:8001"; // FastAPI

function log(m){ const el=document.getElementById("log"); el.textContent += m+"\n"; el.scrollTop = el.scrollHeight; }
async function getActiveTab(){ const [t]=await chrome.tabs.query({active:true,currentWindow:true}); return t; }

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("run").addEventListener("click", onRun);
});

async function runTool(tabId, tool, args){
  return await chrome.tabs.sendMessage(tabId, { type:"RUN_TOOL", tool, args });
}

async function onRun(){
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");
  if (!/^https?:\/\//.test(tab.url || "")) return log("Open a normal webpage first.");

  // inject content tools
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });

  // health
  const h = await fetch(`${API}/health`).then(r=>r.json()).catch(e=>({error:String(e)}));
  log("health: " + JSON.stringify(h));
  if (!h || h.ok !== true) return log("Backend not healthy");

  const goal = (document.getElementById("goal")?.value || "Open appointments page").trim();
  const sessionId = crypto.randomUUID();

  let lastTool = null, lastObs = null;

  for (let turn = 0; turn < 30; turn++){
    // get page_state from content
    const snap = await runTool(tab.id, "get_page_state", {});
    const page_state = snap?.data || { url: tab.url, title: tab.title };

    // ask agent
    const body = { session_id: sessionId, goal, last_tool: lastTool, last_observation: lastObs, page_state };
    log(">> /agent/step body: " + JSON.stringify(body));
    const res = await fetch(`${API}/agent/step`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
    const { next } = await res.json();
    log(`<< agent: ${next.tool} ${JSON.stringify(next.args)}`);

    if (next.tool === "done" || next.tool === "fail"){
      log(`** ${next.tool.toUpperCase()}: ${JSON.stringify(next.args)}`);
      break;
    }

    // execute tool in page
    const obs = await runTool(tab.id, next.tool, next.args);
    log(`.. obs: ${JSON.stringify(obs)}`);

    lastTool = next;
    lastObs  = obs;

    await new Promise(r => setTimeout(r, 200)); // tiny pacing
  }
}
