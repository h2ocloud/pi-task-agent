"""Language of replies. The model is told the user's language. Chrome strings follow it too.

Detection is a heuristic so a command still works when the fast model is down.
A chat remembers the last clear language. Slash commands do not change it.
"""
from __future__ import annotations

import os
import re

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")

# Appended to every model prompt. The last line wins over any Chinese example in the prompt.
RULE = (
    "Respond in the same language as the user's message. "
    "Use Traditional Chinese when they wrote Chinese. Use English when they wrote English. "
    "Do not default to Chinese."
)

_S = {
    "owner_only": {
        "zh": "這個指令只有擁有者能用。",
        "en": "Only the owner can use this.",
    },
    "backend_down": {
        "zh": "模型後端暫時連不上，我先把『{name}』擱著，恢復後自己接著做，不用你管。",
        "en": "The model is unreachable. I parked “{name}” and will resume it when the backend is back.",
    },
    "retry": {
        "zh": "模型暫時連不上，我自動重試一下…",
        "en": "The model is unreachable. Retrying…",
    },
    "defer": {
        "zh": "模型後端現在連不上，你這則我先記著；一恢復我就自動處理，不用重傳。",
        "en": "The model is unreachable. I saved this message and will run it when the backend is back.",
    },
    "asr_unset": {
        "zh": "沒有設定 ASR_URL，語音請改打字。",
        "en": "ASR_URL is not set. Please type instead of sending voice.",
    },
    "asr_fail": {
        "zh": "語音轉文字失敗（語音服務不通？），請改打字或稍後再試。",
        "en": "Speech-to-text failed. Please type, or try again later.",
    },
    "no_jobs": {
        "zh": "目前沒有背景工作。",
        "en": "No background jobs.",
    },
    "new_chat": {
        "zh": "已開新對話。",
        "en": "Started a new chat.",
    },
    "unknown_cmd": {
        "zh": "未知指令 '{cmd}'。",
        "en": "Unknown command '{cmd}'.",
    },
    "job_active": {
        "zh": "『{name}』｜{status}｜已 {mins} 分鐘｜{desc}",
        "en": "“{name}” | {status} | {mins} min | {desc}",
    },
    "job_blocked": {
        "zh": "『{name}』｜被擋住：{note}{tail}",
        "en": "“{name}” | blocked: {note}{tail}",
    },
    "job_done": {
        "zh": "『{name}』｜做完（{mins} 分鐘）",
        "en": "“{name}” | done ({mins} min)",
    },
    "wait_you": {
        "zh": "（等你回答）",
        "en": " (waiting for you)",
    },
    "resume_hint": {
        "zh": "（/resume {name} 可接續）",
        "en": " (/resume {name} continues it)",
    },
    "hint_retry": {
        "zh": "上一次沒成功：{reason}\n請換一個做法，不要重複同樣的指令。先用工具確認，再回報實際看到的路徑與結果。",
        "en": "Previous attempt failed: {reason}\nTry a different approach. Do not repeat the same command. Verify with a tool, then report only what you saw.",
    },
    "carry": {
        "zh": "這一步先前已做到（壓縮後的前情，請從這裡接續，不要重做）：\n{carry}",
        "en": "Already done on this step (compacted). Continue from here; do not redo it:\n{carry}",
    },
    "pins": {
        "zh": "使用者的明確要求與回答（原文，不可更動）：\n{body}",
        "en": "User requirements, verbatim, do not rewrite:\n{body}",
    },
}


def detect(text: str) -> str:
    """zh when Chinese characters dominate, otherwise en. Empty text is en."""
    cjk = len(_CJK.findall(text or ""))
    latin = len(_LATIN.findall(text or ""))
    if cjk >= 2 and cjk >= latin:
        return "zh"
    return "en"


def normalize(lang: str | None) -> str:
    return "zh" if (lang or "").lower().startswith("zh") else "en"


def t(lang: str, key: str, **kw) -> str:
    row = _S.get(key) or {}
    lang = normalize(lang)
    template = row.get(lang) or row.get("en") or key
    return template.format(**kw) if kw else template


def default_lang() -> str:
    return normalize(os.environ.get("PITG_LANG") or "en")
