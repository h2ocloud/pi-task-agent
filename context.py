"""context — 自主壓縮上下文，同時守住「大方向」。

做法（為什麼壓完不會歪）：
  1. 釘住（pin）的東西絕不交給模型改寫：原始目標、使用者的明確要求/回答、計畫骨架。它們原文每次都帶。
  2. 只壓縮「敘事」（做過什麼、看到什麼）。壓縮是遞迴的：新摘要 = 壓(舊摘要 + 新增內容)，所以長任務也不會越滾越大。
  3. 壓完用規則檢查：舊內容裡的關鍵錨點（檔案路徑、網址、ID、數字單位）有沒有掉；掉了就原樣補回一行，不靠模型記得。
  4. 退階：模型壓縮失敗或太短 → 改用規則式摘要（釘住內容＋最近幾則原文），保證至少方向不丟。
"""
from __future__ import annotations

import glob
import json
import os
import re

from pirun import short

ANCHOR_RE = re.compile(
    r"(https?://[^\s)）」<>]+"
    r"|(?:~|/(?:Users|home)/[^/\s,，。；;)）」`'\"]+|/(?:tmp|opt|var|etc))/[^\s,，。；;)）」`'\"]+"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|\btool\s?\d+\b|\bt\d{2,3}\b)"
)

COMPACTOR = (
    "你是上下文壓縮員。把下面「舊摘要＋新增內容」壓成一份新的工作摘要，供助理接續工作時當唯一的前情。\n"
    "必須保留（逐項、具體）：\n"
    "1. 大方向：目前在做/討論的主題與目標\n2. 已確定的結論與決定（含理由一句話）\n3. 進行中的事與下一步\n4. 還沒解決的問題\n"
    "5. 所有關鍵事實：檔案路徑、網址、ID、數字、指令名稱、錯誤訊息與已知的解法\n6. 使用者的偏好、禁忌與明確指示\n"
    "可以丟掉：寒暄、重複、已過時的嘗試過程、冗長的工具輸出。不要編造；不確定的就不要寫。\n"
    "用繁體中文，條列，總長 {limit} 字以內。直接輸出摘要本文，不要前言。"
)


def anchors(text: str) -> list[str]:
    seen, out = set(), []
    for m in ANCHOR_RE.findall(text or ""):
        m = m.rstrip(".,;:")
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def ungrounded(text: str, blob: str) -> list[str]:
    """回報裡的路徑或網址，若目標、釘選、進度、工具紀錄都沒出現過，就當沒有根據。"""
    known = []
    for a in anchors(blob):
        if a.startswith("http") or a.startswith("~") or a.startswith("/"):
            known.append(a.rstrip("…"))
    out = []
    for a in anchors(text):
        if not (a.startswith("http") or a.startswith("~") or a.startswith("/")):
            continue
        base = a.rstrip("…")
        if base and (base in blob or a in blob):
            continue
        if any(len(k) >= 8 and (base.startswith(k) or k.startswith(base)) for k in known):
            continue
        out.append(a)
    return out[:8]


def ensure_anchors(summary: str, source: str, limit: int = 12) -> str:
    """壓縮後缺掉的錨點，原樣補回一行（規則式，不靠模型記得）。"""
    missing = [a for a in anchors(source) if a not in summary][:limit]
    return summary + ("\n（保留的關鍵路徑／連結：" + "；".join(missing) + "）" if missing else "")


class Compactor:
    def __init__(self, env):
        self.env = env

    def compress(self, prev: str, new: str, limit: int = 900) -> str:
        """遞迴壓縮：壓(舊摘要 + 新增)。失敗退階成規則式（保留尾端＋錨點）。"""
        source = f"【舊摘要】\n{prev or '（無）'}\n\n【新增內容】\n{new}"
        out = self.env.llm_text(COMPACTOR.format(limit=limit), source[-9000:], max_tokens=min(1200, limit * 2))
        if not out or len(out) < 20:
            out = short(prev, limit // 2) + "\n" + short(new, limit // 2)
        return ensure_anchors(short(out, limit + 400) if len(out) > limit * 1.6 else out, source)

    # ---- 聊天 session：讀 pi 的 session 檔 → 逐則文字
    @staticmethod
    def read_session(sessions_dir: str, sid: str, per_msg: int = 500):
        msgs = []
        for f in sorted(glob.glob(f"{sessions_dir}/**/*{sid}*.jsonl", recursive=True)):
            for line in open(f, errors="ignore"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message") if isinstance(d.get("message"), dict) else None
                if not m or m.get("role") not in ("user", "assistant", "toolResult"):
                    continue
                c = m.get("content")
                if isinstance(c, str):
                    text, calls = c, []
                else:
                    text = " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
                    calls = [x for x in (c or []) if isinstance(x, dict) and x.get("type") in ("toolCall", "tool_use")]
                if m["role"] == "toolResult":
                    msgs.append(("工具結果", short(text, 160)))
                    continue
                if text.strip():
                    if m["role"] == "user" and text.lstrip().startswith("【前情摘要"):
                        continue                        # 壓縮時自己帶進來的前情：已在 prev 裡，別重複吃進敘事
                    msgs.append(("使用者" if m["role"] == "user" else "助理", short(text, per_msg)))
                for cl in calls:
                    a = cl.get("arguments") or cl.get("args") or {}
                    msgs.append(("助理動作", short(f"{cl.get('name')} {json.dumps(a, ensure_ascii=False)}", 140)))
        return msgs

    @staticmethod
    def dangling(sessions_dir: str, sid: str) -> bool:
        """session 最後一則是「有工具呼叫但沒有結果」（執行中被砍、橋接重啟、/stop）→ 直接接續會被模型供應端拒絕。"""
        last = None
        for f in sorted(glob.glob(f"{sessions_dir}/**/*{sid}*.jsonl", recursive=True)):
            for line in open(f, errors="ignore"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d.get("message"), dict) and d["message"].get("role") in ("user", "assistant", "toolResult"):
                    last = d["message"]
        if not last or last.get("role") != "assistant":
            return False
        c = last.get("content")
        return isinstance(c, list) and any(isinstance(x, dict) and x.get("type") in ("toolCall", "tool_use") for x in c)

    def compact_chat(self, sessions_dir: str, sid: str, prev_summary: str, keep_recent: int = 4):
        """回傳 (新摘要, 最近幾則原文)。最近幾則原文不經模型改寫，保住眼前這條對話線。"""
        msgs = self.read_session(sessions_dir, sid)
        talk = [m for m in msgs if m[0] in ("使用者", "助理")]
        recent = talk[-keep_recent:]
        narrative = "\n".join(f"{who}：{t}" for who, t in msgs)           # 摘要涵蓋全部；最近幾則另外原文附帶（重複沒關係，寧可不漏）
        summary = self.compress(prev_summary, narrative, limit=1100)
        return summary, "\n".join(f"{who}：{short(t, 300)}" for who, t in recent)

    # ---- 一件工作：把「步驟結果」滾進摘要
    def roll_digest(self, digest: str, step_title: str, result: str, limit: int = 900) -> str:
        add = f"完成「{short(step_title, 40)}」：{short(result, 500)}"
        if len(digest) + len(add) <= limit:                  # 還放得下就原文累加，不必動用模型
            return (digest + "\n" + add).strip()
        return self.compress(digest, add, limit)

    def carry_from_trace(self, trace: list, last_text: str = "") -> str:
        """步驟中途上下文爆了：把這一步到目前為止做過的事濃縮成接續用的前情。"""
        raw = "\n".join(f"- {t['action']} → {short(t['result'], 120)}" for t in trace[-30:])
        return self.compress("", raw + (f"\n最後的想法：{short(last_text, 300)}" if last_text else ""), limit=700)
