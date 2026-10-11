#!/usr/bin/env python3
"""Provider resolution, language heuristic, dashboard clamp and loopback API. No network."""
import json, os, sys, tempfile, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D
import lang
import providers as P
import pi_tg_bridge as B

fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:200]))
    if not cond:
        fails.append(name)

check("auto + local up stays local", P.resolve({"PITG_PROVIDER": "auto", "XAI_API_KEY": "x"}, "main", True, "spark-main", "http://127.0.0.1:9", "k")["provider"] == "local")
g = P.resolve({"PITG_PROVIDER": "auto", "XAI_API_KEY": "xai-test"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")
check("auto + local down uses Grok Build", g["provider"] == "grok" and g["model"] == "grok-build-0.1" and g["base"] == "https://api.x.ai/v1", g)
f = P.resolve({"XAI_API_KEY": "xai-test"}, "fast", False, "spark-fast", "http://127.0.0.1:9", "k")
check("fast tier is grok-4-1-fast-non-reasoning", f["model"] == "grok-4-1-fast-non-reasoning", f)
forced = P.resolve({"PITG_PROVIDER": "local", "XAI_API_KEY": "x"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")
check("local mode never leaves the machine", forced["provider"] == "local" and forced["model"] == "spark-main")
nokey = P.resolve({"PITG_PROVIDER": "grok"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")
check("grok without a key falls back to local", nokey["provider"] == "local")
over = P.resolve({"PI_GROK_MAIN": "grok-4.7", "XAI_API_KEY": "x"}, "main", False, "m", "http://127.0.0.1:9", "")
check("PI_GROK_MAIN overrides the task model", over["model"] == "grok-4.7")

check("Chinese text is zh", lang.detect("幫我查一下這個") == "zh")
check("English text is en", lang.detect("check this file") == "en")
check("slash fallback can be English", lang.t("en", "owner_only") == "Only the owner can use this.")
check("Chinese owner string kept", "只有擁有者" in lang.t("zh", "owner_only"))
check("chat prompt is not Chinese-only", "預設繁體中文" not in B.CHAT_ONLY_PROMPT and "Do not default to Chinese" in B.CHAT_ONLY_PROMPT)

c = D.clamp({"compact_at": 999999, "skill_mode": "nope", "provider": "grok", "planner_temperature": 9, "skill_limit": "2"})
check("clamp bounds", c["compact_at"] == 120000 and c["skill_mode"] == "ondemand" and c["provider"] == "grok" and c["planner_temperature"] == 1.5 and c["skill_limit"] == 2, c)
secret = D.scrub("Authorization: Bearer thk_live_abc sk-secret xai-123 api_key=zzz")
check("log scrub", "thk_live_abc" not in secret and "sk-secret" not in secret and "***" in secret, secret)

tmp = tempfile.mkdtemp(prefix="pitg-dash-")
path = tmp + "/tune.json"
dash = D.Dashboard(bridge=None, path=path, log_path=tmp + "/log", host="127.0.0.1", port=0)
httpd = dash.start()
check("dashboard binds an ephemeral port", httpd is not None and dash.port > 0, dash.port)
try:
    D.Dashboard(host="0.0.0.0")
    check("non-loopback bind refused", False)
except ValueError:
    check("non-loopback bind refused", True)

def get(path_q, host):
    req = urllib.request.Request(f"http://127.0.0.1:{dash.port}{path_q}", headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

code, body = get("/", f"127.0.0.1:{dash.port}")
check("GET / is the page", code == 200 and "pi-task-agent" in body, code)
code, body = get("/api/state", "127.0.0.1")
check("state includes the Grok recipe", code == 200 and "grok-build-0.1" in body, body[:160])
code, body = get("/api/state", "evil.example")
check("foreign Host is refused", code == 403, (code, body[:80]))
req = urllib.request.Request(
    f"http://127.0.0.1:{dash.port}/api/tune",
    data=json.dumps({"provider": "grok", "compact_at": 40000}).encode(),
    headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=5) as r:
    saved = json.loads(r.read().decode())
check("POST /api/tune persists grok", saved["tune"]["provider"] == "grok" and json.load(open(path))["provider"] == "grok", saved)
open(tmp + "/log", "w").write("ok sk-LEAKED\n")
code, body = get("/api/log", "localhost")
check("log endpoint scrubs", code == 200 and "sk-LEAKED" not in body and "ok" in body, body[:120])
httpd.shutdown()

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
