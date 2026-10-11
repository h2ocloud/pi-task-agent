#!/usr/bin/env python3
"""Telegram <-> pi 橋接（取代 OpenClaw 的 Telegram 通道）。純 stdlib。

分三層，各管一件事：
  pi_tg_bridge.py  通訊與調度：收訊息、先回、分流（補充／無關／要問）、斜線指令、重啟接續、離線排隊
  orchestrator.py  把較大的事自主做完：規劃 → 小步執行（每步全新 session）→ 驗收 → 太大就遞迴拆 → 做不到就退階
  pirun.py         執行一次 pi：單一工具卡死、重複空轉、輪數、牆鐘四道防線

體驗原則：先回再做 · 工作不卡住對話 · 少打擾但不漏 · 慢沒關係只要做完 · 該自己解決的自己解決，只有真的需要他才問。
斜線指令不經模型、即時生效。技能按需載入（skills.py），不要把整個技能目錄塞進每一次對話。
"""
from __future__ import annotations

import collections
import fcntl
import glob
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import pirun
from context import Compactor
from line import LineChannel, LineClient, Router, WebhookServer
from orchestrator import Orchestrator, TRANSIENT
from pirun import short
from skills import format_list, skills_for
from lang import RULE, default_lang, detect, t
from providers import grok_key, resolve

HOME = os.path.expanduser("~")
CFG_DIR = os.environ.get("PITG_CFG_DIR", HOME + "/.config/pi-tg-bridge")
DATA = os.environ.get("PITG_HOME", HOME + "/.pi-tg")
STATE_DIR = DATA + "/state"
MEDIA_DIR = DATA + "/media"
SESSIONS_DIR = DATA + "/sessions"
SKILLS_DIR = DATA + "/skills"
CHAT_CWD = DATA + "/chat"          # 唯讀聊天模式的空工作目錄（不載入 AGENTS.md）

COMPACT_AT = int(os.environ.get("PITG_COMPACT_AT", "40000"))     # 聊天 session 上下文超過這麼多 token → 自動壓縮
PROMOTE_AT = 3                     # 第 3 輪工具開始＝超過兩輪 → 交給 orchestrator 當背景工作
MAX_RUNS = int(os.environ.get("PITG_MAX_RUNS", "4"))                # 同時跑的 pi 數（後端單序列，更多只會互相拖慢）
FG_WALL = int(os.environ.get("PITG_FG_WALL", "600"))                # 前景一則在到達第 3 輪前的牆鐘上限（超過一樣轉背景，不砍）
TOOL_STALL = int(os.environ.get("PITG_TOOL_STALL", "180"))
RETRY_WAITS = (15, 30, 60)         # 暫時性錯誤的重試間隔
SESSION_MAX_BYTES = 2_000_000      # session 檔過大就自動開新的（本地模型 context 有限）
TYPING_EVERY = 4
POLL_TIMEOUT = 50
ACTIVE = ("開始了", "還在跑")

BASE_PRESETS = {                   # /model 可選。別名對到你在 pi 裡設定的 provider 模型，不要寫死某台機器。
    "main": "spark-main", "fast": "spark-fast", "think": "spark-think", "vision": "spark-vision",
}
PRESETS = dict(BASE_PRESETS)


def apply_presets(env: dict):
    """環境變數覆寫預設別名。PI_MODELS=name=alias,name2=alias2 用來加公開程式沒列的別名。"""
    PRESETS.clear()
    PRESETS.update(BASE_PRESETS)
    for key, envk in (("main", "PI_MODEL_MAIN"), ("fast", "PI_MODEL_FAST"), ("think", "PI_MODEL_THINK"), ("vision", "PI_MODEL_VISION")):
        if env.get(envk):
            PRESETS[key] = env[envk].strip()
    for item in (env.get("PI_MODELS") or "").split(","):
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            PRESETS[k] = v
STATUS_RE = re.compile(r"(在忙什麼|在做什麼|在忙啥|進度|做到哪|有什麼在跑|有在跑什麼|還在跑嗎|好了嗎|完成了嗎|跑到哪)")
STOP_RE = re.compile(r"(先停|停掉|停止|取消|別做了|不用做了|不要做了|算了)")

CHAT_ONLY_PROMPT = (
    "You are the assistant for this chat. Text chat and general questions only. "
    "You have no tools. You cannot read or write files or use the network. "
    "Be brief. Do not reveal the owner's private information (schedule, accounts, family, servers). "
    "If asked, say only the owner can ask about those. "
    "Do not claim you finished something you did not do. Do not invent paths, hosts, or numbers. "
    + RULE
)
BOT_COMMANDS = [
    {"command": "jobs", "description": "背景工作清單與進度"},
    {"command": "new", "description": "開新對話（清掉上下文）"},
    {"command": "compact", "description": "壓縮上下文（保留大方向）"},
    {"command": "status", "description": "模型／思考／背景工作／後端狀態"},
    {"command": "stop", "description": "中止目前處理或指定的背景工作"},
    {"command": "resume", "description": "接續被擋住/被停止的工作"},
    {"command": "model", "description": "查看或切換模型別名"},
    {"command": "thinking", "description": "思考模式 on/off"},
    {"command": "skills", "description": "列出可用 skills"},
    {"command": "help", "description": "指令清單"},
]
HELP_TEXT = (
    "指令（不經模型，即時生效）\n"
    "/jobs  背景工作清單與進度（也可直接問「現在在忙什麼」）\n"
    "/stop [名稱]  中止目前處理，或只停指定的那一件\n"
    "/resume <名稱>  接續被擋住/被停止的工作\n"
    "/new（/reset）  開新對話（連前情摘要一起清）\n"
    "/compact  壓縮上下文，保留大方向（超過門檻我也會自己壓）\n"
    "/status  模型／思考／背景工作／後端狀態\n"
    "/model  看目前；/model list；/model <名稱> 切換\n"
    "/thinking on|off（/think）  思考模式\n"
    "/skills  可用 skills\n"
    "/help  這份清單"
)

log = logging.getLogger("pi-tg")


# ---------------------------------------------------------------- config / state / registry
def load_env(path: str) -> dict:
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


class State:
    """小型 JSON 狀態檔：模型別名、思考、各 chat 的 session 序號。"""

    def __init__(self, path: str):
        self.path, self.lock = path, threading.Lock()
        self.d = {"model": "main", "thinking": False, "session_n": {}, "summary": {}}
        try:
            self.d.update(json.load(open(path)))
        except Exception:
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def alias(self) -> str:
        return PRESETS.get(self.d["model"], "spark-main")

    def sid(self, chat_id: int) -> str:
        return f"tg-{chat_id}-{self.d['session_n'].get(str(chat_id), 0)}"

    def new_session(self, chat_id: int) -> str:
        with self.lock:
            self.d["session_n"][str(chat_id)] = self.d["session_n"].get(str(chat_id), 0) + 1
            self.save()
        return self.sid(chat_id)


class Jobs:
    """任務清單（磁碟檔）：一筆＝使用者交代的一件背景工作，連同計畫與各步結果。
    狀態只用 開始了／還在跑／做完／被擋住；被擋住且 awaiting＝在等使用者回答。"""

    def __init__(self, path: str):
        self.path, self.lock, self.items = path, threading.RLock(), []
        try:
            self.items = json.load(open(path))
        except Exception:
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([{k: v for k, v in j.items() if not k.startswith("_")} for j in self.items], f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, chat_id: int, name: str, goal: str, sid: str) -> dict:
        with self.lock:
            base, n = name, 1
            while any(j["name"] == name and j["chat_id"] == chat_id and j["status"] != "做完" for j in self.items):
                n += 1
                name = f"{base}{n}"
            now = time.time()
            job = {"id": uuid.uuid4().hex[:6], "name": name, "goal": goal, "chat_id": chat_id, "sid": sid,
                   "status": "開始了", "note": "", "started": now, "updated": now, "finished": None,
                   "result": "", "surfaced": False, "plan": [], "cursor": None, "resumes": 0, "awaiting": False}
            self.items.append(job)
            self.items = self.items[-60:]
            self.save()
            return job

    def update(self, job: dict, **kw):
        with self.lock:
            job.update(kw)
            job["updated"] = time.time()
            if kw.get("status") in ("做完", "被擋住"):
                job["finished"] = time.time()
            self.save()

    def of(self, chat_id: int, statuses=None, awaiting=None):
        with self.lock:
            return [j for j in self.items if j["chat_id"] == chat_id
                    and (statuses is None or j["status"] in statuses)
                    and (awaiting is None or bool(j.get("awaiting")) == awaiting)]

    def find(self, chat_id: int, text: str, pool=None):
        pool = pool if pool is not None else self.of(chat_id)
        text = (text or "").strip().strip("『』「」")
        for j in reversed(pool):
            if j["name"] == text:
                return j
        for j in reversed(pool):
            if text and (text in j["name"] or j["name"] in text):
                return j
        return None


# ---------------------------------------------------------------- telegram transport
class Telegram:
    def __init__(self, token: str, outbox: str | None = None):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.outbox = outbox
        self.mu = threading.Lock()

    def call(self, method: str, _t: int = 30, **params):
        data = json.dumps(params).encode() if params else None
        req = urllib.request.Request(f"{self.base}/{method}", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_t) as r:
            return json.loads(r.read().decode())

    def _send_one(self, p: dict) -> bool:
        for wait in (0, 2, 6):
            time.sleep(wait)
            try:
                self.call("sendMessage", **p)
                return True
            except urllib.error.HTTPError as e:
                if e.code == 400 and "reply" in str(getattr(e, "reason", "")).lower() + e.read().decode()[:200].lower():
                    p = {k: v for k, v in p.items() if not k.startswith(("reply_to", "allow_sending"))}
                    continue
            except Exception:
                pass
        return False

    def send(self, chat_id: int, text: str, reply_to: int | None = None, essential: bool = True):
        text = text or "（沒有產生回覆）"
        chunks, cur = [], ""
        for line in text.split("\n"):
            while len(line) > 4000:
                if cur:
                    chunks.append(cur); cur = ""
                chunks.append(line[:4000]); line = line[4000:]
            if len(cur) + len(line) + 1 > 4000:
                chunks.append(cur); cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        for i, c in enumerate(chunks):
            p = {"chat_id": chat_id, "text": c}
            if reply_to and i == 0:
                p["reply_to_message_id"] = reply_to
                p["allow_sending_without_reply"] = True
            if not self._send_one(p):                     # 退階：送不出去就先存起來，網路恢復後補送，結果不會丟
                log.warning("sendMessage failed 3x, parked in outbox")
                self._park({"chat_id": chat_id, "text": c})

    def _park(self, p: dict):
        if self.outbox:
            with self.mu, open(self.outbox, "a") as f:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

    def flush_outbox(self):
        if not self.outbox or not os.path.exists(self.outbox):
            return
        with self.mu:
            try:
                rows = [json.loads(l) for l in open(self.outbox) if l.strip()]
                os.remove(self.outbox)
            except Exception:
                return
        for p in rows:
            if not self._send_one(p):
                self._park(p)

    def typing(self, chat_id: int):
        try:
            self.call("sendChatAction", _t=10, chat_id=chat_id, action="typing")
        except Exception:
            pass

    def download(self, file_id: str, suffix: str = "") -> str | None:
        try:
            info = self.call("getFile", file_id=file_id)["result"]
            url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
            ext = os.path.splitext(info["file_path"])[1] or suffix
            os.makedirs(MEDIA_DIR, exist_ok=True)
            dst = f"{MEDIA_DIR}/{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
            with urllib.request.urlopen(url, timeout=60) as r, open(dst, "wb") as f:
                f.write(r.read())
            return dst
        except Exception:
            log.exception("download failed")
            return None


def transcribe(path: str, asr_url: str) -> str | None:
    """POST {ASR_URL}/transcribe，multipart 欄位 audio → {"text":...}。沒設 URL 就回 None。"""
    if not asr_url or not path:
        return None
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(asr_url.rstrip("/") + "/transcribe", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return (json.loads(r.read().decode()).get("text") or "").strip()
    except Exception:
        log.exception("ASR failed")
        return None


# ---------------------------------------------------------------- the bridge
class Bridge:
    log = log

    def __init__(self, tg: Telegram, env: dict, state: State, jobs: Jobs | None = None):
        self.tg, self.env, self.state = tg, env, state
        self.jobs = jobs or Jobs(STATE_DIR + "/jobs.json")
        self.owner = int(env["OWNER_ID"])
        self.chat_only = {int(x) for x in env.get("CHAT_ONLY_USERS", "").split(",") if x.strip()}
        self.groups = {int(x) for x in env.get("GROUPS", "").split(",") if x.strip()}
        self.line_owner = env.get("LINE_OWNER_ID", "")
        self.line_chat_only = {x.strip() for x in env.get("LINE_CHAT_ONLY_USERS", "").split(",") if x.strip()}
        apply_presets(env)
        self.pi = env.get("PI_BIN", "/opt/homebrew/bin/pi")
        self.provider = env.get("PI_PROVIDER", "spark")
        self.asr = (env.get("ASR_URL") or "").strip()
        spark = load_env(env.get("SPARK_ENV", HOME + "/.config/spark-home/api.env"))
        self.spark_base, self.spark_key = spark.get("SPARK_HOME_BASE", ""), spark.get("SPARK_HOME_KEY", "")
        self.username = ""
        self.started = time.time()
        self.slots = threading.Semaphore(MAX_RUNS)
        self.orch = Orchestrator(self)
        self.cx = Compactor(self)
        self.chat_locks: dict[int, threading.Lock] = {}
        self.fg: dict[int, pirun.Handle] = {}       # chat id -> 前景那一則的 handle（/stop 用）
        self.pending: dict[int, dict] = {}          # chat id -> 等使用者回「加/新」的那則
        self.deferred: list[dict] = []              # 後端離線時先記下的訊息，恢復後自動處理
        self.mu = threading.RLock()
        self.last_turn = ""
        os.makedirs(CHAT_CWD, exist_ok=True)
        try:
            self.deferred = json.load(open(STATE_DIR + "/deferred.json"))
        except Exception:
            pass

    # ------------------------------------------------------------ orchestrator's env interface
    def send(self, chat_id, text, reply_to=None, essential=True):
        self.tg.send(chat_id, text, reply_to, essential=essential)

    def lang_for(self, chat_id, text: str = "") -> str:
        saved = self.state.d.setdefault("lang", {})
        if text and not str(text).startswith("/"):
            det = detect(text)
            if saved.get(str(chat_id)) != det:
                saved[str(chat_id)] = det
                self.state.save()
            return det
        return saved.get(str(chat_id)) or default_lang()

    def tune(self) -> dict:
        from dashboard import DEFAULTS, apply_env, clamp, load_tune
        path = os.environ.get("PITG_TUNE") or (STATE_DIR + "/tune.json")
        if os.path.isfile(path):
            data = load_tune(path)
            apply_env(data)
            self.env["PITG_PROVIDER"] = data["provider"]
            return data
        return clamp({
            "compact_at": os.environ.get("PITG_COMPACT_AT", DEFAULTS["compact_at"]),
            "step_ctx": os.environ.get("PITG_STEP_CTX", DEFAULTS["step_ctx"]),
            "skill_mode": os.environ.get("PITG_SKILLS", "ondemand"),
            "skill_limit": os.environ.get("PITG_SKILL_LIMIT", 3),
            "tool_stall": os.environ.get("PITG_TOOL_STALL", TOOL_STALL),
            "fg_wall": os.environ.get("PITG_FG_WALL", FG_WALL),
            "planner_temperature": os.environ.get("PITG_PLAN_TEMP", DEFAULTS["planner_temperature"]),
            "provider": os.environ.get("PITG_PROVIDER", "auto"),
        })

    def _local_up_now(self) -> bool:
        now = time.time()
        if now - getattr(self, "_ping_at", 0) < 20:
            return bool(getattr(self, "_local_up", False))
        self._ping_at = now
        self._local_up = self._ping_local()
        return self._local_up

    def _ping_local(self) -> bool:
        if not self.spark_base:
            return False
        try:
            self._post_to({"base": self.spark_base, "key": self.spark_key}, "/v1/chat/completions",
                          {"model": PRESETS.get("fast", PRESETS.get("main", "")), "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}, 5)
            return True
        except Exception:
            return False

    def _endpoint(self, tier: str) -> dict:
        data = self.tune()
        local_model = PRESETS.get("fast" if tier == "fast" else "main", "")
        if tier == "main":
            local_model = self.state.alias()
        self.env["PITG_PROVIDER"] = data["provider"]
        return resolve(self.env, tier, self._local_up_now(), local_model, self.spark_base, self.spark_key)

    def _post_to(self, ep: dict, path: str, body: dict, timeout: int):
        headers = {"Content-Type": "application/json"}
        if ep.get("key"):
            headers["Authorization"] = f"Bearer {ep['key']}"
        req = urllib.request.Request(ep["base"].rstrip("/") + path, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def _asr_fail(self, chat_id=None) -> str:
        lang = self.lang_for(chat_id) if chat_id is not None else default_lang()
        return t(lang, "asr_unset" if not self.asr else "asr_fail")

    def run_pi(self, job, sid, prompt, handle, max_rounds, wall, max_ctx=0):
        data = self.tune()
        ep = self._endpoint("main")
        provider = ep["pi_provider"] if ep["provider"] == "grok" else self.provider
        model = ep["model"] if ep["provider"] == "grok" else self.state.alias()
        spec = pirun.Spec(self.pi, provider, model, sid, prompt, SESSIONS_DIR, DATA,
                          skills=skills_for(prompt, SKILLS_DIR),
                          max_rounds=max_rounds, wall=wall, stall=int(data["tool_stall"]), max_ctx=max_ctx)
        with self.slots:
            return pirun.run(spec, handle)

    def _post(self, path: str, body: dict, timeout: int):
        req = urllib.request.Request(self.spark_base.rstrip("/") + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.spark_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def llm_text(self, system: str, user: str, max_tokens: int = 300, timeout: int = 90, tier: str = "fast", temperature: float | None = None) -> str | None:
        """One short completion, no tools. fast = judgment. main = planning and the final report."""
        ep = self._endpoint(tier)
        if not ep.get("base"):
            return None
        if temperature is None:
            temperature = 0.2 if tier == "fast" else float(self.tune()["planner_temperature"])
        try:
            d = self._post_to(ep, "/v1/chat/completions", {"model": ep["model"], "temperature": temperature, "max_tokens": max_tokens,
                                                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout)
            return (d["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            log.warning("llm call failed: %s", e)
            return None

    def llm_json(self, system: str, user: str, max_tokens: int = 300, tier: str = "fast") -> dict | None:
        txt = self.llm_text(system, user, max_tokens, tier=tier)
        m = re.search(r"\{.*\}", txt or "", re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None

    def backend_ok(self) -> bool:
        if self._ping_local():
            return True
        mode = self.tune().get("provider") or "auto"
        return mode != "local" and bool(grok_key(self.env))

    def ping(self):
        t0 = time.time()
        ok = self._ping_local()
        self._local_up = ok
        self._ping_at = time.time()
        return ok, time.time() - t0

    # ------------------------------------------------------------ job views / slash commands
    def job_line(self, j: dict) -> str:
        mins = int(((j["finished"] or time.time()) - j["started"]) // 60)
        if j["status"] in ACTIVE:
            return f"『{j['name']}』｜{j['status']}｜已 {mins} 分鐘｜{self.orch.describe(j)}"
        if j["status"] == "被擋住":
            tail = "（等你回答）" if j.get("awaiting") else f"（/resume {j['name']} 可接續）"
            return f"『{j['name']}』｜被擋住：{short(j['note'], 60)}{tail}"
        return f"『{j['name']}』｜做完（{mins} 分鐘）"

    def jobs_text(self, chat_id: int) -> str:
        mine = self.jobs.of(chat_id)
        act = [j for j in mine if j["status"] in ACTIVE]
        blocked = [j for j in mine if j["status"] == "被擋住" and time.time() - (j["finished"] or 0) < 7 * 86400]
        recent = [j for j in mine if j["status"] == "做完" and time.time() - (j["finished"] or 0) < 86400][-3:]
        if not (act or blocked or recent):
            return t(self.lang_for(chat_id), "no_jobs")
        lines = []
        for title, group in (("進行中：", act), ("被擋住：", blocked), ("最近做完：", recent)):
            if group:
                lines += [title] + [f"  {self.job_line(j)}" for j in group]
        return "\n".join(lines)

    def session_kb(self, chat_id: int) -> int:
        sid = self.state.sid(chat_id)
        files = glob.glob(f"{SESSIONS_DIR}/**/*{sid}*.jsonl", recursive=True)
        return sum(os.path.getsize(f) for f in files) // 1024

    def command(self, text: str, chat_id: int, user_id: int, is_owner: bool) -> str | None:
        if not text.startswith("/"):
            return None
        head, _, arg = text.partition(" ")
        cmd = head.lower().split("@")[0]
        arg = arg.strip()
        if cmd in ("/help", "/start", "/commands"):
            return HELP_TEXT
        if cmd in ("/new", "/reset"):
            self.state.d["summary"].pop(str(chat_id), None)
            self.state.d.get("fresh", {}).pop(str(chat_id), None)
            sid = self.state.new_session(chat_id)
            return t(self.lang_for(chat_id), "new_chat") + f" ({sid})"
        if cmd == "/compact" and is_owner:
            s = self.compact_chat(chat_id, self.state.sid(chat_id))
            return "上下文已壓縮，大方向我留著：\n" + short(s, 700)
        if cmd == "/status":
            ok, dt = self.ping()
            lines = [
                f"模型：{self.state.d['model']} → {self.state.alias()}",
                f"思考：{'on' if self.state.d['thinking'] else 'off'}",
                f"session：{self.state.sid(chat_id)}（{self.session_kb(chat_id)} KB）",
                f"後端：{'OK' if ok else '不通'}（{dt:.1f}s）" + (f"；離線期間先記下 {len(self.deferred)} 則" if self.deferred else ""),
                f"橋接：已運行 {int((time.time() - self.started) // 60)} 分鐘" + (f"；上一則：{self.last_turn}" if self.last_turn else ""),
            ]
            if is_owner:
                lines.append(self.jobs_text(chat_id))
            return "\n".join(lines)
        if not is_owner:
            return t(self.lang_for(chat_id), "owner_only") if cmd in ("/model", "/thinking", "/think", "/stop", "/skills", "/jobs", "/resume") else None
        if cmd == "/jobs":
            return self.jobs_text(chat_id)
        if cmd == "/model":
            if not arg:
                return f"目前：{self.state.d['model']} → {self.state.alias()}\n可選：{', '.join(PRESETS)}（/model list）"
            if arg.lower() == "list":
                return "\n".join(f"{k} → {v}" for k, v in PRESETS.items())
            if arg not in PRESETS:
                return f"沒有 '{arg}'。可選：{', '.join(PRESETS)}"
            self.state.d["model"] = arg
            self.state.d["thinking"] = (arg == "think")
            self.state.save()
            return f"已切換：{arg} → {PRESETS[arg]}（下一則生效）"
        if cmd in ("/thinking", "/think"):
            if arg.lower() not in ("on", "off"):
                return f"思考：{'on' if self.state.d['thinking'] else 'off'}\n用法：/thinking on|off"
            on = arg.lower() == "on"
            self.state.d["thinking"] = on
            self.state.d["model"] = "think" if on else "main"      # 閘門以別名決定思考，只送旗標會被吃掉
            self.state.save()
            return f"思考：{'on' if on else 'off'}（模型 → {self.state.alias()}）"
        if cmd == "/stop":
            return self.stop(chat_id, arg)
        if cmd == "/resume":
            return self.resume(chat_id, arg)
        if cmd == "/skills":
            return format_list(SKILLS_DIR)
        return f"未知指令 '{cmd}'。\n" + HELP_TEXT

    def stop(self, chat_id: int, name: str) -> str:
        act = self.jobs.of(chat_id, ACTIVE)
        if name:
            j = self.jobs.find(chat_id, name, act)
            return self._stop_job(j) if j else f"沒有進行中的『{name}』。\n" + self.jobs_text(chat_id)
        with self.mu:
            fg = self.fg.get(chat_id)
        if fg:
            fg.kill()
            return "已中止目前處理。"
        if len(act) == 1:
            return self._stop_job(act[0])
        if len(act) > 1:
            return "要停哪一件？回 /stop <名稱>：\n" + "\n".join(f"  『{j['name']}』" for j in act)
        return "沒有進行中的處理。"

    def _stop_job(self, j: dict) -> str:
        self.orch.stop(j)
        self.jobs.update(j, status="被擋住", note="依指示停止", awaiting=False)
        return f"已停止『{j['name']}』（只停這一件，做到哪都留著；/resume {j['name']} 可接續）"

    def resume(self, chat_id: int, name: str) -> str:
        blocked = self.jobs.of(chat_id, ("被擋住",))
        j = self.jobs.find(chat_id, name, blocked) if name else (blocked[-1] if len(blocked) == 1 else None)
        if not j:
            return "要接續哪一件？\n" + self.jobs_text(chat_id)
        self.orch.resume(j)
        return f"好，接續『{j['name']}』，從上次停下的地方繼續。"

    # ------------------------------------------------------------ inbound
    def handle_update(self, u: dict):
        m = u.get("message")
        if not m or "chat" not in m:
            return
        chat, frm = m["chat"], m.get("from") or {}
        chat_id, uid = chat["id"], frm.get("id", 0)
        text = (m.get("text") or m.get("caption") or "").strip()
        who = frm.get("first_name") or str(uid)
        private = chat.get("type") == "private"

        if private:
            if uid == self.owner:
                mode = "owner"
            elif uid in self.chat_only:
                mode = "chat"
            else:
                log.info("ignored private message from %s", uid)
                return
        else:
            if chat_id not in self.groups or (uid != self.owner and uid not in self.chat_only):
                return
            mention = f"@{self.username}".lower()
            replied_to_bot = ((m.get("reply_to_message") or {}).get("from") or {}).get("username", "").lower() == self.username.lower()
            if mention not in text.lower() and not replied_to_bot:
                return                                        # 群組需被點名（同 OpenClaw requireMention）
            text = re.sub(re.escape(mention), "", text, flags=re.I).strip()
            mode = "chat"

        files = []
        if text.startswith("/"):
            if self.dispatch(chat_id, uid == self.owner, mode, text, files, m.get("message_id"), uid):
                return
        if m.get("photo"):
            p = self.tg.download(m["photo"][-1]["file_id"], ".jpg")
            if p:
                files.append(p)
        for key in ("voice", "audio", "video_note"):
            if m.get(key):
                p = self.tg.download(m[key]["file_id"], ".oga")
                t = transcribe(p, self.asr) if p else None
                if t:
                    text = f"{text}\n{t}".strip() if text else t
                    self.tg.send(chat_id, f"🎤 {t}", m.get("message_id"))
                else:
                    self.tg.send(chat_id, self._asr_fail(chat_id), m.get("message_id"))
                    return
        if m.get("document"):
            d = m["document"]
            p = self.tg.download(d["file_id"])
            if p:
                if (d.get("mime_type") or "").startswith("image/"):
                    files.append(p)
                else:
                    text = f"{text}\n（使用者傳了檔案 {d.get('file_name', '')}，已存到 {p}）".strip()
        if not text and not files:
            return
        self.dispatch(chat_id, uid == self.owner, mode, text or "請看這張圖片。", files, m.get("message_id"), uid)

    def dispatch(self, chat_id, is_owner: bool, mode: str, text: str, files: list, msg_id, uid) -> bool:
        """Telegram 與 LINE 共用的入口：斜線指令即時處理（回 True），其餘每則各開一條執行緒（輪詢/webhook 永遠不被卡住）。"""
        if text.startswith("/"):
            reply = self.command(text, chat_id, uid, is_owner)
            if reply is not None:
                self.tg.send(chat_id, reply, msg_id)
                return True
        log.info("inbound chat=%s mode=%s len=%d files=%d", chat_id, mode, len(text), len(files))
        threading.Thread(target=self.process_message, args=(chat_id, uid, mode, text, files, msg_id), daemon=True).start()
        return False

    # ------------------------------------------------------------ LINE
    def handle_line_event(self, ev: dict):
        """LINE webhook 事件：只收 1 對 1 文字/圖片/語音；身份用 LINE userId 對應 owner / 唯讀聊天；其他一律忽略。"""
        if ev.get("type") != "message":
            return
        src = ev.get("source") or {}
        if src.get("type") != "user":
            log.info("LINE: ignored non-user source (%s)", src.get("type"))
            return
        uid = src.get("userId", "")
        if uid and uid == self.line_owner:
            mode, is_owner = "owner", True
        elif uid in self.line_chat_only:
            mode, is_owner = "chat", False
        else:
            log.info("LINE: ignored user %s…", uid[:6])
            return
        chat_id = "line-" + uid
        line = getattr(self.tg, "line", None)
        if line:
            line.note_event(chat_id, ev.get("replyToken"))
        m = ev.get("message") or {}
        kind, text, files = m.get("type"), "", []
        if kind == "text":
            text = (m.get("text") or "").strip()
        elif kind == "image" and line:
            p = line.download(m.get("id", ""), ".jpg")
            if p:
                files.append(p)
        elif kind == "audio" and line:
            p = line.download(m.get("id", ""), ".m4a")
            text = (transcribe(p, self.asr) if p else None) or ""
            if not text:
                self.tg.send(chat_id, self._asr_fail(chat_id))
                return
        else:
            self.tg.send(chat_id, "我目前看得懂文字、圖片和語音訊息。")
            return
        if not text and not files:
            return
        self.dispatch(chat_id, is_owner, mode, text or "請看這張圖片。", files, None, uid)

    # ------------------------------------------------------------ routing（多線）
    def process_message(self, chat_id, uid, mode, text, files, msg_id):
        try:
            if mode == "owner" and self.route(chat_id, text, files, msg_id):
                return
            self.foreground(chat_id, uid, mode, text, files, msg_id)
        except Exception:
            log.exception("process_message failed")
            self.tg.send(chat_id, "（我這邊出了點狀況，這則沒處理成，請再傳一次）", msg_id)

    def route(self, chat_id: int, text: str, files: list, msg_id) -> bool:
        """有背景工作在跑（或在等他回答）時，先判斷這則是誰的。回 True＝已處理。"""
        pend = self.pending.pop(chat_id, None)
        if pend and time.time() - pend["ts"] < 1800:
            if re.match(r"^\s*(加|併|加進|加入|併進|併入)", text):
                j = self.jobs.find(chat_id, pend["jobs"][0], self.jobs.of(chat_id, ACTIVE))
                if j:
                    self._addto(j, pend["text"], pend["files"], chat_id, msg_id)
                    return True
            elif re.match(r"^\s*(新|另開|新的)", text):
                threading.Thread(target=self.process_pending_as_new, args=(chat_id, pend, msg_id), daemon=True).start()
                return True
            else:                                              # 沒回答選項 → 當初那則照新事處理，這則正常往下走
                threading.Thread(target=self.process_pending_as_new, args=(chat_id, pend, None), daemon=True).start()
        act = self.jobs.of(chat_id, ACTIVE)
        waiting = self.jobs.of(chat_id, ("被擋住",), awaiting=True)
        if not (act or waiting):
            return False
        if len(text) <= 40 and STATUS_RE.search(text):
            self.tg.send(chat_id, self.jobs_text(chat_id), msg_id)
            return True
        if len(text) <= 40 and STOP_RE.search(text) and act:
            j = self.jobs.find(chat_id, text, act) or (act[0] if len(act) == 1 else None)
            if j:
                self.tg.send(chat_id, self._stop_job(j), msg_id)
            else:
                self.tg.send(chat_id, "要停哪一件？\n" + "\n".join(f"  『{x['name']}』" for x in act) + "\n回「/stop 名稱」。", msg_id)
            return True
        if len(waiting) == 1 and not act:                      # 只有一件在等答案：他的回覆多半就是答案，除非明顯無關
            r = self.llm_json(
                'Judge whether the new message answers the pending question (answer) or is a different matter (other). '
                'The user may write Chinese or English. Output one JSON object only: {"route":"answer"} or {"route":"other"}',
                f"待答：{waiting[0]['note']}\n新訊息：{text}", 40)
            if (r or {}).get("route", "answer") == "answer":
                self.orch.resume(waiting[0], answer=text + "".join(f"（附檔：{f}）" for f in files))
                self.tg.send(chat_id, f"收到，我接著做『{waiting[0]['name']}』。", msg_id)
                return True
            return False
        pool = act + waiting
        listing = "\n".join(f"{i + 1}. 名稱：{j['name']}｜目標：{short(j['goal'], 60)}" for i, j in enumerate(pool))
        r = self.llm_json(
            "Classify the new message. The user may write Chinese or English. Output one JSON object only, English keys:\n"
            '{"route":"addto","job":"<name>"}  = it amends, narrows, or answers that job\n'
            '{"route":"other"}  = unrelated chat, a question, or a new task\n'
            '{"route":"unclear"}  = it might be either', f"進行中的事：\n{listing}\n\n新訊息：{text}", 60)
        route = (r or {}).get("route", "other")
        log.info("triage route=%s job=%s", route, (r or {}).get("job"))
        if route == "addto":
            j = self.jobs.find(chat_id, (r or {}).get("job", ""), pool) or (pool[0] if len(pool) == 1 else None)
            if j:
                self._addto(j, text, files, chat_id, msg_id)
                return True
            route = "unclear"
        if route == "unclear":
            names = [j["name"] for j in pool]
            self.pending[chat_id] = {"text": text, "files": files, "jobs": names, "ts": time.time()}
            self.tg.send(chat_id, f"這則是要加進『{names[0]}』，還是另外開一件？回「加」或「新」，我先不動。", msg_id)
            return True
        return False

    def process_pending_as_new(self, chat_id, pend, msg_id):
        self.foreground(chat_id, self.owner, "owner", pend["text"], pend["files"], msg_id)

    def _addto(self, j: dict, text: str, files: list, chat_id: int, msg_id):
        extra = text + "".join(f"（附檔：{f}）" for f in files)
        if j["status"] == "被擋住":                                # 在等他回答 → 直接接續
            self.orch.resume(j, answer=extra)
            self.tg.send(chat_id, f"收到，我接著做『{j['name']}』。", msg_id)
            return
        ctrl = self.orch.get(j["id"])
        if ctrl:
            ctrl.supplements.append(extra)
            self.tg.send(chat_id, f"好，已加進『{j['name']}』，做完手上這步就照新的要求調整。", msg_id)
        else:
            self.tg.send(chat_id, f"『{j['name']}』目前沒在執行（{j['status']}），先 /resume 再補充。", msg_id)

    # ------------------------------------------------------------ 前景一則（先回再做；超過兩輪交給 orchestrator）
    def foreground(self, chat_id, uid, mode, text, files, msg_id):
        lock = self.chat_locks.setdefault(chat_id, threading.Lock())     # 同一聊天的前景共用一個 session 檔，一次一則
        if lock.locked():
            self.tg.send(chat_id, "前一件還在處理，做完就接著處理你這則。", msg_id, essential=False)
        with lock:
            sid = self.pick_sid(chat_id)
            prompt = self.build_prompt(chat_id, mode, text)
            handle, stop = pirun.Handle(), threading.Event()
            with self.mu:
                self.fg[chat_id] = handle
            threading.Thread(target=self._typing, args=(chat_id, stop), daemon=True).start()
            t0, acked = time.time(), [False]

            def on_round(said, calls, n):
                if n == 1 and not acked[0]:
                    acked[0] = True
                    # 先回再做：第一個工具一開始就回一句「做什麼＋第一步」；模型自己有先說一句就用它的
                    first = pirun.describe_call(calls[0])
                    self.tg.send(chat_id, said.strip() or f"好，我來處理「{short(text, 14)}」，先{first}。", msg_id, essential=False)
                if n >= PROMOTE_AT and mode == "owner":
                    return "abort"

            knobs = self.tune()
            ep = self._endpoint("main")
            provider = ep["pi_provider"] if ep["provider"] == "grok" else self.provider
            model = ep["model"] if ep["provider"] == "grok" else self.state.alias()
            spec = pirun.Spec(self.pi, provider, model, sid, prompt, SESSIONS_DIR, DATA, skills=skills_for(text, SKILLS_DIR),
                              files=files, mode=mode, chat_prompt=CHAT_ONLY_PROMPT, chat_cwd=CHAT_CWD,
                              wall=int(knobs["fg_wall"]), stall=int(knobs["tool_stall"]),
                              max_ctx=int(float(knobs["compact_at"]) * 2.2))
            attempt, notified = 0, False
            try:
                while True:
                    with self.slots:
                        r = pirun.run(spec, handle, on_round)
                    if r.kind == "error" and TRANSIENT.search(r.text) and attempt < len(RETRY_WAITS):
                        if not notified:
                            self.tg.send(chat_id, t(self.lang_for(chat_id, text), "retry"), essential=False)
                            notified = True
                        if handle.cancel.wait(RETRY_WAITS[attempt]):
                            return
                        spec.prompt = "（連線剛才中斷，請接續剛才的任務。）" if r.rounds else prompt
                        attempt += 1
                        continue
                    break
            finally:
                stop.set()
                with self.mu:
                    if self.fg.get(chat_id) is handle:
                        self.fg.pop(chat_id, None)
            self.last_turn = f"{time.time() - t0:.0f}s"
            log.info("foreground done chat=%s kind=%s %s", chat_id, r.kind, self.last_turn)
            if r.kind == "cancelled":
                return
            if r.kind in ("aborted", "loop", "stalled", "too_long", "timeout", "ctx_full") and mode == "owner":
                self.promote(chat_id, text, sid, r.trace)                  # 這件比想像大 → 交給 orchestrator，聊天立刻空出來
                return
            if r.kind == "final":
                self.tg.send(chat_id, r.text, msg_id)
                if r.ctx_tokens > int(self.tune()["compact_at"]):             # 回完話之後再壓，不讓他等
                    self.compact_chat(chat_id, sid, r.ctx_tokens)
            elif r.kind == "error" and TRANSIENT.search(r.text):
                self.defer(chat_id, uid, mode, text, files, msg_id)
            elif r.kind == "empty":
                self.tg.send(chat_id, "（模型沒有產生回覆，換個說法或 /new 後再試一次）", msg_id)
            else:
                self.tg.send(chat_id, f"（沒處理成：{short(r.text or r.kind, 200)}）", msg_id)

    def promote(self, chat_id, text, sid, trace):
        first = re.split(r"[，。,.；;：:！!？?\n]", re.sub(r"[「」『』]", "", text).strip())[0]
        name = short(first, 10).rstrip("…") or "任務"                         # 暫名；規劃完成後由模型取短名取代
        job = self.jobs.add(chat_id, name, text, sid)
        self.state.new_session(chat_id)                                    # 前景聊天換新 session，舊的留給這件工作的前情
        self.tg.send(chat_id, "這件要多做幾步，我先拆一下步驟再動手；你可以繼續聊別的，做完我直接跟你說。", essential=False)
        self.orch.start(job, trace=trace)

    def defer(self, chat_id, uid, mode, text, files, msg_id):
        """退階：後端連不上 → 先記下，恢復後自動處理，不要他重傳。"""
        with self.mu:
            self.deferred.append({"chat_id": chat_id, "uid": uid, "mode": mode, "text": text, "files": files, "msg_id": msg_id})
            json.dump(self.deferred, open(STATE_DIR + "/deferred.json", "w"), ensure_ascii=False)
        self.tg.send(chat_id, t(self.lang_for(chat_id, text), "defer"), msg_id)

    def _deferred_loop(self):
        while True:
            time.sleep(30)
            try:
                self.tg.flush_outbox()
            except Exception:
                pass
            with self.mu:
                if not self.deferred:
                    continue
            if not self.backend_ok():
                continue
            with self.mu:
                items, self.deferred = self.deferred, []
                json.dump([], open(STATE_DIR + "/deferred.json", "w"))
            for it in items:
                threading.Thread(target=self.process_message, args=(it["chat_id"], it["uid"], it["mode"], it["text"], it["files"], it["msg_id"]),
                                 daemon=True).start()

    def compact_chat(self, chat_id: int, sid: str, tokens: int = 0) -> str:
        """自主壓縮聊天上下文：釘住的（人格、常駐 skills、使用者的偏好）本來就在 AGENTS.md；
        這裡把對話敘事壓成摘要＋保留最近幾則原文，換新 session，下一則自動帶前情。"""
        prev = self.state.d["summary"].get(str(chat_id), "")
        summary, recent = self.cx.compact_chat(SESSIONS_DIR, sid, prev)
        self.state.d["summary"][str(chat_id)] = summary
        self.state.d.setdefault("recent", {})[str(chat_id)] = recent
        self.state.d.setdefault("fresh", {})[str(chat_id)] = True        # 下一則要帶前情
        self.state.save()
        new_sid = self.state.new_session(chat_id)
        log.info("compacted chat=%s %s → %s (%d tokens → summary %d chars)", chat_id, sid, new_sid, tokens, len(summary))
        return summary

    def pick_sid(self, chat_id: int) -> str:
        sid = self.state.sid(chat_id)
        if self.cx.dangling(SESSIONS_DIR, sid):                              # 上次被砍在半途：壓縮前情後換新 session，別接在殘缺的後面
            log.warning("session %s ends with a dangling tool call → compact & rotate", sid)
            self.compact_chat(chat_id, sid)
            return self.state.sid(chat_id)
        if self.session_kb(chat_id) * 1024 > SESSION_MAX_BYTES:               # 檔案太大的保險：先壓縮再換新
            self.compact_chat(chat_id, sid)
            return self.state.sid(chat_id)
        return sid

    def build_prompt(self, chat_id: int, mode: str, text: str) -> str:
        if mode != "owner":
            return text
        notes = []
        with self.jobs.lock:
            for j in self.jobs.items:
                if j["chat_id"] != chat_id:
                    continue
                if j["status"] == "做完" and not j["surfaced"]:
                    notes.append(f"背景工作『{j['name']}』已完成：{short(j['result'], 200)}")
                    j["surfaced"] = True
                elif j["status"] in ACTIVE:
                    notes.append(f"背景工作『{j['name']}』進行中（與這則無關就不要碰它）")
            self.jobs.save()
        head = ""
        if self.state.d.get("fresh", {}).get(str(chat_id)):                  # 剛壓縮過 → 這則帶前情（之後就在 session 裡了）
            head = ("【前情摘要（上下文已壓縮；以此接續，方向以此為準）】\n" + self.state.d["summary"].get(str(chat_id), "")
                    + "\n【最近幾則對話原文】\n" + self.state.d.get("recent", {}).get(str(chat_id), "") + "\n【以上為前情，下面是新訊息】\n")
            self.state.d["fresh"][str(chat_id)] = False
            self.state.save()
        align = (
            "Alignment: answer only from this message, the prior context, and what tools actually returned. "
            "Do not claim work you did not do. Do not invent paths, hosts, or numbers.\n" + RULE + "\n"
        )
        return head + (("（" + "；".join(notes) + "）\n") if notes else "") + align + text

    def _typing(self, chat_id, stop):
        while not stop.is_set():
            self.tg.typing(chat_id)
            stop.wait(TYPING_EVERY)

    # ------------------------------------------------------------ startup / main loop
    def recover(self):
        """橋接重啟：上次還在跑的工作自己接著做（最多自動接 3 次，避免重啟迴圈），並說一聲。"""
        for j in [x for x in self.jobs.items if x["status"] in ACTIVE]:
            if j.get("resumes", 0) >= 3:
                self.jobs.update(j, status="被擋住", note="反覆被中斷，先停下；/resume 再試")
                self.tg.send(j["chat_id"], f"『{j['name']}』被中斷好幾次了，我先停下來，你說一聲我再接。（/resume {j['name']}）")
                continue
            self.orch.resume(j, auto=True)
            self.tg.send(j["chat_id"], f"我這邊剛重啟過，『{j['name']}』接著剛才的進度繼續做（{self.orch.describe(j)}）。")

    def run(self):
        threading.Thread(target=self._deferred_loop, daemon=True).start()
        offset_file = STATE_DIR + "/offset"
        try:
            offset = int(open(offset_file).read().strip())
        except Exception:
            offset = 0
        backoff = 3
        while True:
            try:
                r = self.tg.call("getUpdates", _t=POLL_TIMEOUT + 15, offset=offset, timeout=POLL_TIMEOUT)
                backoff = 3
                self.tg.flush_outbox()
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode()[:200]
                except Exception:
                    pass
                if e.code == 409:
                    if "webhook" in body.lower():
                        log.warning("webhook active, deleting it"); self.tg.call("deleteWebhook")
                    else:
                        log.warning("409 conflict (another poller on this token?): %s", body)
                    time.sleep(10)
                elif e.code == 429:
                    time.sleep(10)
                else:
                    log.warning("getUpdates HTTP %s: %s", e.code, body); time.sleep(backoff); backoff = min(backoff * 2, 60)
                continue
            except Exception as e:
                log.warning("getUpdates failed: %s", e); time.sleep(backoff); backoff = min(backoff * 2, 60)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                try:
                    self.handle_update(u)
                except Exception:
                    log.exception("handle_update failed")
                with open(offset_file, "w") as f:
                    f.write(str(offset))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(STATE_DIR + "/lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another bridge instance is running")
    env = load_env(CFG_DIR + "/env")
    for k in ("BOT_TOKEN", "OWNER_ID"):
        if not env.get(k):
            sys.exit(f"missing {k} in {CFG_DIR}/env")
    tg = Telegram(env["BOT_TOKEN"], STATE_DIR + "/outbox.jsonl")
    line_ch = hook = None
    if env.get("LINE_CHANNEL_ACCESS_TOKEN") and env.get("LINE_CHANNEL_SECRET"):
        client = LineClient(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_CHANNEL_SECRET"])
        line_ch = LineChannel(client, STATE_DIR, MEDIA_DIR, notify=lambda t: tg.send(int(env["OWNER_ID"]), t))
    br = Bridge(Router(tg, line_ch), env, State(STATE_DIR + "/settings.json"))
    br.username = tg.call("getMe")["result"]["username"]
    if line_ch:
        hook = WebhookServer(int(env.get("LINE_PORT", "18789")), line_ch.c, br.handle_line_event, env.get("LINE_BIND", "127.0.0.1"))
        hook.start()
    try:
        tg.call("setMyCommands", commands=BOT_COMMANDS)
    except Exception:
        log.warning("setMyCommands failed (cosmetic)")
    br.recover()
    if os.environ.get("PITG_DASH", "1") != "0":
        from dashboard import Dashboard
        dash = Dashboard(br, path=STATE_DIR + "/tune.json", log_path=os.environ.get("PITG_LOG", ""),
                         port=int(os.environ.get("PITG_DASH_PORT", "8787")))
        if dash.start():
            log.info("dashboard http://127.0.0.1:%s/", dash.port)
        else:
            log.warning("dashboard did not start (port busy); bridge continues")
    log.info("bridge ready as @%s (owner=%s, chat-only=%s, groups=%s, LINE=%s)", br.username, br.owner, br.chat_only, br.groups, "on" if line_ch else "off")
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    br.run()


if __name__ == "__main__":
    main()
