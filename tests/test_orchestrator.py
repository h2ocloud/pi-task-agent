#!/usr/bin/env python3
"""orchestrator 邏輯測試：假 pi / 假 LLM，不碰網路。涵蓋：正常規劃執行、驗收失敗重試、遞迴拆小、最深退階跳過、
需要人類、補充要求重排、上下文爆掉壓縮接續、後端離線自癒、重啟接續、取消。python3 tests/test_orchestrator.py"""
import json, os, sys, tempfile, threading, time, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as O, pirun
from pirun import Result
O.PROGRESS_EVERY = 3600

class FakeJobs:
    def __init__(s): s.items = []; s.lock = threading.RLock()
    def update(s, job, **kw): job.update(kw); job["updated"] = time.time()
class Env:
    log = logging.getLogger("t")
    def __init__(s, script):
        s.jobs = FakeJobs(); s.sent = []; s.meta = []; s.script = script; s.calls = []; s.llm_log = []; s.backend = True
    def send(s, chat_id, text, reply_to=None, essential=True, key=None): s.sent.append(text); s.meta.append((text, essential, key))
    def run_pi(s, job, sid, prompt, handle, max_rounds, wall, max_ctx=0):
        s.calls.append((sid, prompt)); return s.script(sid, prompt, len(s.calls), s)
    def llm_json(s, system, user, max_tokens=300, tier="fast"):
        s.llm_log.append(("json", system[:6], user)); return s.llm_handler(system, user)
    def llm_text(s, system, user, max_tokens=300, timeout=90, tier="fast", temperature=None):
        s.llm_log.append(("text", system[:6], user)); return "做好了：" + user[-60:].replace("\n", " ")
    def backend_ok(s): return s.backend
    llm_handler = staticmethod(lambda system, user: None)

def mkjob(goal="整理九月發票"):
    return {"id": "j1", "name": "發票", "goal": goal, "chat_id": 1, "sid": "x", "status": "開始了", "note": "", "started": time.time(),
            "updated": 0, "finished": None, "result": "", "surfaced": False, "plan": [], "cursor": None, "resumes": 0, "awaiting": False}
def run_job(env, job, trace=None, timeout=30):
    orch = O.Orchestrator(env); ctrl = orch.start(job, trace); ctrl.thread.join(timeout); return orch, ctrl
def planner(steps):
    def h(system, user):
        if "任務規劃器" in system and "補充了新要求" not in system and "太大" not in system: return {"steps": [{"title": t, "done_when": "ok"} for t in steps]}
        if "驗收員" in system: return {"ok": True}
        return None
    return h
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:200])); (not cond) and fails.append(name)

# 1 正常：3 步，每步 final；摘要滾動；最後回報「做完」
env = Env(lambda sid, p, n, e: Result("final", f"步驟完成 /tmp/out{n}.txt", rounds=2, trace=[{"action": f"寫 /tmp/out{n}.txt", "result": "ok"}])); env.llm_handler = planner(["讀檔", "整理", "輸出"])
job = mkjob(); run_job(env, job)
check("1 normal: done + 3 pi runs", job["status"] == "做完" and len(env.calls) == 3, (job["status"], len(env.calls)))
check("1 start said first, then the plan", env.sent and "背景" in env.sent[0] and "拆成 3 步" in env.sent[1], env.sent[:2])
ess = [m for m, e, k in env.meta if e]
check("1 LINE budget: only start + done are essential; plan/milestones are keyed progress", len(ess) == 2 and "背景" in ess[0] and ess[1].startswith("做完")
      and all(k == job["id"] for m, e, k in env.meta if not e) and sum("進行中 " in m for m, e, k in env.meta) == 3, env.meta)
check("1 digest carries paths", "/tmp/out1.txt" in job.get("digest", "") and "/tmp/out3.txt" in job["digest"], job.get("digest"))
check("1 goal pinned verbatim in every step prompt", all("整理九月發票" in c[1] for c in env.calls))

# 2 驗收失敗 → 換做法重試一次 → 成功
state = {"n": 0}
def s2(sid, p, n, e):
    if n == 1: return Result("final", "做了但不對", rounds=1)
    return Result("final", "這次對了 /tmp/ok.txt", rounds=1, trace=[{"action": "寫 /tmp/ok.txt", "result": "ok"}])
env = Env(s2); verdicts = [{"ok": False, "why": "沒有產出檔案"}, {"ok": True}]
env.llm_handler = lambda system, user: (planner(["只一步"])(system, user) if "任務規劃器" in system else (verdicts.pop(0) if "驗收員" in system else None))
job = mkjob(); run_job(env, job)
check("2 verify-fail then retry with hint", job["status"] == "做完" and "換一個做法" in env.calls[1][1], (job["status"], env.calls[1][1][-80:]))

# 3 遞迴拆小：第一步 too_long → 拆成 2 子步 → 成功
def s3(sid, p, n, e):
    if "大步驟" in p and "子步驟" not in p: return Result("too_long", rounds=15)
    return Result("final", "子步驟完成", rounds=1)
env = Env(s3)
def h3(system, user):
    if "已經太大" in system or "太大或做不起來" in system: return {"steps": [{"title": "子步驟A", "done_when": "a"}, {"title": "子步驟B", "done_when": "b"}]}
    return planner(["大步驟"])(system, user)
env.llm_handler = h3; job = mkjob(); run_job(env, job)
sub = job["plan"][0]["sub"]
check("3 recursion: step decomposed into 2 subs and done", job["status"] == "做完" and len(sub) == 2 and all(x["status"] == "done" for x in sub), job["plan"])

# 4 最深退階：永遠 too_long，遞迴到 MAX_DEPTH 後跳過；另一步仍完成；最後誠實回報部分完成
def s4(sid, p, n, e): return Result("too_long", rounds=15) if "難步驟" in p.split("這一步：")[1].split("\n")[0] or "子" in p.split("這一步：")[1].split("\n")[0] else Result("final", "好", rounds=1)
env = Env(s4)
def h4(system, user):
    if "太大或做不起來" in system: return {"steps": [{"title": "子步1", "done_when": ""}, {"title": "子步2", "done_when": ""}]}
    return planner(["難步驟", "簡單步驟"])(system, user)
env.llm_handler = h4; job = mkjob(); run_job(env, job, timeout=60)
st = {s["title"]: s["status"] for s in job["plan"]}
check("4 graceful: hard step skipped, easy step done, job still finishes", st["難步驟"] == "skipped" and st["簡單步驟"] == "done" and job["status"] == "做完", (st, job["status"]))
check("4 final report mentions skipped (llm_text got 沒成)", any("沒成" in x[2] for x in env.llm_log if x[0] == "text"))

# 5 需要人類：blocked 且含「密碼」→ NeedHuman，等回答；回答後 resume 接續並把回答釘住
def s5(sid, p, n, e): return Result("final", "被擋住：需要 NAS 管理密碼才能繼續", rounds=1) if "使用者的明確要求" not in p else Result("final", "用了密碼完成", rounds=1)
env = Env(s5); env.llm_handler = planner(["登入NAS"]); job = mkjob("檢查NAS備份")
orch, _ = run_job(env, job)
check("5 need-human → 被擋住 awaiting", job["status"] == "被擋住" and job["awaiting"] and "需要你" in job["note"], (job["status"], job["awaiting"]))
ctrl = orch.resume(job, answer="密碼是 hunter2-test"); ctrl.thread.join(30)
check("5 answer pinned + resumed to done", job["status"] == "做完" and "hunter2-test" in env.calls[-1][1], (job["status"], env.calls[-1][1][-120:]))

# 6 補充要求：第 1 步進行中加入補充 → 重排後面的步驟，補充原文釘住
gate = threading.Event(); started = threading.Event()
def s6(sid, p, n, e):
    if n == 1: started.set(); gate.wait(10)
    return Result("final", f"完成{n}", rounds=1)
env = Env(s6)
def h6(system, user):
    if "補充了新要求" in system: return {"steps": [{"title": "依新要求重做B", "done_when": "x"}]}
    return planner(["A", "B", "C"])(system, user)
env.llm_handler = h6; job = mkjob(); orch = O.Orchestrator(env); ctrl = orch.start(job)
started.wait(10); ctrl.supplements.append("只要公司的"); gate.set(); ctrl.thread.join(30)
titles = [s["title"] for s in job["plan"]]
check("6 replan: A kept, rest replaced by new plan, supplement pinned", titles == ["A", "依新要求重做B"] and "只要公司的" in job["pins"] and any("只要公司的" in c[1] for c in env.calls[1:]), (titles, job.get("pins")))

# 7 上下文爆掉：ctx_full → 壓縮前情 → 新 session 接續（不算失敗、不拆）
seq = []
def s7(sid, p, n, e):
    seq.append(sid)
    if n == 1: return Result("ctx_full", "", rounds=5, trace=[{"action": "讀 /tmp/proj/a.py", "result": "ok"}] * 3)
    return Result("final", "接續完成", rounds=2)
env = Env(s7); env.llm_handler = planner(["長步驟"]); job = mkjob(); run_job(env, job)
check("7 ctx_full → carry & continue in new session", job["status"] == "做完" and len(seq) == 2 and seq[0] != seq[1] and "從這裡接續" in env.calls[1][1] and "/tmp/proj/a.py" in env.calls[1][1], (seq, env.calls[1][1][-200:]))

# 8 後端離線自癒：error(ECONNRESET) → 等待 → 恢復後同一步重做，只通知一次
O.Orchestrator.wait_backend.__defaults__
cnt = {"n": 0}
def s8(sid, p, n, e):
    cnt["n"] += 1
    return Result("error", "fetch failed ECONNRESET") if cnt["n"] <= 2 else Result("final", "好了", rounds=1)
env = Env(s8); env.llm_handler = planner(["單步"]); env.backend = True
_orig_wait = threading.Event.wait
class FastEvent(threading.Event):
    def wait(self, timeout=None): return super().wait(0.05 if timeout and timeout > 1 else timeout)
pirun.Handle.__init__.__globals__["threading"].Event  # noqa
h = pirun.Handle; 
def fast_handle_init(self): self.cancel = FastEvent(); self.proc = None; self.last_action = ""; self.rounds = 0; self.ctx_tokens = 0
h.__init__ = fast_handle_init
job = mkjob(); run_job(env, job)
check("8 backend down → self-heal → done, notified once", job["status"] == "做完" and sum("連不上" in m for m in env.sent) == 1, (job["status"], env.sent))

# 9 取消：stop → 狀態由呼叫方處理，orchestrator 不亂回報
gate2 = threading.Event()
def s9(sid, p, n, e):
    gate2.wait(5); return Result("cancelled")
env = Env(s9); env.llm_handler = planner(["A", "B"]); job = mkjob(); orch = O.Orchestrator(env); ctrl = orch.start(job)
time.sleep(0.3); ctrl.handle.cancel.set(); gate2.set(); ctrl.thread.join(10)
check("9 cancel: no 做完 message, no crash", job["status"] != "做完" and not any("做完" in m for m in env.sent), (job["status"], env.sent))

# 10 重啟接續：plan 做到一半（step1 done, step2 doing）→ resume(auto) 從 step2 繼續、不重新規劃
env = Env(lambda sid, p, n, e: Result("final", f"續做{n}", rounds=1)); env.llm_handler = planner(["不該被呼叫"])
job = mkjob(); job["plan"] = [O.new_step("1", "甲"), O.new_step("2", "乙"), O.new_step("3", "丙")]
job["plan"][0]["status"] = "done"; job["plan"][0]["result"] = "甲完成"; job["plan"][1]["status"] = "doing"; job["digest"] = "完成「甲」：甲完成"
orch = O.Orchestrator(env); ctrl = orch.resume(job, auto=True); ctrl.thread.join(20)
check("10 restart resume: skips done step, redoes doing, no replan", job["status"] == "做完" and len(env.calls) == 2 and job["resumes"] == 1 and not any(x[0] == "json" and "規劃" in x[1] for x in env.llm_log[:1]), (job["status"], len(env.calls)))

# 11 規劃失敗退階：planner 回壞 JSON → 整件當一步；那一步 too_long → 遞迴拆小
def s11(sid, p, n, e): return Result("too_long", rounds=15) if n == 1 else Result("final", "子完成", rounds=1)
env = Env(s11)
env.llm_handler = lambda system, user: ({"steps": [{"title": "子1", "done_when": ""}, {"title": "子2", "done_when": ""}]} if "太大或做不起來" in system else None)
job = mkjob(); run_job(env, job)
check("11 planner failure degrades to single step then decomposes", job["status"] == "做完" and job["plan"][0]["sub"], job["plan"])

# 12 對齊：回報了工具紀錄沒有的路徑 → 不採信；第二次有工具紀錄才算完成
def s12(sid, p, n, e):
    if "工具紀錄裡沒有" in p:
        return Result("final", "已寫 /tmp/real.txt", rounds=1, trace=[{"action": "寫 /tmp/real.txt", "result": "ok"}])
    return Result("final", "已寫 /tmp/fake.txt", rounds=1, trace=[{"action": "執行 `ls`", "result": "ok"}])
env = Env(s12); env.llm_handler = planner(["寫檔"]); job = mkjob(); run_job(env, job)
check("12 ungrounded path rejected, grounded retry accepted", job["status"] == "做完" and "/tmp/real.txt" in (job["plan"][0]["result"] or "") and "/tmp/fake.txt" not in (job["plan"][0]["result"] or ""), job["plan"])
check("planner refuses a home-directory survey", "家目錄" in O.PLANNER and "家目錄" in O.STEP_PROMPT and "家目錄" in O.DECOMPOSER)

# 13 工具輸出比摘要長：路徑記在 paths 裡仍算有根據
def s13(sid, p, n, e):
    return Result("final", "看到 /tmp/proj/readme.md", rounds=1, trace=[{"action": "find", "result": "…", "paths": "/tmp/proj/readme.md"}])
env = Env(s13); env.llm_handler = planner(["看目錄"]); job = mkjob(); run_job(env, job)
check("13 path kept beside the short trace is grounded", job["status"] == "做完" and "/tmp/proj/readme.md" in (job["plan"][0]["result"] or ""), job["plan"])

# 14 只排出一步：不開新 session，沿用轉背景前的前景 session（job sid）接著做；帶意圖
env = Env(lambda sid, p, n, e: Result("final", "明天有三個行程", rounds=1)); env.llm_handler = planner(["查明天行事曆"])
job = mkjob("明天行事曆"); job["intent"] = "我先看明天的行事曆。"; run_job(env, job)
check("14 single step continues the foreground session (no job-* session) with the continue prompt",
      job["status"] == "做完" and [c[0] for c in env.calls] == ["x"] and "接著做完" in env.calls[0][1] and "我先看明天的行事曆" in env.calls[0][1], env.calls)

# 15 多步：下一步帶上一步工具輸出原文（檔名照抄）＋意圖；不再鼓勵「多花幾步」
def s15(sid, p, n, e):
    if n == 1:
        return Result("final", "列好了", rounds=1, trace=[{"action": "執行 `ls ~/.pi-tg/tmp`", "result": "costco-a.md costco-b.md",
                                                           "out": "costco-a.md\ncostco-b.md\nnotes.txt"}])
    return Result("final", "比對完：costco-b.md 是重複的", rounds=1)
env = Env(s15); env.llm_handler = planner(["列出草稿", "比對重複"])
job = mkjob("刪除重複的 Costco 草稿"); job["intent"] = "我先列出 Costco 的草稿檔。"; run_job(env, job)
p2 = env.calls[1][1]
check("15 step 2 sees step 1's raw tool output and the intent; no 'spend more steps'",
      "costco-a.md\ncostco-b.md\nnotes.txt" in p2 and "我先列出 Costco 的草稿檔" in p2 and "可以多花幾步" not in O.STEP_PROMPT
      and "沒在工具輸出裡出現過的檔名不得使用" in p2 and "不要重做" in p2, p2[:400])
check("15 every step runs in its own job-* session when there are several", all(c[0].startswith("job-j1-") for c in env.calls), [c[0] for c in env.calls])

# 16 拆小最多一層：1.1 可以；1.1 還做不起來就跳過，不會有 1.1.1
def s16(sid, p, n, e): return Result("too_long", rounds=15) if "難" in p.split("這一步：")[1].split("\n")[0] or n == 1 else Result("final", "好", rounds=1)
env = Env(s16)
env.llm_handler = lambda system, user: ({"steps": [{"title": "難子1", "done_when": ""}, {"title": "難子2", "done_when": ""}]} if "太大或做不起來" in system
                                        else planner(["難步驟", "簡單"])(system, user))
job = mkjob(); run_job(env, job, timeout=60)
ids = [x["id"] for x in job["plan"][0]["sub"]] + [y["id"] for x in job["plan"][0]["sub"] for y in x["sub"]]
check("16 decompose at most one level (1.1 yes, 1.1.x never)", O.MAX_DEPTH == 1 and ids == ["1.1", "1.2"] and job["plan"][0]["status"] == "skipped"
      and job["plan"][1]["status"] == "done", (ids, job["plan"]))

# 17 回報：拿到實際工具動作清單，規則要求沒對應動作的指示說「沒做到」、不提內部字眼
def s17(sid, p, n, e):
    if "整理" in p.split("這一步：")[1].split("\n")[0]:
        return Result("loop", rounds=3, trace=[{"action": "執行 `trash /tmp/x/dup.md`", "result": ""}])
    return Result("final", "列好了", rounds=1, trace=[{"action": "執行 `ls /tmp/x`", "result": "a"}])
env = Env(s17); env.llm_handler = planner(["列出", "整理"]); job = mkjob("列出並寄信給 Sammi")
run_job(env, job, trace=[{"action": "執行 `gog calendar events --tomorrow`", "result": "3 events"}])
rep = [x[2] for x in env.llm_log if x[0] == "text"][-1]
check("17 reporter gets every tool action (foreground, done and failed steps) and is told to say 沒做到 for unmatched instructions",
      "實際執行過的工具動作" in rep and "ls /tmp/x" in rep and "trash /tmp/x/dup.md" in rep and "gog calendar events" in rep and "沒做到" in O.REPORTER and "清單" in O.REPORTER and "session" in O.REPORTER
      and "草稿" in O.REPORTER, rep[-300:])

# 18 規劃：帶意圖與對話脈絡，規則要先解指代詞
env = Env(lambda sid, p, n, e: Result("final", "好", rounds=1)); env.llm_handler = planner(["一步"])
job = mkjob("刪除重複的＋cc 改 loop 進行程"); job["intent"] = "我先看 Costco 行程。"; job["ctx"] = "使用者：Costco 行程跟信都重複了"
run_job(env, job)
plan_in = [x[2] for x in env.llm_log if x[0] == "json" and "任務：" in x[2]][0]
check("18 planner sees intent + recent turns and must resolve references first",
      "我先看 Costco 行程" in plan_in and "Costco 行程跟信都重複了" in plan_in and "指代詞" in O.PLANNER and "不要把任務改成別件事" in O.PLANNER, plan_in)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
