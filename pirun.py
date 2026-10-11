"""pirun — 執行一次 `pi`（JSON 事件串流）。

只管「一次執行」，不碰 Telegram，也不知道任務是什麼。內建四道防線，全部是「單獨處理那一個問題」而不是整件放棄：
  • 單一工具卡死（超過 stall 秒沒回來）→ 只終止那個工具，pi 會收到錯誤、模型自己改走別條路；同一次執行卡第二次才放棄 → kind=stalled
  • 重複同樣的工具呼叫 ≥ loop_n 次 → kind=loop（不再空轉）
  • 輪數上限 max_rounds → kind=too_long（上層會把這一步拆小）
  • 上下文超過 max_ctx → kind=ctx_full（上層壓縮上下文後接續，不是失敗）
  • 牆鐘上限 wall → kind=timeout（只是安全網，不是「慢就砍」）
注意：刻意不用「沒輸出」判斷整件卡住——模型在想、在等下載時本來就沒輸出。
pi 會等 stdin 結束才開工，所以一律 stdin=DEVNULL。
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("pirun")


_SCAN = re.compile(
    r"(?:^|[;&|`('\"]\s*)(?:timeout\s+\d+\s*s?\s+)?(find|grep)\b([^;&|`]*)",
    re.I,
)
_TARGET = re.compile(r"(?:^|\s)(~(?:/\S*)?|/\S*)")


def broad_scan(command: str) -> bool:
    """A find or recursive grep aimed at the whole home directory or the whole disk."""
    if not command:
        return False
    for match in _SCAN.finditer(command):
        tool, rest = match.group(1).lower(), match.group(2)
        if tool == "grep" and not re.search(r"(?:^|\s)-[A-Za-z]*[rR]\b", rest):
            continue
        for raw in _TARGET.findall(rest):
            path = raw.rstrip("/")
            if path in {"", "/", "~", "/Users", "/home"}:
                return True
            if re.fullmatch(r"/(?:Users|home)/[^/]+", path):
                return True
    return False


def path_hints(text: str, limit: int = 30) -> str:
    """Paths actually present in a tool result, so a later report can cite them."""
    found = re.findall(
        r"(?:/(?:Users|home|tmp|opt|var|etc)(?:/[^\s,，。；;)）」`'\"]+)?|~/[^\s,，。；;)）」`'\"]+)",
        text or "",
    )
    out, seen = [], set()
    for item in found:
        item = item.rstrip(".,;:")
        if len(item) < 4 or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return " ".join(out)


def short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_content(m: dict):
    c = m.get("content")
    texts, calls = [], []
    if isinstance(c, str):
        texts = [c]
    elif isinstance(c, list):
        for x in c:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text":
                texts.append(x.get("text", ""))
            elif x.get("type") in ("toolCall", "tool_use"):
                calls.append(x)
    return "\n".join(t for t in texts if t).strip(), calls


def describe_call(call: dict) -> str:
    name = call.get("name", "?")
    a = call.get("arguments") or call.get("args") or {}
    a = a if isinstance(a, dict) else {}
    if name == "bash":
        return f"執行 `{short(a.get('command', ''), 50)}`"
    verb = {"read": "讀", "write": "寫", "edit": "改"}.get(name)
    return f"{verb} {short(a.get('path', ''), 50)}" if verb else f"用 {name}"


def kill_tree(pid: int, sig=signal.SIGKILL):
    for k in _children(pid):
        kill_tree(k, sig)
    try:
        os.kill(pid, sig)
    except Exception:
        pass


def kill_children(pid: int) -> int:
    kids = _children(pid)
    for k in kids:
        kill_tree(k)
    return len(kids)


def _children(pid: int):
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
        return [int(x) for x in out]
    except Exception:
        return []


@dataclass
class Spec:
    pi: str
    provider: str
    alias: str
    sid: str
    prompt: str
    sessions: str
    data: str                                   # owner 模式的工作目錄（含 AGENTS.md、bin/）
    skills: list = field(default_factory=list)   # 選中的 skill 目錄；空的就不載。不要傳整個 skills 根目錄。
    files: list = field(default_factory=list)
    mode: str = "owner"                         # owner＝工具＋人格；chat＝唯讀聊天
    chat_prompt: str = ""
    chat_cwd: str = ""
    max_rounds: int = 0                         # 0＝不限
    max_ctx: int = 0                            # 上下文 token 上限（0＝不限）；超過 → kind=ctx_full，上層壓縮後接續
    wall: int = 10800
    stall: int = 180
    loop_n: int = 3


@dataclass
class Result:
    kind: str                                   # final|error|empty|cancelled|timeout|loop|too_long|stalled|ctx_full|aborted
    text: str = ""
    rounds: int = 0
    trace: list = field(default_factory=list)   # [{"action","result"}]：做過什麼（上層規劃時當前情）
    stalls: int = 0
    ctx_tokens: int = 0                         # 最後一次呼叫時的上下文大小（token）


class Handle:
    """讓外部能取消、查看現況。一個 Handle 可重複用於多次 run（例如一件工作的各個步驟）。"""

    def __init__(self):
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.last_action = ""
        self.rounds = 0
        self.ctx_tokens = 0

    def kill(self):
        self.cancel.set()
        if self.proc:
            kill_tree(self.proc.pid)


def _skill_list(skills) -> list[str]:
    if not skills:
        return []
    if isinstance(skills, str):
        return [skills]
    return [s for s in skills if s]


def run(spec: Spec, h: Handle, on_round_start=None) -> Result:
    """on_round_start(text, calls, n) → 回傳 "abort" 就立刻終止（kind=aborted）；其他回傳值忽略。"""
    cmd = [spec.pi, "-p", "-ne", "--mode", "json", "--provider", spec.provider, "--model", spec.alias,
           "--session-dir", spec.sessions, "--session-id", spec.sid]
    if spec.mode == "owner":
        cmd += ["--tools", "read,bash,edit,write", "--no-skills"]
        for s in _skill_list(spec.skills):
            cmd += ["--skill", s]
        cwd = spec.data
    else:
        cmd += ["--no-tools", "-ns", "-nc", "--system-prompt", spec.chat_prompt]
        cwd = spec.chat_cwd or spec.data
    cmd += ["@" + f for f in spec.files] + ["--", spec.prompt]
    env = dict(os.environ, PI_OFFLINE="1", PATH=f"{spec.data}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
    h.proc, h.rounds = proc, 0
    err_buf: list[str] = []
    threading.Thread(target=lambda: err_buf.append(proc.stderr.read()), daemon=True).start()

    st = {"tool_started": None, "inflight": 0, "stalls": 0, "reason": None, "t0": time.time(), "block_scan": False}
    done = threading.Event()

    def note_stall():
        st["stalls"] += 1
        st["tool_started"] = None
        st["inflight"] = 0
        st["block_scan"] = False
        kill_children(proc.pid)                              # 只終止卡住的那個工具
        if st["stalls"] >= 2:
            st["reason"] = "stalled"
            kill_tree(proc.pid)

    def watchdog():
        while not done.wait(2):
            now = time.time()
            if now - st["t0"] > spec.wall:
                st["reason"] = "timeout"; kill_tree(proc.pid); return
            if st["block_scan"] and st["inflight"]:
                log.warning("stopping a home-wide scan")
                note_stall()
                if st["reason"]:
                    return
                continue
            ts = st["tool_started"]
            if ts and st["inflight"] and now - ts > spec.stall:
                note_stall()
                if st["reason"]:
                    return

    threading.Thread(target=watchdog, daemon=True).start()

    trace, final, last_sig, same = [], "", None, 0
    for line in proc.stdout:
        if h.cancel.is_set():
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        m = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if t in ("message_end", "turn_end") and m.get("role") == "assistant" and isinstance(m.get("usage"), dict):
            u = m["usage"]
            h.ctx_tokens = int(u.get("totalTokens") or (u.get("input", 0) + u.get("cacheRead", 0) + u.get("output", 0)))
        if t == "message_end" and m.get("role") == "assistant":
            text, calls = parse_content(m)
            if calls:
                for call in calls:
                    args = call.get("arguments") or call.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if broad_scan(str((args or {}).get("command") or (args or {}).get("cmd") or "")):
                        st["block_scan"] = True
                        log.warning("home-wide scan queued")
                h.rounds += 1
                h.last_action = describe_call(calls[0])
                a = calls[0].get("arguments") or calls[0].get("args") or {}
                sig = (calls[0].get("name"), json.dumps(a, sort_keys=True, ensure_ascii=False))
                same = same + 1 if sig == last_sig else 1
                last_sig = sig
                trace.append({"action": h.last_action, "result": ""})
                if spec.loop_n and same >= spec.loop_n:
                    st["reason"] = "loop"; kill_tree(proc.pid); break
                if spec.max_ctx and h.ctx_tokens > spec.max_ctx:
                    st["reason"] = "ctx_full"; kill_tree(proc.pid); break
                if spec.max_rounds and h.rounds > spec.max_rounds:
                    st["reason"] = "too_long"; kill_tree(proc.pid); break
                if on_round_start and on_round_start(text, calls, h.rounds) == "abort":
                    st["reason"] = "aborted"; kill_tree(proc.pid); break
            elif text:
                final = text
        elif t == "message_end" and m.get("role") == "toolResult" and trace:
            full = parse_content(m)[0]
            trace[-1]["result"] = short(full, 160)
            hints = path_hints(full)
            if hints:
                trace[-1]["paths"] = hints
        elif t == "tool_execution_start":
            st["inflight"] += 1
            if st["tool_started"] is None:
                st["tool_started"] = time.time()
        elif t == "tool_execution_end":
            if st["inflight"]:
                st["inflight"] -= 1
                if st["inflight"] == 0:
                    st["tool_started"] = None
                    st["block_scan"] = False
        elif t == "turn_end" and m.get("role") == "assistant" and m.get("stopReason") != "toolUse":
            text, _ = parse_content(m)
            if text:
                final = text
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        rc = proc.wait()
    done.set()
    time.sleep(0.05)
    err = (err_buf[0] if err_buf else "").strip()
    base = dict(rounds=h.rounds, trace=trace, stalls=st["stalls"], ctx_tokens=h.ctx_tokens)
    if h.cancel.is_set():
        return Result("cancelled", **base)
    if st["reason"]:
        return Result(st["reason"], text=final, **base)
    if rc == 0 and final:
        return Result("final", final, **base)
    if rc == 0:
        return Result("empty", **base)
    return Result("error", scrub(err or "pi 異常結束"), **base)


def scrub(s: str) -> str:
    return re.sub(r"(thk_live_|sk-|Bearer\s+)[A-Za-z0-9_\-]+", r"\1***", s)
