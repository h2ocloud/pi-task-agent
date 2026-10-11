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
PROVIDERS = ("auto", "local", "grok")

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
  :root { color-scheme: light; --bg:#f4f1ea; --ink:#1c1915; --muted:#6b645c; --line:#d9d2c5; --card:#fffdf8; --accent:#9a3412; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.45 "Iowan Old Style", Palatino, "Songti TC", serif; background:var(--bg); color:var(--ink); }
  header { display:flex; justify-content:space-between; gap:16px; align-items:end; padding:22px 28px 8px; }
  h1 { font-size:28px; margin:0; letter-spacing:-.03em; }
  h2 { font-size:16px; margin:0 0 10px; }
  button, select, input { font:inherit; }
  button { background:var(--ink); color:var(--bg); border:0; border-radius:999px; padding:6px 14px; cursor:pointer; }
  button.ghost { background:transparent; color:var(--ink); border:1px solid var(--line); }
  main { display:grid; grid-template-columns: 1.1fr .9fr; gap:16px; padding:8px 28px 32px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px 18px; }
  .nums { display:grid; grid-template-columns:1fr 1fr; gap:8px 16px; }
  label { display:flex; flex-direction:column; gap:4px; font-size:13px; color:var(--muted); }
  input, select, textarea { border:1px solid var(--line); border-radius:8px; padding:7px 8px; background:#fff; color:var(--ink); }
  textarea { width:100%; min-height:88px; }
  pre { white-space:pre-wrap; word-break:break-word; background:#241c16; color:#f6efe4; border-radius:12px; padding:12px; max-height:320px; overflow:auto; font:12px/1.4 ui-monospace, monospace; }
  .warn { color:var(--accent); font-size:13px; }
  @media (max-width:800px) { main { grid-template-columns:1fr; padding:8px 14px 24px; } }
</style>
</head>
<body>
<header>
  <div>
    <h1 id="title">Harness bench</h1>
    <p id="sub" style="margin:4px 0 0;color:var(--muted)"></p>
  </div>
  <button class="ghost" id="lang" type="button">繁體中文</button>
</header>
<main>
  <section>
    <h2 id="h-tune">Tune</h2>
    <form id="tune" class="nums"></form>
    <p class="warn" id="warn"></p>
    <p><button id="save" type="button">Save</button> <span id="saved"></span></p>
  </section>
  <section>
    <h2 id="h-recipe">Measured setup</h2>
    <pre id="recipe"></pre>
  </section>
  <section>
    <h2 id="h-test">Test</h2>
    <textarea id="prompt">Reply in one sentence: what is 2+2?</textarea>
    <p><label>temperature <input id="temp" type="number" min="0" max="1.5" step="0.1" value="0.7"/></label></p>
    <p><button id="run" type="button">Run</button></p>
    <pre id="out"></pre>
  </section>
  <section>
    <h2 id="h-log">Log</h2>
    <pre id="log"></pre>
  </section>
</main>
<script>
const I18N = {
  en: { title:"Harness bench", sub:"localhost only · does not load a second model",
    tune:"Tune", recipe:"Measured setup", test:"Test", log:"Log", save:"Save", run:"Run", saved:"saved",
    lang:"繁體中文", warn:"Above 60k tokens this model starts to drift. Compact earlier.",
    fields:{ compact_at:"Compact chat after (tokens)", step_ctx:"Step context cap", skill_mode:"Skills",
      skill_limit:"Skill cap", tool_stall:"Stuck tool (seconds)", fg_wall:"Foreground wall (seconds)",
      planner_temperature:"JSON helper temperature", provider:"Provider" } },
  zh: { title:"調參台", sub:"只聽本機 · 不會另外載一顆模型",
    tune:"微調", recipe:"實測配方", test:"測試", log:"日誌", save:"儲存", run:"送出", saved:"已存",
    lang:"English", warn:"超過 6 萬 token，這顆模型容易跑偏。提早壓縮。",
    fields:{ compact_at:"聊天超過幾 token 就壓縮", step_ctx:"單步 context 上限", skill_mode:"技能",
      skill_limit:"技能上限", tool_stall:"工具卡死（秒）", fg_wall:"前景牆鐘（秒）",
      planner_temperature:"短 JSON 的溫度", provider:"接口" } }
};
let lang = localStorage.getItem("pitg-lang") || "en";
const fields = ["compact_at","step_ctx","skill_mode","skill_limit","tool_stall","fg_wall","planner_temperature","provider"];
function L(){ return I18N[lang]; }
function paint(){
  const t = L();
  document.documentElement.lang = lang === "zh" ? "zh-Hant" : "en";
  title.textContent = t.title; sub.textContent = t.sub; hTune.textContent = t.tune;
  hRecipe.textContent = t.recipe; hTest.textContent = t.test; hLog.textContent = t.log;
  save.textContent = t.save; run.textContent = t.run; langBtn.textContent = t.lang;
}
const hTune = document.getElementById("h-tune"), hRecipe = document.getElementById("h-recipe");
const hTest = document.getElementById("h-test"), hLog = document.getElementById("h-log");
const save = document.getElementById("save"), run = document.getElementById("run"), langBtn = document.getElementById("lang");
langBtn.onclick = () => { lang = lang === "en" ? "zh" : "en"; localStorage.setItem("pitg-lang", lang); paint(); build(); };
function build(){
  const t = L();
  const form = document.getElementById("tune");
  const prev = Object.fromEntries(new FormData(form).entries());
  form.innerHTML = "";
  for (const name of fields) {
    const lab = document.createElement("label");
    lab.textContent = t.fields[name];
    let el;
    if (name === "skill_mode" || name === "provider") {
      el = document.createElement("select");
      const opts = name === "provider" ? ["auto","local","grok"] : ["ondemand","all","off"];
      for (const o of opts) { const op = document.createElement("option"); op.value = o; op.textContent = o; el.appendChild(op); }
    } else {
      el = document.createElement("input");
      el.type = "number"; el.step = name === "planner_temperature" ? "0.1" : "1";
    }
    el.name = name; el.id = name;
    if (prev[name] != null) el.value = prev[name];
    lab.appendChild(el); form.appendChild(lab);
  }
}
async function load(){
  const s = await (await fetch("/api/state")).json();
  const form = document.getElementById("tune");
  const editing = document.activeElement && form.contains(document.activeElement);
  if (!editing) {
    build();
    for (const name of fields) { const el = document.getElementById(name); if (el) el.value = s.tune[name]; }
  }
  document.getElementById("warn").textContent = Number(s.tune.compact_at) > 60000 ? L().warn : "";
  document.getElementById("recipe").textContent = JSON.stringify(s.recipe, null, 2);
  document.getElementById("log").textContent = (s.log || []).join("\n") || "(no log file yet)";
}
save.onclick = async () => {
  const body = Object.fromEntries(new FormData(document.getElementById("tune")).entries());
  const r = await fetch("/api/tune", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const d = await r.json();
  document.getElementById("saved").textContent = L().saved;
  document.getElementById("warn").textContent = Number(d.tune.compact_at) > 60000 ? L().warn : "";
};
run.onclick = async () => {
  document.getElementById("out").textContent = "…";
  const r = await fetch("/api/test", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({prompt: document.getElementById("prompt").value, temperature: Number(document.getElementById("temp").value)})});
  const d = await r.json();
  document.getElementById("out").textContent = d.error || d.text || JSON.stringify(d);
};
paint(); load();
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
                    return self._send(200, {"tune": tune, "recipe": RECIPE, "log": tail(dash.log_path)})
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
        text = self.bridge.llm_text(
            "Reply in the same language as the user. One short answer. Do not invent tools or files.",
            prompt, max_tokens=200, timeout=60, tier="fast", temperature=temp)
        if not text:
            return {"error": "no reply. Check the local server, or set XAI_API_KEY for Grok."}
        return {"text": text, "temperature": temp}
