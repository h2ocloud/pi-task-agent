"""Local dashboard: tune the harness, send a short test prompt, read the log.

Binds to 127.0.0.1 only. It does not load a model and it does not print API keys.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SECRET = re.compile(r"(thk_live_|sk-|Bearer\s+|xai-|api[_-]?key[=:]\s*)[A-Za-z0-9_\-\.]+", re.I)

DEFAULTS = {
    "compact_at": 40000,
    "step_ctx": 45000,
    "skill_mode": "ondemand",
    "skill_limit": 3,
    "tool_stall": 180,
    "fg_wall": 600,
    "planner_temperature": 0.2,
    "provider": "auto",
}
BOUNDS = {
    "compact_at": (4000, 120000),
    "step_ctx": (4000, 120000),
    "skill_limit": (0, 20),
    "tool_stall": (15, 900),
    "fg_wall": (30, 7200),
    "planner_temperature": (0.0, 1.5),
}
MODES = ("ondemand", "all", "off")
PROVIDERS = ("auto", "local", "grok", "chatgpt")

RECIPE = {
    "machine": "NVIDIA DGX Spark, 128 GB unified memory, one resident model",
    "model": "Qwen3.8-Flash-Next, GGUF IQ3_S, served by Strata",
    "server_context": 131072,
    "kv": "int8",
    "speculative": 4,
    "harness_compact": 40000,
    "harness_step": 45000,
    "tool_temperature": 0.7,
    "json_temperature": 0.2,
    "grok_fast": "grok-4-1-fast-non-reasoning",
    "grok_main": "grok-build-0.1",
    "grok_base": "https://api.x.ai/v1",
}


def scrub(text: str) -> str:
    return _SECRET.sub(r"\1***", text or "")


def clamp(raw: dict | None) -> dict:
    src = raw or {}
    out = dict(DEFAULTS)
    for key, (lo, hi) in BOUNDS.items():
        if key not in src or src[key] in ("", None):
            continue
        try:
            num = float(src[key])
        except (TypeError, ValueError):
            continue
        if key != "planner_temperature":
            num = int(num)
        out[key] = max(lo, min(hi, num))
    mode = str(src.get("skill_mode") or out["skill_mode"]).lower()
    out["skill_mode"] = mode if mode in MODES else DEFAULTS["skill_mode"]
    provider = str(src.get("provider") or out["provider"]).lower()
    out["provider"] = provider if provider in PROVIDERS else DEFAULTS["provider"]
    return out


def load_tune(path: str) -> dict:
    try:
        return clamp(json.load(open(path, encoding="utf-8")))
    except Exception:
        return dict(DEFAULTS)


def save_tune(path: str, raw: dict) -> dict:
    data = clamp(raw)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return data


def apply_env(data: dict):
    os.environ["PITG_SKILLS"] = data["skill_mode"]
    os.environ["PITG_SKILL_LIMIT"] = str(int(data["skill_limit"]))
    os.environ["PITG_COMPACT_AT"] = str(int(data["compact_at"]))
    os.environ["PITG_STEP_CTX"] = str(int(data["step_ctx"]))
    os.environ["PITG_PROVIDER"] = data["provider"]


def tail(path: str, n: int = 200) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []
    return [scrub(x) for x in lines[-max(1, min(n, 500)):]]


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>pi-task-agent</title>
<style>
  :root { color-scheme: light; --paper:#e4ebf0; --ink:#17212b; --navy:#16324f; --amber:#e6b325; --line:#c3ced6; --well:#f5f8fa; --muted:#4e5d6b; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font:16px/1.45 "Avenir Next", "Segoe UI", sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:28px 22px 48px; }
  header { display:flex; justify-content:space-between; gap:16px; align-items:end; }
  h1 { margin:0; font:600 22px/1 "Avenir Next Condensed", "Avenir Next", "Arial Narrow", sans-serif; }
  h2 { margin:0 0 12px; font:600 18px/1.2 "Avenir Next Condensed", "Avenir Next", sans-serif; }
  button, input, select, textarea { font:inherit; }
  button { background:var(--navy); color:#fff; border:0; border-radius:0; padding:8px 14px; cursor:pointer; }
  #lang { background:transparent; color:var(--ink); border:1px solid var(--line); }
  #sub { margin:6px 0 0; color:var(--muted); max-width:42em; }
  #board { margin-top:18px; }
  #hero { background:var(--navy); color:#f4f7f8; padding:22px 22px 18px; }
  #hero-kicker { margin:0; color:#c5d4e0; }
  #live-model { margin:4px 0 0; color:var(--amber); font:600 52px/0.95 "Avenir Next Condensed", "Arial Narrow", sans-serif; letter-spacing:-.03em; word-break:break-word; }
  #hero-detail { margin:10px 0 0; max-width:40em; }
  #route { display:flex; border:1px solid var(--navy); border-top:0; }
  #route button { flex:1; background:var(--well); color:var(--navy); border-right:1px solid var(--navy); border-radius:0; padding:12px 6px; font-size:15px; }
  #route button:last-child { border-right:0; }
  #route button[aria-pressed="true"] { background:var(--navy); color:#fff; }
  main { display:grid; grid-template-columns:1fr 1fr; gap:28px 32px; margin-top:28px; }
  .nums { display:grid; grid-template-columns:1fr 1fr; gap:10px 14px; }
  label { display:flex; flex-direction:column; gap:4px; color:var(--muted); font-size:14px; }
  input, select, textarea { border:1px solid var(--line); border-radius:0; padding:8px; background:var(--well); color:var(--ink); }
  textarea { width:100%; min-height:96px; }
  #proof { min-height:1.4em; font-weight:600; }
  pre, #recipe { white-space:pre-wrap; word-break:break-word; background:#121a22; color:#d5dde3; padding:12px; max-height:280px; overflow:auto; font:12px/1.45 ui-monospace, monospace; }
  #out:empty { display:none; }
  #recipe { background:transparent; color:var(--ink); padding:0; max-height:none; font:15px/1.5 "Avenir Next", "Segoe UI", sans-serif; }
  .warn { color:#8a3b12; min-height:1.2em; }
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline:2px solid var(--amber); outline-offset:2px; }
  @media (max-width:720px) {
    #live-model { font-size:34px; }
    main, .nums, #route { grid-template-columns:1fr; display:grid; }
    #route button { border-right:0; border-bottom:1px solid var(--navy); }
    #route button:last-child { border-bottom:0; }
  }
  @media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto; } }
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>pi-task-agent</h1>
    <p id="sub"></p>
  </div>
  <button id="lang" type="button">繁體中文</button>
</header>
<div id="board">
  <div id="hero">
    <p id="hero-kicker"></p>
    <p id="live-model">—</p>
    <p id="hero-detail"></p>
  </div>
  <div id="route" role="group"></div>
</div>
<main>
  <section>
    <h2 id="h-tune">Limits</h2>
    <form id="tune" class="nums"></form>
    <p class="warn" id="warn"></p>
    <p><button id="save" type="button">Save limits</button> <span id="saved"></span></p>
  </section>
  <section>
    <h2 id="h-test">Prove the model</h2>
    <p id="test-note"></p>
    <textarea id="prompt">Reply with only your model id.</textarea>
    <p><label id="temp-lab">temperature <input id="temp" type="number" min="0" max="1.5" step="0.1" value="0.2"/></label></p>
    <p><button id="run" type="button">Send to the main model</button></p>
    <p id="proof"></p>
    <pre id="out"></pre>
  </section>
</main>
<section>
  <h2 id="h-recipe">Measured setup</h2>
  <div id="recipe"></div>
</section>
<section>
  <h2 id="h-log">Log</h2>
  <pre id="log"></pre>
</section>
</div>
<script>
const I18N = {
  en: {
    sub:"This page stays on this computer. Switching models does not load a second one onto the GPU.",
    tune:"Limits", test:"Prove the model", recipeTitle:"Measured setup", log:"Log",
    save:"Save limits", run:"Send to the main model", saved:"saved", lang:"繁體中文",
    note:"Same model as a Telegram chat. The id under the reply is what the endpoint returned.",
    temp:"temperature",
    warn:"Above 60k tokens this model starts to drift. Compact earlier.",
    proof:"Endpoint reported", fell:"Call failed over to the local model",
    empty:"(no log file yet)",
    kicker:{ grok:"Next Telegram message uses", chatgpt:"Next Telegram message uses the ChatGPT subscription", local:"Next Telegram message stays on", auto:"Next message tries the local model first" },
    unsigned:"No Grok session. Next message stays on",
    unsignedChat:"No ChatGPT subscription login. Next message stays on",
    deadChat:"ChatGPT login expired. Sign in again. Next message stays on",
    staleChat:"The access token is old. The next send refreshes it.",
    detail:{
      grok:"Grok Build. If the call fails, the local model {local} answers.",
      chatgpt:"ChatGPT subscription, model {chatgpt}. If the call fails, the local model {local} answers. Auto does not pick this.",
      local:"Local only. Grok is not called.",
      auto:"Local model {local} while it answers. Otherwise {grok}."
    },
    modes:[["grok","Grok Build"],["chatgpt","ChatGPT"],["local","Local only"],["auto","Local first"]],
    fields:{ compact_at:"Compact chat after (tokens)", step_ctx:"Step context cap", skill_mode:"Skills",
      skill_limit:"Skill cap", tool_stall:"Stuck tool (seconds)", fg_wall:"Foreground wall (seconds)",
      planner_temperature:"JSON helper temperature" },
    recipe:{ machine:"Machine", model:"Resident local model", server_context:"Server context", kv:"KV cache",
      speculative:"Speculative tokens", harness_compact:"Compact chat at", harness_step:"Step cap",
      tool_temperature:"Tool temperature", json_temperature:"JSON temperature",
      grok_fast:"Grok fast id", grok_main:"Grok main id" }
  },
  zh: {
    sub:"這個頁面只在這台電腦上。切換模型不會在 GPU 上再載一顆。",
    tune:"門檻", test:"對一下模型", recipeTitle:"實測配方", log:"日誌",
    save:"儲存門檻", run:"用主模型試一句", saved:"已存", lang:"English",
    note:"和 Telegram 聊天同一顆主模型。回覆下面那行是接口自己報的 id。",
    temp:"溫度",
    warn:"超過 6 萬 token，這顆模型容易跑偏。提早壓縮。",
    proof:"接口回報", fell:"呼叫失敗，已改用地端",
    empty:"（還沒有日誌）",
    kicker:{ grok:"下一則 Telegram 用這顆", chatgpt:"下一則用 ChatGPT 訂閱", local:"下一則只走地端", auto:"下一則先試地端" },
    unsigned:"沒有 Grok 登入，下一則改走地端",
    unsignedChat:"沒有 ChatGPT 訂閱登入，下一則改走地端",
    deadChat:"ChatGPT 登入過期，請重新登入。下一則改走地端",
    staleChat:"存取憑證過期，送出時會刷新。",
    detail:{
      grok:"Grok Build。這一則失敗才改用地端 {local}。",
      chatgpt:"ChatGPT 訂閱，模型 {chatgpt}。這一則失敗才改用地端 {local}。auto 不會自動改用這顆。",
      local:"只走地端，不會打到 Grok。",
      auto:"地端 {local} 有回應就用它，不通才用 {grok}。"
    },
    modes:[["grok","Grok Build"],["chatgpt","ChatGPT"],["local","只用地端"],["auto","地端優先"]],
    fields:{ compact_at:"聊天超過幾 token 就壓縮", step_ctx:"單步 context 上限", skill_mode:"技能",
      skill_limit:"技能上限", tool_stall:"工具卡死（秒）", fg_wall:"前景牆鐘（秒）",
      planner_temperature:"短 JSON 的溫度" },
    recipe:{ machine:"機器", model:"常駐地端模型", server_context:"伺服器 context", kv:"KV",
      speculative:"投機解碼", harness_compact:"聊天壓縮點", harness_step:"單步上限",
      tool_temperature:"工具溫度", json_temperature:"JSON 溫度",
      grok_fast:"Grok 快速 id", grok_main:"Grok 主模型 id" }
  }
};
let lang = localStorage.getItem("pitg-lang") || "en";
let routeMode = "auto";
const fields = ["compact_at","step_ctx","skill_mode","skill_limit","tool_stall","fg_wall","planner_temperature"];
const recipeKeys = ["machine","model","server_context","kv","speculative","harness_compact","harness_step","tool_temperature","json_temperature","grok_fast","grok_main"];
function L(){ return I18N[lang]; }
const hTune = document.getElementById("h-tune"), hTest = document.getElementById("h-test");
const hRecipe = document.getElementById("h-recipe"), hLog = document.getElementById("h-log");
const save = document.getElementById("save"), run = document.getElementById("run"), langBtn = document.getElementById("lang");
function paint(){
  const t = L();
  document.documentElement.lang = lang === "zh" ? "zh-Hant" : "en";
  sub.textContent = t.sub; hTune.textContent = t.tune; hTest.textContent = t.test;
  hRecipe.textContent = t.recipeTitle; hLog.textContent = t.log;
  testNote.textContent = t.note; tempLab.firstChild.textContent = t.temp + " ";
  save.textContent = t.save; run.textContent = t.run; langBtn.textContent = t.lang;
  const box = document.getElementById("route");
  box.innerHTML = "";
  box.setAttribute("aria-label", t.tune);
  for (const pair of t.modes) {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.mode = pair[0]; b.textContent = pair[1];
    b.setAttribute("aria-pressed", pair[0] === routeMode ? "true" : "false");
    b.onclick = () => choose(pair[0]);
    box.appendChild(b);
  }
}
langBtn.onclick = () => { lang = lang === "en" ? "zh" : "en"; localStorage.setItem("pitg-lang", lang); paint(); build(); showRoute(lastRoute); };
function fill(s, route){ return s.replace("{local}", route.local_model || "").replace("{grok}", route.grok_model || "").replace("{chatgpt}", route.chatgpt_model || ""); }
let lastRoute = null;
function showRoute(route){
  lastRoute = route;
  const t = L();
  if (!route || !route.model) { liveModel.textContent = "—"; heroKicker.textContent = ""; heroDetail.textContent = ""; return; }
  const unsigned = (route.mode === "grok" && !route.signed) || (route.mode === "chatgpt" && !route.chatgpt_signed);
  routeMode = route.mode;
  for (const b of document.querySelectorAll("#route button")) b.setAttribute("aria-pressed", b.dataset.mode === routeMode ? "true" : "false");
  const chatDead = route.mode === "chatgpt" && route.chatgpt_dead;
  heroKicker.textContent = chatDead ? t.deadChat : (unsigned ? (route.mode === "chatgpt" ? t.unsignedChat : t.unsigned) : (t.kicker[route.mode] || ""));
  liveModel.textContent = unsigned || route.provider === "auto" ? (route.local_model || route.model) : route.model;
  const key = unsigned ? "local" : route.mode;
  let detail = fill(t.detail[key] || "", route);
  if (route.mode === "chatgpt" && route.chatgpt_signed && !route.chatgpt_fresh) detail += " " + t.staleChat;
  heroDetail.textContent = detail;
}
function build(){
  const t = L();
  const form = document.getElementById("tune");
  const prev = Object.fromEntries(new FormData(form).entries());
  form.innerHTML = "";
  for (const name of fields) {
    const lab = document.createElement("label");
    lab.textContent = t.fields[name];
    let el;
    if (name === "skill_mode") {
      el = document.createElement("select");
      for (const v of ["ondemand","all","off"]) { const op = document.createElement("option"); op.value = v; op.textContent = v; el.appendChild(op); }
    } else {
      el = document.createElement("input");
      el.type = "number"; el.step = name === "planner_temperature" ? "0.1" : "1";
    }
    el.name = name; el.id = name;
    if (prev[name] != null) el.value = prev[name];
    lab.appendChild(el); form.appendChild(lab);
  }
}
function bodyFromForm(){
  const body = Object.fromEntries(new FormData(document.getElementById("tune")).entries());
  body.provider = routeMode;
  return body;
}
async function load(){
  const s = await (await fetch("/api/state")).json();
  const form = document.getElementById("tune");
  const editing = document.activeElement && (form.contains(document.activeElement) || document.getElementById("route").contains(document.activeElement));
  if (!editing) {
    routeMode = (s.tune && s.tune.provider) || "auto";
    build();
    for (const name of fields) { const el = document.getElementById(name); if (el && s.tune) el.value = s.tune[name]; }
    paint();
  }
  showRoute(s.route || null);
  document.getElementById("warn").textContent = Number(s.tune.compact_at) > 60000 ? L().warn : "";
  const labels = L().recipe;
  document.getElementById("recipe").textContent = recipeKeys.map(k => labels[k] + "  " + (s.recipe ? s.recipe[k] : "")).join("\n");
  document.getElementById("log").textContent = (s.log || []).join("\n") || L().empty;
}
async function postTune(done){
  const r = await fetch("/api/tune", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(bodyFromForm())});
  const d = await r.json();
  document.getElementById("warn").textContent = Number(d.tune.compact_at) > 60000 ? L().warn : "";
  if (done) done(d);
  load();
}
function choose(mode){
  routeMode = mode;
  for (const b of document.querySelectorAll("#route button")) b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
  postTune(() => { document.getElementById("saved").textContent = L().saved; });
}
save.onclick = () => postTune(() => { document.getElementById("saved").textContent = L().saved; });
run.onclick = async () => {
  document.getElementById("out").textContent = "…";
  document.getElementById("proof").textContent = "";
  const r = await fetch("/api/test", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({prompt: document.getElementById("prompt").value, temperature: Number(document.getElementById("temp").value)})});
  const d = await r.json();
  document.getElementById("out").textContent = d.error || d.text || JSON.stringify(d);
  if (d.model) document.getElementById("proof").textContent = (d.fell_back ? L().fell : L().proof) + "  " + d.model;
};
const testNote = document.getElementById("test-note"), tempLab = document.getElementById("temp-lab");
const heroKicker = document.getElementById("hero-kicker"), liveModel = document.getElementById("live-model"), heroDetail = document.getElementById("hero-detail");
paint(); build(); load();
setInterval(load, 8000);
</script>
</body>
</html>
"""


class Dashboard:
    def __init__(self, bridge=None, path: str = "", log_path: str = "", host: str = "127.0.0.1", port: int = 8787):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("dashboard binds to loopback only")
        self.bridge = bridge
        self.path = path or os.path.join(os.environ.get("PITG_HOME", os.path.expanduser("~/.pi-tg")), "state", "tune.json")
        self.log_path = log_path
        self.host, self.port = host, int(port)

    def start(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _host_ok(self):
                host = (self.headers.get("Host") or "").split(":")[0]
                return host in ("127.0.0.1", "localhost", "")

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 100_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode() or "{}")
                except Exception:
                    return {}

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "localhost only"})
                if self.path.split("?", 1)[0] in ("/", "/index.html"):
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if self.path.startswith("/api/state"):
                    tune = load_tune(dash.path)
                    route = {}
                    if dash.bridge is not None and hasattr(dash.bridge, "route_public"):
                        try:
                            route = dash.bridge.route_public()
                        except Exception:
                            route = {}
                    return self._send(200, {"tune": tune, "recipe": RECIPE, "log": tail(dash.log_path), "route": route})
                if self.path.startswith("/api/log"):
                    return self._send(200, {"lines": tail(dash.log_path)})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, {"error": "localhost only"})
                body = self._read()
                if self.path == "/api/tune":
                    data = save_tune(dash.path, body)
                    apply_env(data)
                    if dash.bridge is not None:
                        dash.bridge.env["PITG_PROVIDER"] = data["provider"]
                    return self._send(200, {"tune": data})
                if self.path == "/api/test":
                    return self._send(200, dash.test(str(body.get("prompt") or "")[:2000], body.get("temperature")))
                self._send(404, {"error": "not found"})

        try:
            httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            return None
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def test(self, prompt: str, temperature) -> dict:
        if not prompt.strip():
            return {"error": "empty prompt"}
        if self.bridge is None or not hasattr(self.bridge, "llm_text"):
            return {"error": "no model endpoint configured"}
        try:
            temp = float(temperature) if temperature is not None else 0.7
        except (TypeError, ValueError):
            temp = 0.7
        temp = max(0.0, min(1.5, temp))
        system = "Reply in the same language as the user. One short answer. Do not invent tools or files. If asked your model id, answer with it."
        meta = {}
        if hasattr(self.bridge, "llm_turn"):
            text, meta = self.bridge.llm_turn(system, prompt, max_tokens=200, timeout=60, tier="main", temperature=temp)
        else:
            text = self.bridge.llm_text(system, prompt, max_tokens=200, timeout=60, tier="main", temperature=temp)
        if not text:
            return {"error": "no reply. The log has the provider and model; no secret is written here."}
        return {"text": text, "temperature": temp, "model": meta.get("model") or "", "provider": meta.get("provider") or "", "fell_back": bool(meta.get("fell_back"))}
