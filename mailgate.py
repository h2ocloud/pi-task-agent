#!/usr/bin/env python3
"""mailgate — 寄信硬擋：使用者沒在對話裡確認，就只產生草稿、不寄。純 stdlib。

流程（前景、平行 session、背景工作都一樣，因為擋在工具這一層）：
  1. 助理執行 `pitg-mail draft --to … --subject … --body …` → 存成草稿、印出全文與 id，不寄。
  2. 使用者在 Telegram/LINE 回「寄出」「確認寄」→ 橋接層呼叫 approve_pending()，核可最新那份草稿
     產生之前、他最後一則訊息之後出的草稿（同一輪出的都算；他要求修改之前的舊稿不算）。助理手上沒有核可指令。
  3. 助理執行 `pitg-mail send --confirmed <id>` → 有核可、沒過期、還沒寄過才寄，而且只寄草稿原文（不能改）。
助理 PATH 裡的 gog 是 tools/gog-guard：一律帶 gog 內建的 --gmail-no-send。真的 gog 只有這裡會呼叫。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

APPROVE_TTL = 1800                 # 核可後多久內要寄出
DRAFT_TTL = 1800                   # 只核可這麼久以內的草稿
# 寄件帳號／client 從環境變數讀（公開 repo 不寫死個人帳號）。
# 由 bridge 從 ~/.config/pi-tg-bridge/env 匯出給子行程；也可直接設 GMAIL_ACCOUNT／GMAIL_CLIENT 或用 --account／--client。
ACCOUNT = os.environ.get("GMAIL_ACCOUNT", "")
CLIENT = os.environ.get("GMAIL_CLIENT", "")


def root() -> str:
    return os.path.join(os.environ.get("PITG_HOME") or os.path.expanduser("~/.pi-tg"), "state", "mail")


def real_gog() -> str:
    return os.environ.get("PITG_REAL_GOG") or "/opt/homebrew/bin/gog"


def _path(d: str, draft_id: str) -> str:
    return os.path.join(d, os.path.basename(draft_id) + ".json")


def _load(d: str, draft_id: str) -> dict | None:
    try:
        return json.load(open(_path(d, draft_id)))
    except Exception:
        return None


def _save(d: str, draft: dict):
    os.makedirs(d, exist_ok=True)
    tmp = _path(d, draft["id"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(draft, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(d, draft["id"]))


def new_draft(to: str, subject: str, body: str, cc: str = "", account: str = ACCOUNT, client: str = CLIENT, d: str | None = None) -> dict:
    if "\\n" in body and "\n" not in body:
        body = body.replace("\\n", "\n")                      # 跟舊腳本一樣：字面 \n 轉真的換行
    draft = {"id": uuid.uuid4().hex[:8], "to": to, "cc": cc, "subject": subject, "body": body.replace("\r", ""),
             "account": account, "client": client, "created": time.time(), "approved": 0, "sent": 0}
    _save(d or root(), draft)
    return draft


def pending(now: float | None = None, d: str | None = None) -> list:
    """還沒寄、DRAFT_TTL 內產生的草稿（舊→新）。"""
    d, now = d or root(), now or time.time()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = [x for x in (_load(d, n[:-5]) for n in names if n.endswith(".json")) if x]
    return sorted((x for x in out if not x.get("sent") and 0 <= now - x.get("created", 0) <= DRAFT_TTL), key=lambda x: x["created"])


def approve_pending(since: float, now: float | None = None, d: str | None = None) -> list:
    """橋接層專用：使用者在對話裡確認寄出 → 核可 since 之後產生、還沒寄的草稿。"""
    d, now = d or root(), now or time.time()
    ids = []
    for draft in pending(now, d):
        if draft["created"] > since:
            draft["approved"] = now
            _save(d, draft)
            ids.append(draft["id"])
    return ids


def check(draft_id: str, now: float | None = None, d: str | None = None) -> tuple[dict | None, str]:
    """(草稿, 不能寄的原因)。原因是空字串才可以寄。"""
    now = now or time.time()
    draft = _load(d or root(), draft_id)
    if not draft:
        return None, f"找不到草稿 {draft_id}"
    if draft.get("sent"):
        return draft, "這封已經寄過了，不重寄"
    if not draft.get("approved") or draft["approved"] < draft["created"]:
        return draft, "使用者還沒在對話裡確認寄出。先把整封草稿給他看，等他說「寄出」"
    if now - draft["approved"] > APPROVE_TTL:
        return draft, "確認已經過期（超過 30 分鐘），請再給他看一次、等他重新確認"
    return draft, ""


def gog_cmd(draft: dict, body_file: str) -> list:
    cmd = [real_gog(), "send", "-a", draft["account"], "--client", draft["client"], "--to", draft["to"]]
    if draft.get("cc"):
        cmd += ["--cc", draft["cc"]]
    return cmd + ["--subject", draft["subject"], "--body-file", body_file, "--json"]


def send(draft_id: str, dry_run: bool = False, d: str | None = None) -> tuple[bool, str]:
    d = d or root()
    draft, why = check(draft_id, d=d)
    if why:
        return False, "沒寄：" + why
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(draft["body"].rstrip("\n") + "\n")
        body_file = f.name
    try:
        cmd = gog_cmd(draft, body_file)
        if dry_run:
            return True, "dry-run（沒寄）：" + " ".join(cmd[:3] + ["…", "--to", draft["to"], "--subject", draft["subject"]])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(body_file)
    if r.returncode != 0:
        return False, "寄送失敗：" + (r.stderr or r.stdout).strip()[:300]
    draft["sent"] = time.time()
    _save(d, draft)
    return True, "已寄出：" + r.stdout.strip()[:500]


def render(draft: dict) -> str:
    head = [f"草稿（還沒寄）id={draft['id']}", f"寄件帳號：{draft['account']}", f"收件人：{draft['to']}"]
    if draft.get("cc"):
        head.append(f"副本：{draft['cc']}")
    return "\n".join(head + [f"主旨：{draft['subject']}", "---", draft["body"].rstrip(), "---",
                             "把上面整封貼給使用者看。他在對話裡說「寄出」之後，再執行：pitg-mail send --confirmed " + draft["id"]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pitg-mail", description="寄信：先出草稿，使用者在對話裡確認後才寄")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("draft", help="產生草稿（不寄）")
    a.add_argument("--to", required=True)
    a.add_argument("--cc", default="")
    a.add_argument("--subject", required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file")
    a.add_argument("--account", default=os.environ.get("GMAIL_ACCOUNT") or ACCOUNT)
    a.add_argument("--client", default=os.environ.get("GMAIL_CLIENT") or CLIENT)
    b = sub.add_parser("send", help="寄出已被使用者確認的草稿")
    b.add_argument("--confirmed", required=True, metavar="DRAFT_ID")
    b.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("show", help="看草稿與狀態")
    c.add_argument("draft_id")
    o = ap.parse_args(argv)
    if o.cmd == "draft":
        body = open(o.body_file).read() if o.body_file else o.body
        print(render(new_draft(o.to, o.subject, body, o.cc, o.account, o.client)))
        return 0
    if o.cmd == "show":
        draft, why = check(o.draft_id)
        print(render(draft) + f"\n狀態：{why or '已確認，可以寄'}" if draft else why)
        return 0 if draft else 1
    ok, msg = send(o.confirmed, o.dry_run)
    print(msg)
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
