"""line — LINE Messaging API 通道（webhook 接收＋回覆/推播）＋跟 Telegram 並用的 Router。純 stdlib。

額度現實：官方帳號每月的推播則數有限（常見是 200）；用 reply token 回覆是免費的。所以：
  • 每個進來的事件帶一個 reply token（約 25 秒內可用一次）→ 第一則回覆走 reply（免費）
  • 之後的訊息要 push：只送「必要的」（長工作的開始、結果／被擋住／需要你）；進度（essential=False）在 LINE 不推
  • 本地計數＋啟動時向 LINE 查實際用量；剩不到 5 則就停推，並改由另一個頻道通知擁有者（結果不丟）
聊天 id 用 "line-<userId>" 字串，跟 Telegram 的整數 id 並存，Bridge 其餘邏輯完全共用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("pi-tg.line")
API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
PREFIX = "line-"
REPLY_WINDOW = 25            # reply token 實務上很快過期，保守只用 25 秒內
CHUNK = 4900                 # LINE 單則文字上限 5000


def plain(t: str) -> str:
    """LINE 不渲染 markdown：去掉程式碼圍欄、粗體、標題井號、行內反引號。"""
    t = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", t or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    return t.strip()


def split_text(t: str, n: int = CHUNK) -> list[str]:
    out, cur = [], ""
    for line in t.split("\n"):
        while len(line) > n:
            if cur:
                out.append(cur); cur = ""
            out.append(line[:n]); line = line[n:]
        if len(cur) + len(line) + 1 > n:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


class LineClient:
    def __init__(self, token: str, secret: str):
        self.token, self.secret = token, secret

    def _req(self, method: str, url: str, body: dict | None = None, timeout: int = 15, raw: bool = False):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return data if raw else (json.loads(data) if data else {})

    def verify(self, body: bytes, signature: str) -> bool:
        mac = base64.b64encode(hmac.new(self.secret.encode(), body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(mac, signature or "")

    def reply(self, reply_token: str, texts: list[str]) -> bool:
        try:
            self._req("POST", f"{API}/message/reply", {"replyToken": reply_token, "messages": [{"type": "text", "text": t} for t in texts[:5]]})
            return True
        except Exception as e:
            log.warning("LINE reply failed: %s", e)
            return False

    def push(self, to: str, texts: list[str]) -> bool:
        try:
            self._req("POST", f"{API}/message/push", {"to": to, "messages": [{"type": "text", "text": t} for t in texts[:5]]})
            return True
        except Exception as e:
            log.warning("LINE push failed: %s", e)
            return False

    def loading(self, user_id: str, seconds: int = 30):
        try:
            self._req("POST", f"{API}/chat/loading/start", {"chatId": user_id, "loadingSeconds": seconds}, timeout=8)
        except Exception:
            pass

    def content(self, message_id: str) -> bytes:
        return self._req("GET", f"{DATA_API}/message/{message_id}/content", timeout=60, raw=True)

    def quota(self) -> tuple[int | None, int | None]:
        try:
            q = self._req("GET", f"{API}/message/quota")
            c = self._req("GET", f"{API}/message/quota/consumption")
            return (q.get("value") if q.get("type") == "limited" else None), c.get("totalUsage")
        except Exception:
            return None, None


class LineChannel:
    """Bridge 用的送訊介面（跟 Telegram 同形）：send / typing / download / flush_outbox。"""

    def __init__(self, client: LineClient, state_dir: str, media_dir: str, notify=None):
        self.c, self.media_dir, self.notify = client, media_dir, notify
        self.outbox = state_dir + "/line_outbox.jsonl"
        self.tokens: dict[str, list] = {}         # chat_id -> [reply_token, ts, used]
        self.last_loading: dict[str, float] = {}
        self.mu = threading.Lock()
        self.limit, self.used = client.quota()
        self.warned = False
        log.info("LINE quota: limit=%s used=%s", self.limit, self.used)

    @staticmethod
    def owns(chat_id) -> bool:
        return isinstance(chat_id, str) and chat_id.startswith(PREFIX)

    def note_event(self, chat_id: str, reply_token: str | None):
        if reply_token:
            self.tokens[chat_id] = [reply_token, time.time(), False]

    def _remaining(self) -> int | None:
        return None if self.limit is None or self.used is None else self.limit - self.used

    def send(self, chat_id: str, text: str, reply_to=None, essential: bool = True):
        parts = split_text(plain(text) or "（沒有產生回覆）")
        tok = self.tokens.get(chat_id)
        if tok and not tok[2] and time.time() - tok[1] < REPLY_WINDOW:      # 免費路徑：reply token
            tok[2] = True
            if self.c.reply(tok[0], parts[:5]):
                parts = parts[5:]
                if not parts:
                    return
        if not essential:
            log.info("LINE: dropped non-essential message (no free reply token): %s", text[:40])
            return
        user = chat_id[len(PREFIX):]
        rem = self._remaining()
        if rem is not None and rem < len(parts) + 5:                         # 額度見底：不推，改 Telegram 通知，結果不丟
            log.warning("LINE push quota nearly exhausted (remaining=%s)", rem)
            if self.notify:
                self.notify(f"LINE 推播額度快用完了（剩 {rem} 則），這則沒推：\n{plain(text)[:1500]}")
            return
        for p in parts:
            if self.c.push(user, [p]):
                self.used = (self.used or 0) + 1
            else:
                self._park({"to": user, "text": p})

    def typing(self, chat_id: str):
        if time.time() - self.last_loading.get(chat_id, 0) > 25:             # LINE 的「輸入中」動畫（免費、最長 60 秒）
            self.last_loading[chat_id] = time.time()
            self.c.loading(chat_id[len(PREFIX):], 30)

    def download(self, message_id: str, suffix: str = "") -> str | None:
        try:
            data = self.c.content(message_id)
            os.makedirs(self.media_dir, exist_ok=True)
            dst = f"{self.media_dir}/line_{int(time.time())}_{uuid.uuid4().hex[:6]}{suffix}"
            open(dst, "wb").write(data)
            return dst
        except Exception:
            log.exception("LINE content download failed")
            return None

    def _park(self, p: dict):
        with self.mu, open(self.outbox, "a") as f:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    def flush_outbox(self):
        if not os.path.exists(self.outbox):
            return
        with self.mu:
            try:
                rows = [json.loads(l) for l in open(self.outbox) if l.strip()]
                os.remove(self.outbox)
            except Exception:
                return
        for p in rows:
            if self.c.push(p["to"], [p["text"]]):
                self.used = (self.used or 0) + 1
            else:
                self._park(p)


class Router:
    """把 Bridge 的送訊/輸入中/下載依聊天 id 分給 Telegram 或 LINE。其餘 Telegram API 呼叫直接轉給 Telegram。"""

    def __init__(self, tg, line: LineChannel | None = None):
        self.tg, self.line = tg, line

    def send(self, chat_id, text, reply_to=None, essential=True, key=None):
        """key＝同一件工作的進度訊息：Telegram 原地更新同一則；LINE 只送 essential 的（開始），其餘不推也不佔 reply token。"""
        if self.line and self.line.owns(chat_id):
            if key and not essential:
                return None
            return self.line.send(chat_id, text, reply_to, essential)
        return self.tg.send(chat_id, text, reply_to, **({"key": key} if key else {}))

    def typing(self, chat_id):
        if self.line and self.line.owns(chat_id):
            return self.line.typing(chat_id)
        return self.tg.typing(chat_id)

    def download(self, *a, **k):
        return self.tg.download(*a, **k)

    def call(self, *a, **k):
        return self.tg.call(*a, **k)

    def flush_outbox(self):
        self.tg.flush_outbox()
        if self.line:
            self.line.flush_outbox()


class WebhookServer:
    """POST /line/webhook：驗簽 → 立刻回 200 → 每個事件各開一條執行緒處理（LINE 要求要快回）。"""

    def __init__(self, port: int, client: LineClient, on_event, host: str = "127.0.0.1"):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes = b"ok"):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, b"pi-tg-bridge line webhook ok")

            def do_POST(self):
                if self.path.split("?")[0] != "/line/webhook":
                    return self._send(404, b"not found")
                n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = self.rfile.read(n)
                if not client.verify(body, self.headers.get("X-Line-Signature", "")):
                    log.warning("LINE webhook: bad signature from %s", self.client_address[0])
                    return self._send(401, b"bad signature")
                self._send(200)
                try:
                    events = json.loads(body.decode("utf-8")).get("events", [])
                except Exception:
                    return
                for ev in events:
                    threading.Thread(target=on_event, args=(ev,), daemon=True).start()

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.port = self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        log.info("LINE webhook listening on %s:%s", *self.httpd.server_address[:2])
