#!/usr/bin/env python3
"""Provider resolution, language heuristic, dashboard clamp and loopback API. No network."""
import base64, json, os, sys, tempfile, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
f = P.resolve({"PITG_PROVIDER": "auto", "XAI_API_KEY": "xai-test"}, "fast", False, "spark-fast", "http://127.0.0.1:9", "k")
check("fast tier is grok-4-1-fast-non-reasoning", f["model"] == "grok-4-1-fast-non-reasoning", f)
forced = P.resolve({"PITG_PROVIDER": "local", "XAI_API_KEY": "x"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")
check("local mode never leaves the machine", forced["provider"] == "local" and forced["model"] == "spark-main")
nokey = P.resolve({"PITG_PROVIDER": "grok", "PITG_GROK_SESSION": "0"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")
check("grok without a key falls back to local", nokey["provider"] == "local")
over = P.resolve({"PITG_PROVIDER": "auto", "PI_GROK_MAIN": "grok-4.7", "XAI_API_KEY": "x"}, "main", False, "m", "http://127.0.0.1:9", "")
check("PI_GROK_MAIN overrides the task model", over["model"] == "grok-4.7")
prefer = P.resolve({"PITG_PROVIDER": "grok", "XAI_API_KEY": "x"}, "main", True, "spark-main", "http://127.0.0.1:9", "k")
check("grok mode prefers Grok Build even when local is up", prefer["provider"] == "grok" and prefer["model"] == "grok-build-0.1", prefer)
check("grok root already contains /v1", P.join_url("https://api.x.ai/v1", "/v1/chat/completions") == "https://api.x.ai/v1/chat/completions")
check("local root keeps /v1", P.join_url("http://127.0.0.1:8000", "/v1/chat/completions") == "http://127.0.0.1:8000/v1/chat/completions")
pi_tmp = tempfile.mkdtemp(prefix="pitg-pi-")
pi_file = pi_tmp + "/models.json"
json.dump({"providers": {"spark": {"apiKey": "keep-me", "models": []}}}, open(pi_file, "w"))
P.sync_pi_provider(pi_file, "session-token-test", "https://api.x.ai/v1")
saved = json.load(open(pi_file))
check("pi config keeps the local provider and adds Grok", saved["providers"]["spark"]["apiKey"] == "keep-me" and saved["providers"]["grok"]["models"][1]["id"] == "grok-build-0.1")
check("synced file is not world-readable", oct(os.stat(pi_file).st_mode & 0o777) == "0o600")

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
check("GET / is the page", code == 200 and "pi-task-agent" in body and 'id="live-model"' in body and "Grok Build" in body and "ChatGPT" in body, code)
code, body = get("/api/state", "127.0.0.1")
check("state includes the Grok recipe", code == 200 and "grok-build-0.1" in body and '"route"' in body, body[:180])
code, body = get("/api/state", "evil.example")
check("foreign Host is refused", code == 403, (code, body[:80]))
req = urllib.request.Request(
    f"http://127.0.0.1:{dash.port}/api/tune",
    data=json.dumps({"provider": "grok", "compact_at": 40000}).encode(),
    headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=5) as r:
    saved = json.loads(r.read().decode())
check("POST /api/tune persists grok", saved["tune"]["provider"] == "grok" and json.load(open(path))["provider"] == "grok", saved)
req = urllib.request.Request(
    f"http://127.0.0.1:{dash.port}/api/tune",
    data=json.dumps({"provider": "chatgpt", "compact_at": 40000}).encode(),
    headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=5) as r:
    saved = json.loads(r.read().decode())
check("POST /api/tune persists chatgpt", saved["tune"]["provider"] == "chatgpt" and json.load(open(path))["provider"] == "chatgpt", saved.get("tune", {}).get("provider"))
open(tmp + "/log", "w").write("ok sk-LEAKED\n")
code, body = get("/api/log", "localhost")
check("log endpoint scrubs", code == 200 and "sk-LEAKED" not in body and "ok" in body, body[:120])
httpd.shutdown()

def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")

def make_jwt(exp, account="acct-test"):
    return b64({"alg": "none", "typ": "JWT"}) + "." + b64({"exp": exp, "https://api.openai.com/auth": {"chatgpt_account_id": account}}) + ".sig"

def write_codex(path, access, refresh="refresh-test", account="acct-test"):
    json.dump({"auth_mode": "chatgpt", "tokens": {"access_token": access, "refresh_token": refresh, "account_id": account}}, open(path, "w"))

codex_dir = tempfile.mkdtemp(prefix="pitg-codex-")
codex = codex_dir + "/auth.json"
write_codex(codex, make_jwt(int(time.time()) + 3600))
env = {"PITG_PROVIDER": "chatgpt", "PITG_CODEX_AUTH": codex, "PITG_GROK_SESSION": "0"}
got = P.resolve(env, "main", True, "spark-main", "http://127.0.0.1:9", "k")
check("chatgpt mode uses the subscription model", got["provider"] == "chatgpt" and got["model"] == "gpt-5.6-luna" and got["pi_provider"] == "openai-codex" and got["account"] == "acct-test" and got["base"].endswith("/backend-api"), (got["provider"], got["model"]))
fast = P.resolve(env, "fast", True, "spark-fast", "http://127.0.0.1:9", "k")
check("chatgpt fast tier is the mini model", fast["model"] == "gpt-5.4-mini", fast["model"])
auto = P.resolve({"PITG_PROVIDER": "auto", "PITG_CODEX_AUTH": codex, "PITG_GROK_SESSION": "0"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")
check("auto does not pick chatgpt", auto["provider"] == "local", auto["provider"])
off = P.resolve({**env, "PITG_CHATGPT": "0"}, "main", True, "spark-main", "http://127.0.0.1:9", "k")
check("PITG_CHATGPT=0 stays local", off["provider"] == "local", off["provider"])
missing = P.resolve({"PITG_PROVIDER": "chatgpt", "PITG_CODEX_AUTH": codex + "-missing", "PITG_GROK_SESSION": "0"}, "main", True, "spark-main", "http://127.0.0.1:9", "")
check("chatgpt without a login falls back to local", missing["provider"] == "local", missing["provider"])
check("clamp accepts chatgpt and rejects unknown", D.clamp({"provider": "chatgpt"})["provider"] == "chatgpt" and D.clamp({"provider": "nope"})["provider"] == "local")
check("default provider is local (spark-main), not auto", D.DEFAULTS["provider"] == "local" and P.resolve({"XAI_API_KEY": "xai-test"}, "main", False, "spark-main", "http://127.0.0.1:9", "k")["provider"] == "local")
check("403 before a tool call → auth fallback", P.fallback_reason("grok", "error", '403 "The OAuth2 access token could not be validated."', 0) == "auth")
check("401 on chatgpt → auth fallback", P.fallback_reason("chatgpt", "error", "401 Unauthorized", 0) == "auth")
check("invalid API key (xAI answers 400) → auth fallback", P.fallback_reason("grok", "error", '400 "Incorrect API key provided."', 0) == "auth")
check("refresh token error → auth fallback", P.fallback_reason("chatgpt", "error", "refresh token was already used", 0) == "auth")
check("timeout → timeout fallback (wall or network)", P.fallback_reason("grok", "timeout", "", 0) == "timeout" and P.fallback_reason("grok", "error", "ETIMEDOUT", 0) == "timeout")
check("other error before a tool call falls back as error", P.fallback_reason("grok", "error", "500 upstream", 0) == "error")
check("no fallback after a tool call (side effects)", P.fallback_reason("grok", "error", "403", 2) == "" and P.fallback_reason("grok", "timeout", "", 3) == "")
check("no fallback for local, a normal answer, or empty", P.fallback_reason("local", "error", "403", 0) == "" and P.fallback_reason("grok", "final", "ok", 0) == "" and P.fallback_reason("grok", "empty", "", 0) == "")
auth_dir = tempfile.mkdtemp(prefix="pitg-piauth-")
authp = auth_dir + "/auth.json"
json.dump({"providers": {"spark": {"apiKey": "keep"}}}, open(auth_dir + "/models.json", "w"))
P.sync_pi_codex(authp, got)
saved_auth = json.load(open(authp))
check("pi auth stores oauth and leaves models.json alone", saved_auth["openai-codex"]["type"] == "oauth" and saved_auth["openai-codex"]["accountId"] == "acct-test" and "keep" in open(auth_dir + "/models.json").read())
check("pi auth is not world-readable", oct(os.stat(authp).st_mode & 0o777) == "0o600")

class Refresh(BaseHTTPRequestHandler):
    ok = False
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode()
        Refresh.ok = "grant_type=refresh_token" in body and "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in body and "refresh-old" in body
        payload = json.dumps({"access_token": make_jwt(int(time.time()) + 7200), "refresh_token": "refresh-new", "expires_in": 7200}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *a):
        pass

token_srv = ThreadingHTTPServer(("127.0.0.1", 0), Refresh)
threading.Thread(target=token_srv.serve_forever, daemon=True).start()
stale = tempfile.mkdtemp(prefix="pitg-stale-") + "/auth.json"
write_codex(stale, make_jwt(int(time.time()) - 10), refresh="refresh-old")
P._chatgpt_block.pop(stale, None)
view = P.chatgpt_view({"PITG_CODEX_AUTH": stale})
untouched = json.load(open(stale))["tokens"]["refresh_token"]
check("dashboard poll sees the login and does not refresh", view["signed"] is True and view["fresh"] is False and untouched == "refresh-old", view)
refreshed = P.resolve({"PITG_PROVIDER": "chatgpt", "PITG_CODEX_AUTH": stale, "PITG_CHATGPT_TOKEN_URL": f"http://127.0.0.1:{token_srv.server_address[1]}", "PITG_GROK_SESSION": "0"}, "main", True, "spark-main", "http://127.0.0.1:9", "")
disk = json.load(open(stale))
check("expired subscription refreshes", Refresh.ok and refreshed["provider"] == "chatgpt" and disk["tokens"]["refresh_token"] == "refresh-new" and bool(disk.get("last_refresh")) and oct(os.stat(stale).st_mode & 0o777) == "0o600", refreshed["provider"])
token_srv.shutdown()

class Reject(BaseHTTPRequestHandler):
    n = 0
    def do_POST(self):
        Reject.n += 1
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        raw = b'{"error":{"message":"used"}}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def log_message(self, *a):
        pass

rej = ThreadingHTTPServer(("127.0.0.1", 0), Reject)
threading.Thread(target=rej.serve_forever, daemon=True).start()
dead_auth = tempfile.mkdtemp(prefix="pitg-dead-") + "/auth.json"
write_codex(dead_auth, make_jwt(int(time.time()) - 10), refresh="refresh-dead")
dead_state = tempfile.mkdtemp(prefix="pitg-deadstate-") + "/chatgpt.json"
P._chatgpt_block.pop(dead_auth, None)
dead_env = {"PITG_PROVIDER": "chatgpt", "PITG_CODEX_AUTH": dead_auth, "PITG_CHATGPT_STATE": dead_state, "PITG_CHATGPT_TOKEN_URL": f"http://127.0.0.1:{rej.server_address[1]}", "PITG_GROK_SESSION": "0"}
first = P.resolve(dead_env, "main", True, "spark-main", "http://127.0.0.1:9", "")
second = P.resolve(dead_env, "main", True, "spark-main", "http://127.0.0.1:9", "")
marked = json.load(open(dead_state))
view = P.chatgpt_view(dead_env)
check("rejected refresh is remembered and not retried", first["provider"] == "local" and second["provider"] == "local" and Reject.n == 1 and marked.get("refresh_failed") is True and view["dead"] is True and view["signed"] is False, (first["provider"], Reject.n, view))
rej.shutdown()
text, _model = P.codex_reply('event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"好"}\n\n')
check("codex stream yields the text", text == "好", text)
body = P.codex_body("gpt-5.4-mini", "sys", "hi", 200)
check("codex request is the responses shape", body["stream"] is True and body["store"] is False and body["model"] == "gpt-5.4-mini" and body["max_output_tokens"] >= 1024, body.get("model"))
hdr = P.codex_headers("acct-test", "token-test")
check("codex headers name the subscription", hdr["chatgpt-account-id"] == "acct-test" and hdr["originator"] == "pi" and hdr["Authorization"].startswith("Bearer "), hdr["originator"])

old_state, old_chat, old_data = B.STATE_DIR, B.CHAT_CWD, B.DATA
old_tune, old_provider = os.environ.get("PITG_TUNE"), os.environ.get("PITG_PROVIDER")
box = tempfile.mkdtemp(prefix="pitg-route-")
B.STATE_DIR, B.CHAT_CWD, B.DATA = box + "/state", box + "/chat", box
os.makedirs(B.STATE_DIR)
json.dump({"provider": "chatgpt"}, open(box + "/tune.json", "w"))
os.environ["PITG_TUNE"] = box + "/tune.json"
try:
    br = B.Bridge(type("T", (), {})(), {"OWNER_ID": "1", "SPARK_ENV": "/dev/null", "PITG_GROK_SESSION": "0", "PITG_CODEX_AUTH": codex}, B.State(box + "/s.json"))
    route = br.route_public()
    check("route names chatgpt without a network call", route["mode"] == "chatgpt" and route["provider"] == "chatgpt" and route["model"] == "gpt-5.6-luna" and route["chatgpt_signed"] is True and route["chatgpt_fresh"] is True, (route.get("mode"), route.get("provider"), route.get("model")))
    note = br.turn_model_note({"provider": "chatgpt", "model": "gpt-5.6-luna"})
    check("turn note names the chatgpt id", "gpt-5.6-luna" in note and "ChatGPT" in note, note)
finally:
    B.STATE_DIR, B.CHAT_CWD, B.DATA = old_state, old_chat, old_data
    if old_tune is None:
        os.environ.pop("PITG_TUNE", None)
    else:
        os.environ["PITG_TUNE"] = old_tune
    if old_provider is None:
        os.environ.pop("PITG_PROVIDER", None)
    else:
        os.environ["PITG_PROVIDER"] = old_provider

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
