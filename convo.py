"""convo — 對話層的共用狀態：最近幾輪對話紀錄＋連發合併。Telegram 與 LINE 走同一套。

為什麼要有：前景主 session、平行 session、背景工作、快速模型各自只看得到自己那一段，
「查」「好」「那明天呢」這種短追問就會被反問。這裡記下每個聊天最近的來回（橋接層看得到的全部），
誰要判斷或開工就帶一段進去。紀錄只做脈絡，不是指令；pi 自己的 session 仍是主模型的記憶。
"""
from __future__ import annotations

import json
import os
import threading
import time

from pirun import short

KEEP = 24                          # 每個聊天留幾則（約 12 輪）


class ConvoLog:
    """每個聊天最近幾則：{"who": "u"|"a", "text", "ts", "via"}。via="main"＝已經在主 session 裡的那一則。"""

    def __init__(self, path: str, keep: int = KEEP):
        self.path, self.keep, self.mu = path, keep, threading.Lock()
        self.rows: dict[str, list] = {}
        self.marks: dict[str, float] = {}
        try:
            d = json.load(open(path))
            self.rows, self.marks = d.get("rows", {}), d.get("marks", {})
        except Exception:
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"rows": self.rows, "marks": self.marks}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, chat_id, who: str, text: str, via: str = ""):
        text = (text or "").strip()
        if not text:
            return
        with self.mu:
            rows = self.rows.setdefault(str(chat_id), [])
            rows.append({"who": who, "text": short(text, 600), "ts": time.time(), "via": via})
            del rows[:-self.keep]
            self._save()

    def recent(self, chat_id, n: int = 8, since: float = 0.0, skip: str = "", skip_via: str = "") -> list:
        """最近 n 則（舊→新）。skip＝正在處理的這則（最後一則同文的使用者訊息不算前文）。"""
        with self.mu:
            rows = [r for r in self.rows.get(str(chat_id), []) if r["ts"] > since]
        if skip:
            key = short(skip, 600)
            for i in range(len(rows) - 1, -1, -1):
                if rows[i]["who"] == "u":
                    if rows[i]["text"] == key:
                        rows.pop(i)
                    break
        if skip_via:
            rows = [r for r in rows if r.get("via") != skip_via]
        return rows[-n:]

    def last_user(self, chat_id, skip: str = "") -> dict | None:
        rows = [r for r in self.recent(chat_id, self.keep, skip=skip) if r["who"] == "u"]
        return rows[-1] if rows else None

    def mark(self, chat_id) -> float:
        """主 session 又要開一輪：回傳上一輪開始的時間，並記下這一輪。中間發生的事要補給主 session。"""
        with self.mu:
            prev = self.marks.get(str(chat_id), 0.0)
            self.marks[str(chat_id)] = time.time()
            self._save()
        return prev

    def clear(self, chat_id):
        with self.mu:
            self.rows.pop(str(chat_id), None)
            self.marks.pop(str(chat_id), None)
            self._save()

    @staticmethod
    def render(rows: list, per: int = 300) -> str:
        return "\n".join(f"{'使用者' if r['who'] == 'u' else '助理'}：{short(r['text'], per)}" for r in rows)


class Debouncer:
    """同一個人幾秒內的連發合成一則：每來一則重新計時（wait 秒），從第一則起最多等 cap 秒；到點呼叫 fire(items)。"""

    def __init__(self, fire, wait: float = 2.5, cap: float = 8.0):
        self.fire, self.wait, self.cap = fire, wait, cap
        self.buf: dict = {}
        self.mu = threading.Lock()

    def push(self, key, item, wait: float | None = None):
        wait = self.wait if wait is None else wait
        now = time.time()
        with self.mu:
            b = self.buf.get(key)
            if b is None:
                b = self.buf[key] = {"items": [], "first": now, "timer": None}
            b["items"].append(item)
            if b["timer"]:
                b["timer"].cancel()
            b["timer"] = threading.Timer(max(0.0, min(wait, b["first"] + self.cap - now)), self._fire, (key, b))
            b["timer"].daemon = True
            b["timer"].start()

    def _fire(self, key, b):
        with self.mu:
            if self.buf.get(key) is not b:            # 已經被另一個計時器送出
                return
            self.buf.pop(key)
        self.fire(b["items"])
