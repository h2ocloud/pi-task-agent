"""orchestrator — 把「比較大的事」自主做完：規劃 → 小步執行 → 驗收 → 太大就遞迴拆 → 做不到就退階。

為什麼這樣設計（針對本地模型）：一個長對話走到底，context 越長越容易跑偏、卡住、空轉。
所以每一步都用「全新 session」只做一件小事，前面的結果只帶摘要；做不起來就把那一步再拆小（遞迴，最深 MAX_DEPTH 層）；
還是不行就退階——換個做法重試 → 拆小 → 誠實跳過並在回報裡說明——整件事不因為一步失敗而報廢。
只有「真的需要使用者」（密碼、授權、要他拍板）才停下來問，其餘一律自己想辦法。
重啟、後端暫時離線都會自己接續。狀態全存在 job 記錄裡（jobs.json），隨時可接續。

依賴（鴨子型別，方便測試）env 需提供：
  send(chat_id, text) · run_pi(job, sid, prompt, handle, max_rounds, wall) -> pirun.Result
  llm_json(system, user, max_tokens) -> dict|None · llm_text(system, user, max_tokens) -> str|None
  backend_ok() -> bool · jobs (Jobs 登錄檔) · log
"""
from __future__ import annotations

import re
import threading
import time

import pirun
from context import Compactor, ungrounded
from pirun import short

MAX_DEPTH = 3                      # 遞迴拆小的最深層數
STEP_ROUNDS = 14                   # 單步工具輪數上限（超過＝這步太大，拆小）
STEP_CTX = 45000                   # 單步上下文 token 上限；超過就壓縮後接續（不算失敗）
MAX_CARRIES = 3                    # 同一步最多壓縮接續幾次，再不行就拆小
MAX_STEPS = 7
JOB_CAP = 10800                    # 整件工作的牆鐘安全網
PROGRESS_EVERY = 900               # 超過這麼久還沒做完 → 每隔這麼久回報一次
TRANSIENT = re.compile(r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|fetch failed|network|timed out|socket hang up|"
                       r"terminated|overloaded|\b50[0-9]\b|\b429\b|connection error", re.I)
HUMAN_RE = re.compile(r"密碼|帳號|登入|授權|權限不足|需要你|請你|同意|要不要|哪一個|API ?key|token|付款|信用卡|驗證碼|2FA|拍板|由你決定")

PLANNER = (
    "你是任務規劃器。把使用者交代的任務拆成 2–6 個「小而具體、各自可驗證」的步驟；每步由一個只有 bash/read/write/edit 的助理，"
    "在全新對話裡獨立完成。\n"
    "不要假設這台機器上有哪些目錄、帳號、遠端主機或服務。任務原文沒寫的，就讓助理用工具查；查不到就當沒有，不要編成有。\n"
    "規則：\n- 步驟要小：一步大約 3–8 次工具呼叫內做得完；不要「全部整理好」這種大步驟。\n"
    "- 每步附 done_when：怎樣算完成（可檢查的產物或輸出）。\n"
    "- 只做讀、查、整理、寫檔；對外寄信/發訊息/部署上線這類動作放最後一步，並標明「需使用者確認」。\n"
    "- 任務其實很單純（一兩步就好）就只給 1 步。不要把任務改成另一件事。\n"
    '只輸出 JSON：{"name":"任務短名（2–8 個字，像「發票整理」）","steps":[{"title":"…","done_when":"…"}]}'
)
DECOMPOSER = (
    "你是任務規劃器。下面這一步對助理來說太大或做不起來，請把它拆成 2–5 個更小、更具體的子步驟（每步 3–6 次工具呼叫內做得完），"
    "並避開原本卡住的做法。只輸出 JSON：" '{"steps":[{"title":"…","done_when":"…"}]}'
)
REPLANNER = (
    "你是任務規劃器。使用者在任務進行中補充了新要求。請依新要求，重新規劃「還沒做的步驟」（已完成的不要動、不要重做）。"
    "每步小而具體並附 done_when。只輸出 JSON：" '{"steps":[{"title":"…","done_when":"…"}]}'
)
VERIFIER = (
    "你是務實的驗收員。依「完成標準」判斷這一步的回報是否算完成。回報明顯沒做到、答非所問、偏離這一步去做成別的事、"
    "或宣稱了沒有根據的檔案與數字，判 false。"
    '無法從回報確認產物是否存在時，以回報為準。只輸出 JSON：{"ok":true|false,"why":"一句話"}'
)
REPORTER = (
    "你是使用者的助理，剛做完交代的一件事，現在要回報。像同事講話：自然、具體、不官腔，不要提內部機制（步驟、session、子任務等字眼）。\n"
    "規則：\n"
    "1. 先逐項對照他原本問/要的每一件事，每一項都要明確回答到，不能漏；有問「有沒有問題」就直接說有或沒有。\n"
    "2. 要具體：檔名與完整路徑、行號、數字、指令、結果內容，不要只說「已完成」。\n"
    "3. 沒做成的部分與原因要誠實說；需要他做的事（沒有就別提）放在最後一行。\n"
    "4. 長度跟著任務走：一般事務 3–6 行；調查/分析/比較類可以到 15 行，用短條列讓手機好讀。繁體中文。"
)
STEP_PROMPT = """你正在完成一件較大的任務裡的「其中一步」。只做這一步，不要重做前面做過的事，也不要提前做後面的步驟。

整體目標（原文，方向以此為準）：{goal}
{pins}計畫：
{plan}
目前累積的進度與關鍵事實：
{prev}
{extra}
這一步：{title}
完成標準：{done}

對齊（這幾條優先於把事情做完的衝動）：
- 只用上面的原文、進度，以及你這一步工具實際讀到的內容。沒出現過的路徑、主機、帳號、數字不要當成已知。
- 沒有做過的事不要說做完。沒看到的檔案與指令輸出不要編。
- 回報裡的路徑與網址必須來自這一步的工具結果，或上面已經寫明的原文。
- 這一步若已經偏離整體目標，停下來換做法，不要把任務做成別的事。
- 可以多花幾步。不要空轉同一個指令。

做完用一到三行說明結果（含產出檔案的完整路徑）。真的做不到才開頭寫「被擋住：」並說明原因與你試過什麼；
只有需要使用者提供資訊、授權或拍板時才寫「需要你：」。其他困難自己想辦法換做法。不要對外寄信或發訊息。"""


class Cancelled(Exception):
    pass


class NeedHuman(Exception):
    def __init__(self, step, reason):
        super().__init__(reason)
        self.step, self.reason = step, reason


class JobTimeout(Exception):
    pass


class Ctrl:
    """一件工作的執行控制：取消、補充要求。"""

    def __init__(self):
        self.handle = pirun.Handle()
        self.supplements: list[str] = []
        self.thread: threading.Thread | None = None

    @property
    def cancelled(self):
        return self.handle.cancel.is_set()


def new_step(sid: str, title: str, done_when: str = "") -> dict:
    return {"id": sid, "title": short(title, 80), "done_when": short(done_when, 120), "status": "todo",
            "result": "", "note": "", "sub": []}


def parse_steps(obj, prefix: str = "") -> list:
    steps = []
    raw = (obj or {}).get("steps") if isinstance(obj, dict) else None
    for i, s in enumerate(raw if isinstance(raw, list) else [], 1):
        if isinstance(s, dict) and str(s.get("title", "")).strip():
            steps.append(new_step(f"{prefix}{i}", s["title"], s.get("done_when", "")))
    return steps[:MAX_STEPS]


def flat_progress(plan: list) -> tuple[int, int]:
    """(完成的葉節點, 全部葉節點)"""
    d = n = 0
    for s in plan:
        if s["sub"]:
            a, b = flat_progress(s["sub"])
            d, n = d + a, n + b
        else:
            n += 1
            d += s["status"] == "done"
    return d, n


class Orchestrator:
    def __init__(self, env):
        self.env = env
        self.cx = Compactor(env)
        self.ctrls: dict[str, Ctrl] = {}
        self.mu = threading.Lock()

    # ------------------------------------------------------------ public
    def start(self, job: dict, trace: list | None = None, resumed: bool = False) -> Ctrl:
        ctrl = Ctrl()
        with self.mu:
            self.ctrls[job["id"]] = ctrl
        ctrl.thread = threading.Thread(target=self._run, args=(job, ctrl, trace, resumed), daemon=True)
        ctrl.thread.start()
        return ctrl

    def get(self, job_id: str) -> Ctrl | None:
        with self.mu:
            return self.ctrls.get(job_id)

    def stop(self, job: dict):
        ctrl = self.get(job["id"])
        if ctrl:
            ctrl.handle.kill()

    def resume(self, job: dict, auto: bool = False, answer: str = ""):
        """接續：把做到一半的步驟放回待辦，從還沒做完的地方繼續（不重新規劃）。"""
        if answer:
            job.setdefault("pins", []).append(answer)
        self._reset(job.get("plan") or [])
        job["awaiting"] = False
        if auto:
            job["resumes"] = job.get("resumes", 0) + 1
        self.env.jobs.update(job, status="還在跑", note="")
        return self.start(job, resumed=True)

    def describe(self, job: dict) -> str:
        """給 /jobs 與進度用的一句話。"""
        cur, plan = job.get("cursor"), job.get("plan") or []
        if not plan:
            return "規劃中"
        d, n = flat_progress(plan)
        bits = [f"完成 {d}/{n} 步"]
        if cur and job["status"] in ("開始了", "還在跑"):
            bits.append(f"現在：{short(cur['title'], 30)}" + ("（這步比較大，拆小在做）" if cur.get("depth") else ""))
        return "，".join(bits)

    # ------------------------------------------------------------ main flow
    def _run(self, job, ctrl, trace, resumed):
        env = self.env
        timer = threading.Thread(target=self._progress_loop, args=(job, ctrl), daemon=True)
        try:
            if not job.get("plan"):
                job["plan"] = self.make_plan(job, trace)
                env.jobs.update(job, status="還在跑")
                self.announce(job)
            timer.start()
            ok = self.run_steps(job, job["plan"], 0, ctrl)
            self.conclude(job, ctrl, ok)
        except Cancelled:
            pass                                          # 停止的人已經更新狀態、回過話了
        except NeedHuman as e:
            self.ask_human(job, e)
        except JobTimeout:
            env.jobs.update(job, status="被擋住", note="超過安全時間上限，先停下（進度都保留）")
            env.send(job["chat_id"], f"『{job['name']}』做太久了，我先停在這裡（進度都存著）。要我繼續就回「/resume {job['name']}」。")
        except Exception as e:
            env.log.exception("orchestrator crashed")
            env.jobs.update(job, status="被擋住", note=f"內部錯誤：{type(e).__name__}")
            env.send(job["chat_id"], f"『{job['name']}』我這邊出了點狀況停下來了（{type(e).__name__}），進度存著，回「/resume {job['name']}」可以接續。")
        finally:
            ctrl.handle.cancel.set() if ctrl.cancelled else None
            job["_done"] = True
            with self.mu:
                self.ctrls.pop(job["id"], None)

    def run_steps(self, job, steps: list, depth: int, ctrl: Ctrl) -> bool:
        all_ok, i = True, 0
        while i < len(steps):
            self._check(job, ctrl)
            if depth == 0 and ctrl.supplements:
                self.replan(job, steps, i, ctrl)
            step = steps[i]
            if step["status"] in ("done", "skipped"):
                all_ok = all_ok and step["status"] == "done"
                i += 1
                continue
            all_ok = self.do_step(job, steps, i, depth, ctrl) and all_ok
            i += 1
        return all_ok

    def do_step(self, job, steps, i, depth, ctrl) -> bool:
        step, jobs = steps[i], self.env.jobs
        job["cursor"] = {"path": step["id"], "title": step["title"], "i": i + 1, "n": len(steps), "depth": depth}
        step["status"] = "doing"
        jobs.update(job)
        hint, reason, kind = "", "", ""
        for attempt in range(2):
            r = self.exec_step(job, steps, step, attempt, hint, ctrl)
            if r.kind == "cancelled":
                raise Cancelled()
            if r.kind == "final":
                if r.text.startswith("被擋住") or r.text.startswith("需要你"):
                    kind, reason = "blocked", r.text
                else:
                    blob = "\n".join([
                        job.get("goal") or "", step.get("title") or "", step.get("done_when") or "",
                        job.get("digest") or "", "\n".join(job.get("pins") or []),
                        "\n".join(f"{t.get('action', '')} {t.get('result', '')}" for t in (r.trace or [])),
                    ])
                    missing = ungrounded(r.text, blob)
                    if missing:
                        kind, reason = "ungrounded", "回報了工具紀錄裡沒有的路徑或連結：" + "、".join(missing)
                    else:
                        ok, why = self.verify(step, r.text)
                        if ok:
                            step["status"], step["result"] = "done", short(r.text, 900)
                            job["digest"] = self.cx.roll_digest(job.get("digest", ""), step["title"], r.text)   # 遞迴壓縮進度摘要
                            jobs.update(job)
                            return True
                        kind, reason = "unverified", f"結果沒達到完成標準（{why}）"
            elif r.kind in ("too_long", "loop", "stalled", "timeout"):
                kind = r.kind
                reason = {"too_long": "這步太大，做太久還沒收尾", "loop": "一直重複同樣的動作", "stalled": "有個動作卡死了兩次",
                          "timeout": "做太久"}[r.kind]
                break                                      # 太大/卡住：不是換個說法能解，直接拆小
            else:
                kind, reason = r.kind, r.text or "沒有產生結果"
            if kind == "blocked" and HUMAN_RE.search(reason):
                raise NeedHuman(step, reason)
            hint = f"上一次沒成功：{reason}\n請換一個做法，不要重複同樣的指令。先用工具確認，再回報實際看到的路徑與結果。"
        # 還是沒成 → 遞迴拆小；拆不了／已到最深 → 退階：跳過並記下原因，整件事繼續
        if depth < MAX_DEPTH:
            subs = self.decompose(job, step, reason)
            if subs:
                step["sub"] = subs
                jobs.update(job)
                ok = self.run_steps(job, subs, depth + 1, ctrl)
                step["status"] = "done" if ok else "skipped"
                step["note"] = "" if ok else "拆小後仍有部分沒成"
                jobs.update(job)
                return ok
        step["status"], step["note"] = "skipped", short(reason, 160)
        jobs.update(job)
        return False

    # ------------------------------------------------------------ one step
    def exec_step(self, job, steps, step, attempt, hint, ctrl):
        env = self.env
        extra = [hint] if hint else []
        pins = ("使用者的明確要求與回答（原文，不可更動）：\n" + "\n".join(f"- {x}" for x in job["pins"][-8:]) + "\n") if job.get("pins") else ""
        base = dict(goal=short(job["goal"], 400), plan=self.plan_text(job["plan"], step["id"]), pins=pins,
                    prev=job.get("digest") or "（還沒有）", title=step["title"], done=step["done_when"] or "（自行判斷合理的完成）")
        n, carries, carry = 0, 0, ""
        while True:
            remaining = JOB_CAP - (time.time() - job["started"])
            if remaining <= 0:
                raise JobTimeout()
            ex = list(extra) + ([f"這一步先前已做到（壓縮後的前情，請從這裡接續，不要重做）：\n{carry}"] if carry else [])
            prompt = STEP_PROMPT.format(extra=("\n" + "\n".join(ex) + "\n") if ex else "", **base)
            sid = f"job-{job['id']}-{step['id'].replace('.', '_')}-{attempt}" + (f"-r{n}" if n else "")
            r = env.run_pi(job, sid, prompt, ctrl.handle, STEP_ROUNDS, int(min(remaining, 3600)), STEP_CTX)
            n += 1
            if r.kind == "error" and TRANSIENT.search(r.text) and not ctrl.cancelled:
                self.wait_backend(job, ctrl)              # 後端暫時不通：自己等，恢復後同一步重做
                continue
            if r.kind == "ctx_full" and carries < MAX_CARRIES:
                carries += 1                              # 上下文快滿：壓成前情，開新 session 接續（目標/要求原文每次都重新帶）
                carry = self.cx.carry_from_trace(r.trace, r.text)
                env.log.info("step %s ctx_full → compacted and continuing (%d)", step["id"], carries)
                continue
            return r

    def wait_backend(self, job, ctrl):
        env = self.env
        if time.time() - job.get("_paused_ts", 0) > 1800:      # 離線/恢復反覆時別洗版：半小時內只說一次
            job["_paused_ts"] = time.time()
            env.send(job["chat_id"], f"模型後端暫時連不上，我先把『{job['name']}』擱著，恢復後自己接著做，不用你管。", essential=False)
        while not ctrl.cancelled:
            if time.time() - job["started"] > JOB_CAP:
                raise JobTimeout()
            ctrl.handle.cancel.wait(30)
            if ctrl.cancelled:
                raise Cancelled()
            if env.backend_ok():
                return
        raise Cancelled()

    def verify(self, step, text):
        r = self.env.llm_json(VERIFIER, f"這一步：{step['title']}\n完成標準：{step['done_when'] or '（無）'}\n回報：{short(text, 600)}", 120)
        if isinstance(r, dict) and r.get("ok") is False:
            return False, short(str(r.get("why", "")), 80)
        return True, ""                                   # 驗收員失效時放行：寧可信回報，不卡住

    # ------------------------------------------------------------ planning
    def make_plan(self, job, trace):
        tr = "\n".join(f"- {t['action']} → {short(t['result'], 80)}" for t in (trace or [])[:6]) or "（無）"
        sup = "；".join(job.get("pins", [])) or "（無）"
        obj = self.env.llm_json(PLANNER, f"任務：{job['goal']}\n已做過的前情：\n{tr}\n補充：{sup}", 600)
        steps = parse_steps(obj)
        if not steps:                                     # 退階：規劃失敗 → 整件當一步交給遞迴機制去拆
            steps = [new_step("1", job["goal"], "任務完成並回報結果")]
        nm = str((obj or {}).get("name", "")).strip().strip("『』「」")
        if 2 <= len(nm) <= 10 and nm != job["name"]:       # 模型取的短名取代「截前 12 字」的暫名（同聊天內不重名）
            taken = {j["name"] for j in self.env.jobs.items if j["chat_id"] == job["chat_id"] and j is not job}
            base, k = nm, 1
            while nm in taken:
                k += 1
                nm = f"{base}{k}"
            job["name"] = nm
        return steps

    def decompose(self, job, step, reason):
        obj = self.env.llm_json(DECOMPOSER, f"整體目標：{short(job['goal'], 300)}\n太大的這一步：{step['title']}\n完成標準：{step['done_when']}\n"
                                            f"做不下去的原因：{reason}", 500)
        subs = parse_steps(obj, prefix=step["id"] + ".")
        return subs if len(subs) >= 2 else []

    def replan(self, job, steps, i, ctrl):
        sup, ctrl.supplements = list(ctrl.supplements), []
        done = "\n".join(f"- {s['title']}：{short(s['result'], 80)}" for s in steps[:i] if s["status"] == "done") or "（無）"
        rest = "\n".join(f"- {s['title']}" for s in steps[i:] if s["status"] not in ("done", "skipped")) or "（無）"
        obj = self.env.llm_json(REPLANNER, f"整體目標：{short(job['goal'], 300)}\n已完成：\n{done}\n原本還沒做的：\n{rest}\n新的補充要求：{'；'.join(sup)}", 600)
        new = parse_steps(obj, prefix="r")
        if not new:                                       # 退階：重排失敗 → 在最後加一步專門處理補充
            new = [new_step("r1", "處理補充要求：" + short("；".join(sup), 60), "補充要求已處理")]
        for k, s in enumerate(new, i + 1):
            s["id"] = str(k)
        steps[i:] = new
        job.setdefault("pins", []).extend(sup)            # 補充原文釘住：之後每一步都原樣帶著，不經壓縮改寫
        self.env.jobs.update(job)
        self.env.send(job["chat_id"], "好，我照新的要求調整了後面的做法：\n" + "\n".join(f"{k}. {s['title']}" for k, s in enumerate(new[:4], 1)), essential=False)

    # ------------------------------------------------------------ reporting
    def announce(self, job):
        plan = job["plan"]
        if len(plan) < 2:
            return
        lines = "\n".join(f"{k}. {s['title']}" for k, s in enumerate(plan, 1))
        self.env.send(job["chat_id"], f"好，『{job['name']}』我拆成 {len(plan)} 步來做：\n{lines}\n先從第 1 步開始，要改方向隨時說。", essential=False)

    def _progress_loop(self, job, ctrl):
        last = time.time()
        while not job.get("_done") and not ctrl.cancelled:
            time.sleep(5)
            if time.time() - last >= PROGRESS_EVERY:
                last = time.time()
                mins = int((time.time() - job["started"]) // 60)
                self.env.send(job["chat_id"], f"『{job['name']}』還在進行（已 {mins} 分鐘）：{self.describe(job)}。做完我再跟你說。", essential=False)

    def conclude(self, job, ctrl, ok):
        env, plan = self.env, job["plan"]
        done, total = flat_progress(plan)
        lines = self.result_lines(plan)
        text = env.llm_text(REPORTER, f"使用者原本交代的任務（原文）：{short(job['goal'], 800)}\n"
                                      + (f"之後補充/回答的要求：{'；'.join(job.get('pins', []))}\n" if job.get("pins") else "")
                                      + f"各部分的做法與結果：\n{short(lines, 7000)}", 900)
        if not text or len(text) < 5:
            text = lines                                  # 退階：報告員失效 → 直接給各步結果
        if done == 0:
            env.jobs.update(job, status="被擋住", note="每一步都沒成功")
            env.send(job["chat_id"], f"『{job['name']}』我試了幾種做法都沒成。\n{text}\n（想換方向直接告訴我，或回「/resume {job['name']}」再試一次）")
            return
        env.jobs.update(job, status="做完", result=short(text, 600))
        env.send(job["chat_id"], f"做完：『{job['name']}』\n{text}")

    def ask_human(self, job, e: NeedHuman):
        self.env.jobs.update(job, status="被擋住", note=f"需要你：{short(e.reason, 150)}", awaiting=True)
        self.env.send(job["chat_id"], f"『{job['name']}』做到「{e.step['title']}」需要你：\n{short(e.reason.replace('被擋住：', '').replace('需要你：', ''), 400)}\n"
                                      f"你直接回我就好，我拿到就接著做。")

    # ------------------------------------------------------------ helpers
    def _check(self, job, ctrl):
        if ctrl.cancelled:
            raise Cancelled()
        if time.time() - job["started"] > JOB_CAP:
            raise JobTimeout()

    @staticmethod
    def _reset(steps):
        for s in steps:
            if s["status"] == "doing":
                s["status"] = "todo"
            Orchestrator._reset(s["sub"])

    @staticmethod
    def plan_text(plan, cur_id, depth=0):
        out = []
        for s in plan:
            mark = {"done": "✓", "skipped": "✗", "doing": "▶"}.get(s["status"], " ")
            if s["id"] == cur_id:
                mark = "▶"
            out.append(f"{'  ' * depth}{mark} {s['id']}. {s['title']}")
            if s["sub"]:
                out.append(Orchestrator.plan_text(s["sub"], cur_id, depth + 1))
        return "\n".join(out)

    @staticmethod
    def prev_text(plan):
        res = [f"- {s['title']}：{s['result']}" for s in plan if s["status"] == "done" and s["result"]]
        return "\n".join(res[-6:]) or "（還沒有）"

    @staticmethod
    def result_lines(plan, depth=0):
        out = []
        for s in plan:
            tag = {"done": "完成", "skipped": "沒成"}.get(s["status"], s["status"])
            out.append(f"{'  ' * depth}- {s['title']}｜{tag}｜{s['result'] or s['note']}")
            if s["sub"]:
                out.append(Orchestrator.result_lines(s["sub"], depth + 1))
        return "\n".join(out)
