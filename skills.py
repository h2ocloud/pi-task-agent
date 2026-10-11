"""skills — 按需載入。一個 skill 是一個含 SKILL.md 的目錄，放進 skills 目錄就擴充，不必改橋接。

預設 PITG_SKILLS=ondemand：
  • frontmatter `always: true` 的每次都帶（不計入上限）
  • 其餘只帶名稱／描述／triggers 與這則訊息有關鍵字重疊的，最多 PITG_SKILL_LIMIT 個（預設 3）
  • 中文連續詞會再切二字，所以「查天氣」對得上描述裡的「天氣」
PITG_SKILLS=all 退回「整個目錄都載」；off 則一個都不載。
pi 端用 `--no-skills` 關掉自動發現，再對每個選中的目錄各給一次 `--skill`。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

_STOP = {"這個", "可以", "我們", "什麼", "一下", "沒有", "不是", "就是", "如果", "因為", "所以", "還是", "已經", "需要", "一個", "自己"}


def tokens(text: str) -> set[str]:
    s = (text or "").lower()
    out = set(re.findall(r"[a-z0-9_]{3,}", s))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", s):
        if len(run) <= 12:
            out.add(run)
        if len(run) >= 3:
            for i in range(len(run) - 1):
                g = run[i:i + 2]
                if g not in _STOP:
                    out.add(g)
    return out


def _frontmatter(text: str) -> dict:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line[0] in " \t-" or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta


def _flag(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Skill:
    name: str
    description: str
    path: str
    always: bool
    triggers: str


def catalog(skills_dir: str) -> list[Skill]:
    if not skills_dir or not os.path.isdir(skills_dir):
        return []
    out = []
    for name in sorted(os.listdir(skills_dir)):
        d = os.path.join(skills_dir, name)
        md = os.path.join(d, "SKILL.md")
        if not os.path.isfile(md):
            continue
        try:
            meta = _frontmatter(open(md, encoding="utf-8", errors="ignore").read(6000))
        except OSError:
            continue
        out.append(Skill(
            meta.get("name") or name,
            meta.get("description") or "",
            d,
            _flag(meta.get("always", "")),
            meta.get("triggers") or "",
        ))
    return out


def skills_for(prompt: str, skills_dir: str) -> list[str]:
    """回傳要傳給 pi 的 skill 目錄。always 一定在前面，不會被上限拿掉。"""
    mode = os.environ.get("PITG_SKILLS", "ondemand").strip().lower()
    items = catalog(skills_dir)
    if mode in ("off", "none", "0"):
        return []
    if mode == "all":
        return [s.path for s in items]
    try:
        limit = int(os.environ.get("PITG_SKILL_LIMIT", "3"))
    except ValueError:
        limit = 3
    always = [s for s in items if s.always]
    want = tokens(prompt)
    scored = []
    for s in items:
        if s.always:
            continue
        overlap = want & tokens(f"{s.name} {s.description} {s.triggers} {os.path.basename(s.path)}")
        if overlap:
            scored.append((len(overlap), s.name, s.path))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s.path for s in always] + [p for _, _, p in scored[:max(0, limit)]]


def format_list(skills_dir: str) -> str:
    mode = os.environ.get("PITG_SKILLS", "ondemand")
    items = catalog(skills_dir)
    if not items:
        return f"skills（{mode}）：（無）"
    lines = []
    for s in items:
        tag = "常駐" if s.always else "按需"
        desc = f" — {s.description[:48]}" if s.description else ""
        lines.append(f"{s.name}（{tag}）{desc}")
    return f"skills（{mode}）：\n" + "\n".join(lines)
