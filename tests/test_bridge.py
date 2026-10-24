#!/usr/bin/env python3
"""橋接層測試（假 pi、假 Telegram，不碰網路）：暫時性錯誤重試、後端離線→先記下→恢復後自動處理、
訊息送不出去→先存起來→網路恢復補送、前景轉背景、重啟接續、路由（狀態/停止/補充/分不出來）、聊天壓縮。"""
import json, os, sys, tempfile, threading, time, urllib.error, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp(prefix="pitg-bridge-test-")
import pi_tg_bridge as B, orchestrator as O
B.DATA = tmp; B.STATE_DIR = tmp + "/state"; B.SESSIONS_DIR = tmp + "/sessions"; B.CHAT_CWD = tmp + "/chat"; B.SKILLS_DIR = tmp
os.makedirs(B.STATE_DIR); B.RETRY_WAITS = (0.1, 0.1, 0.1)
def ev(**k): return json.dumps(k, ensure_ascii=False)
def final_ev(txt): return ev(type="turn_end", message={"role": "assistant", "content": [{"type": "text", "text": txt}], "stopReason": "stop"})
def fake(name, body):
    p = f"{tmp}/{name}"; open(p, "w").write("#!/bin/bash\n" + body); os.chmod(p, 0o755); return p
class TG:
    def __init__(s): s.sent = []; s.cv = threading.Condition()
    def send(s, c, t, r=None, essential=True):
        with s.cv: s.sent.append((c, t)); s.cv.notify_all()
    def typing(s, c): pass
    def flush_outbox(s): pass
def mk(pi, jobs=None):
    tg = TG(); br = B.Bridge(tg, {"OWNER_ID": "1", "PI_BIN": pi, "SPARK_ENV": "/dev/null", "PITG_GROK_SESSION": "0"}, B.State(B.STATE_DIR + f"/s{time.time()}.json"), jobs or B.Jobs(B.STATE_DIR + f"/j{time.time()}.json"))
    br.backend_ok = lambda: True; br.convo = B.ConvoLog(B.STATE_DIR + f"/c{time.time()}.json"); return br, tg
def wait(tg, pred, t=20):
    end = time.time() + t
    with tg.cv:
        while not pred() and time.time() < end: tg.cv.wait(0.2)
    return pred()
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:250])); (not cond) and fails.append(name)

# 1 暫時性錯誤：重試兩次後成功，只通知一次
cnt = f"{tmp}/c1"
pi = fake("flaky", f'n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}; if [ $n -le 2 ]; then echo "fetch failed ECONNRESET" >&2; exit 1; fi; echo \'{final_ev("RECOVERED")}\'')
br, tg = mk(pi); br.foreground(1, 1, "owner", "幫我看這個錯誤", [], 7)
texts = [t for _, t in tg.sent]
check("1 transient → retry → RECOVERED, notified once", texts[-1] == "RECOVERED" and sum(("連不上" in t or "unreachable" in t) for t in texts) == 1, texts)

# 2 後端持續離線 → 先記下（deferred 落檔）→ 恢復後自動處理
pi = fake("down", 'echo "fetch failed ECONNRESET" >&2; exit 1')
br, tg = mk(pi); br.foreground(1, 1, "owner", "幫我查個東西", [], 9)
check("2 down → deferred + told user not to resend", len(br.deferred) == 1 and any("先記著" in t for _, t in tg.sent), (br.deferred, tg.sent))
check("2 deferred persisted on disk", json.load(open(B.STATE_DIR + "/deferred.json"))[0]["text"] == "幫我查個東西")
pi_ok = fake("up", f"echo '{final_ev('我來了')}'"); br.pi = pi_ok
items, br.deferred = br.deferred, []
br.process_message(items[0]["chat_id"], items[0]["uid"], items[0]["mode"], items[0]["text"], items[0]["files"], items[0]["msg_id"])
check("2 after recovery the message is processed automatically", tg.sent[-1][1] == "我來了", tg.sent)

# 3 Telegram 送不出去 → 存 outbox → 恢復後補送
class Flaky(B.Telegram):
    up = False
    def call(self, method, _t=30, **p):
        if not self.up: raise urllib.error.URLError("down")
        self.log.append(p); return {}
real_sleep = time.sleep; time.sleep = lambda s: None
t = Flaky("x", tmp + "/outbox.jsonl"); t.log = []; t.send(1, "重要結果"); 
check("3 failed send parked in outbox", os.path.exists(tmp + "/outbox.jsonl") and "重要結果" in open(tmp + "/outbox.jsonl").read())
Flaky.up = True; t.flush_outbox(); time.sleep = real_sleep
check("3 outbox flushed after network returns", t.log and t.log[-1]["text"] == "重要結果" and not os.path.exists(tmp + "/outbox.jsonl"), t.log)

# 4 前景到第 PROMOTE_AT 輪 → 轉背景（聊天空出來、session 換新、orchestrator 接手拿到前情 trace）
def tool_ev(i): return ev(type="message_end", message={"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": f"echo {i}"}}], "stopReason": "toolUse"})
pi = fake("many", "for i in 1 2 3 4 5 6 7; do echo '" + "'\"$(echo '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"toolCall\",\"name\":\"bash\",\"arguments\":{\"command\":\"echo '$i'\"}}],\"stopReason\":\"toolUse\"}}')\"'" + "'; sleep 0.2; done; sleep 30")
br, tg = mk(pi); started = {}
class FakeOrch:
    def __init__(s): s.calls = []
    def start(s, job, trace=None, resumed=False): s.calls.append((job, trace))
    def describe(s, j): return "規劃中"
    def get(s, i): return None
br.orch = FakeOrch(); sid0 = br.state.sid(1)
br.llm_text = lambda *a, **k: '{"say":"我先把九月的發票收成一張表。","work":true,"know":false}'
br.foreground(1, 1, "owner", "把九月發票整理成表", [], 11)
check("4 promoted: session rotated, orchestrator got job+trace (start message is the orchestrator's)", br.state.sid(1) != sid0 and len(br.orch.calls) == 1 and len(br.orch.calls[0][1]) >= 2 and not br.fg, (tg.sent, br.orch.calls))
check("4 fast model speaks first, without quoting the command", any("收成一張表" in t for _, t in tg.sent) and not any("先執行" in t or "我來處理" in t or "echo 1" in t for _, t in tg.sent), tg.sent)
pi = fake("know", "for i in 1 2 3 4; do echo '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"toolCall\",\"name\":\"bash\",\"arguments\":{\"command\":\"ls '$i'\"}}],\"stopReason\":\"toolUse\"}}'; sleep 0.1; done; sleep 30")
br, tg = mk(pi); br.orch = FakeOrch(); br.llm_text = lambda *a, **k: "我只看過 toolhub-publish。"
br.foreground(1, 1, "owner", "你對於toolhubmaster 的了解到哪邊", [], 12)
check("4b knowledge question is answered from notes, not turned into a job", any("筆記裡沒有這項" in t for _, t in tg.sent) and br.orch.calls == [] and not any("多做幾步" in t for _, t in tg.sent), tg.sent)
pi = fake("recognize", f"echo '{final_ev('SHOULD_NOT_RUN')}'")
br, tg = mk(pi); br.orch = FakeOrch()
br.llm_text = lambda *a, **k: '{"say":"我來處理，先執行 ls","work":true,"know":true}'
br.foreground(1, 1, "owner", "你認識toolhubmaster 還有我的portfolio 嗎", [], 13)
check("4c recognition without notes does not open a search", any("不會" in t and "掃" in t for _, t in tg.sent) and br.orch.calls == [] and not any("SHOULD_NOT_RUN" in t or "先執行" in t or "我來處理" in t for _, t in tg.sent), tg.sent)
open(B.DATA + "/brief.md", "w").write("使用者是 Howard Wu。作品集程式碼在 /tmp/proj-site。網頁是 https://example.com/works/task-agent/ 。設計筆記是 https://toolhub.example/note 。\n")
br, tg = mk(fake("blind", f"echo '{final_ev('主模型答了')}'"))
br.llm_text = lambda *a, **k: '{"say":"我不知道你是誰，也沒看過你的作品集。","work":false,"know":false}'
say, work, know = br.first_words("所以你知道我是誰嗎 我的作品集網頁的程式碼在哪 還有網址", {"provider": "grok", "model": "grok-build-0.1"})
check("4d brief answers who he is and where the page is", work is False and "Howard Wu" in say and "/tmp/proj-site" in say and "https://example.com/works/task-agent/" in say and "不知道" not in say and "我的名字是" not in say, (say, work))
br.foreground(1, 1, "owner", "所以你知道我是誰嗎 我的作品集網頁的程式碼在哪 還有網址", [], 14)
check("4d the brief is sent and the main model stays idle", any("Howard Wu" in t and "example.com" in t for _, t in tg.sent) and not any("主模型答了" in t or "不知道你是誰" in t for _, t in tg.sent), tg.sent)
say, work, know = br.first_words("你是什麼模型", {"provider": "grok", "model": "grok-build-0.1"})
check("4e model question is the real id and does not open tools", say == "我是 grok-build-0.1。" and work is False and know is False, (say, work, know))
br.foreground(1, 1, "owner", "你是什麼模型", [], 15)
check("4e foreground answers the resolved id and stays idle", any(t == "我是 spark-main。" for _, t in tg.sent) and not any("主模型答了" in t for _, t in tg.sent), tg.sent)
say, work, _ = br.first_words("你認識我的 portfolio 嗎", {"provider": "grok", "model": "grok-build-0.1"})
check("4f portfolio is answered from the brief", work is False and "/tmp/proj-site" in say and "example.com/works/task-agent" in say, (say, work))
br.foreground(1, 1, "owner", "你好", [], 16)
check("4g a greeting is one line and does not start the main model", any(t == "你好。" for _, t in tg.sent) and not any("主模型答了" in t for _, t in tg.sent), tg.sent)
br.llm_text = lambda *a, **k: '{"say":"我來處理，先執行 ls","work":true,"know":false}'
say, work, _ = br.first_words("把九月的發票收成一張表", {"provider": "grok", "model": "grok-build-0.1"})
check("4h a canned opener is dropped", say == "" and work is True, (say, work))
br.llm_text = lambda *a, **k: '{"say":"請把會議紀錄的內容給我，我幫你整理成三點。","work":true,"know":false}'
say, work, _ = br.first_words("幫我把桌上的會議紀錄整理成三點", {"provider": "grok", "model": "grok-build-0.1"})
check("4h a request for the missing text does not open tools", "三點" in say and work is False and "我來處理" not in say, (say, work))
prompt = br.build_prompt(1, "owner", "網址呢")
check("known facts follow the do-not-invent line", prompt.find("Do not invent") < prompt.find("可以直接說") < prompt.find("網址呢"), prompt[prompt.find("Do not"):prompt.find("網址呢")+4])

# 5 路由：狀態（不經模型）/ 補充 / 無關 / 分不出來→當新的事（不反問）/ 停止
br, tg = mk(fake("noop", "true")); jobs = br.jobs
j = jobs.add(1, "發票整理", "整理九月發票成表", "sid"); jobs.update(j, status="還在跑")
llm_answer = {"v": {"route": "other"}}; br.llm_json = lambda s, u, m=300, tier="fast": llm_answer["v"]
class Orch2:
    def __init__(s): s.sup = []; s.stopped = []
    def get(s, i): return type("C", (), {"supplements": s.sup})()
    def stop(s, job): s.stopped.append(job["name"])
    def describe(s, job): return "完成 1/3 步"
    def resume(s, job, auto=False, answer=""): s.resumed = (job["name"], answer)
br.orch = Orch2()
check("5 status query answered without model", br.route(1, "現在在忙什麼", [], 5) and "進行中" in tg.sent[-1][1] and "發票整理" in tg.sent[-1][1], tg.sent)
llm_answer["v"] = {"route": "addto", "job": "發票整理"}
check("5 supplement → added to job (not a new one)", br.route(1, "只要公司的", [], 6) and br.orch.sup == ["只要公司的"] and "已加進" in tg.sent[-1][1], (br.orch.sup, tg.sent[-1]))
llm_answer["v"] = {"route": "other"}
check("5 unrelated → not routed (foreground handles it)", br.route(1, "今天星期幾", [], 7) is False)
llm_answer["v"] = {"route": "unclear"}; n5 = len(tg.sent)
check("5 unclear → not routed, no 加/新 question (runs as its own thing)", br.route(1, "改成十月", [], 8) is False and len(tg.sent) == n5, tg.sent[n5:])
check("5 stop by name only stops that one", br.route(1, "發票整理先停", [], 10) and br.orch.stopped == ["發票整理"] and j["status"] == "被擋住" and j["note"] == "依指示停止", (br.orch.stopped, j["status"]))

# 6 等待回答的工作：他的回覆直接接續（並把回答釘住）
j2 = jobs.add(1, "NAS", "檢查NAS", "s2"); jobs.update(j2, status="被擋住", note="需要你：NAS 密碼", awaiting=True)
llm_answer["v"] = {"route": "answer"}
check("6 awaiting job: reply resumes it with the answer", br.route(1, "密碼是 abc", [], 11) and br.orch.resumed == ("NAS", "密碼是 abc"), getattr(br.orch, "resumed", None))

# 7 重啟接續：ACTIVE 的工作自動接著做，並說一聲；反覆中斷 3 次就停下
br, tg = mk(fake("noop2", "true")); j3 = br.jobs.add(1, "長工作", "g", "s3"); br.jobs.update(j3, status="還在跑", plan=[O.new_step("1", "甲")])
br.orch = Orch2(); br.orch.resume = lambda job, auto=False, answer="": setattr(br, "_r", (job["name"], auto)) or job.update(resumes=job.get("resumes", 0) + 1)
br.recover()
check("7 recover: auto-resume + told user", br._r == ("長工作", True) and any("接著剛才的進度" in t for _, t in tg.sent), (getattr(br, "_r", None), tg.sent))
j3["resumes"] = 3; br.recover()
check("7 crash-loop guard: stops auto-resuming after 3", j3["status"] == "被擋住", j3["status"])

# 8 聊天壓縮：摘要＋最近幾則原文，下一則帶前情；/new 清掉
sess_dir = B.SESSIONS_DIR + "/p"; os.makedirs(sess_dir, exist_ok=True)
rows = [{"message": {"role": "user", "content": [{"type": "text", "text": "我的專案代號是紫色獅子，資料夾 /tmp/proj-lion"}]}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "好，記住了"}]}}] + \
       [{"message": {"role": "user" if i % 2 == 0 else "assistant", "content": [{"type": "text", "text": f"閒聊{i}"}]}} for i in range(8)]
sid = br.state.sid(1); open(f"{sess_dir}/2026_{sid}.jsonl", "w").write("\n".join(json.dumps(r) for r in rows))
br.llm_text = lambda system, user, max_tokens=300, timeout=90: "【大方向】專案紫色獅子\n- 資料夾 /tmp/proj-lion"
s = br.compact_chat(1, sid, 50000)
check("8 compaction keeps pinned facts and anchors", "紫色獅子" in s and "/tmp/proj-lion" in s, s)
note = br.turn_model_note({"provider": "grok", "model": "grok-build-0.1"})
check("turn note names Grok and not a hardcoded local model", "grok-build-0.1" in note and "Qwen" not in note, note)
check("local turn note names the local id", "spark-main" in br.turn_model_note({"provider": "local", "model": "spark-main"}))
p = br.build_prompt(1, "owner", "接著做")
check("8 next prompt carries summary + recent verbatim, once", "前情摘要" in p and "最近幾則對話原文" in p and "閒聊7" in p and "前情摘要" not in br.build_prompt(1, "owner", "再來"), p[:200])
br.llm_text = lambda *a, **k: None   # 壓縮模型失效 → 規則式退階仍保住錨點
s2 = br.cx.compress("舊：檔案在 /tmp/proj-lion/a.py", "新：網址 https://example.com/x", 300)
check("8 compaction degrade (llm down) keeps anchors", "/tmp/proj-lion/a.py" in s2 and "https://example.com/x" in s2, s2)
# 8b 只有兩則訊息（全落在「最近原文」）也要進摘要；再壓一次（遞迴）前情不丟，前情摘要區塊本身不被當敘事重複吃
rows2 = [{"message": {"role": "user", "content": [{"type": "text", "text": "我的專案代號是紫色獅子，資料夾 /tmp/proj-lion"}]}},
         {"message": {"role": "assistant", "content": [{"type": "text", "text": "好，記住了"}]}}]
sid2 = "tg-9-0"; open(f"{sess_dir}/x_{sid2}.jsonl", "w").write("\n".join(json.dumps(r) for r in rows2))
seen_user = []
br.llm_text = lambda system, user, max_tokens=300, timeout=90: (seen_user.append(user), "【大方向】紫色獅子；/tmp/proj-lion")[1]
s8b, rec = br.cx.compact_chat(B.SESSIONS_DIR, sid2, "")
check("8b two-message session still summarized (not lost to 'recent')", "紫色獅子" in seen_user[-1] and "/tmp/proj-lion" in s8b, seen_user[-1][:120])
head = "【前情摘要（上下文已壓縮）】\n【大方向】紫色獅子\n【最近幾則對話原文】\n…"
rows3 = [{"message": {"role": "user", "content": [{"type": "text", "text": head + "\n新訊息：繼續"}]}}, {"message": {"role": "assistant", "content": [{"type": "text", "text": "好"}]}}]
sid3 = "tg-9-1"; open(f"{sess_dir}/y_{sid3}.jsonl", "w").write("\n".join(json.dumps(r) for r in rows3))
s8c, _ = br.cx.compact_chat(B.SESSIONS_DIR, sid3, s8b)
check("8c recursive compaction: prev summary passed separately, carried-in head not re-ingested as narrative", "【前情摘要" not in seen_user[-1].split("【新增內容】")[1] and "紫色獅子" in seen_user[-1], seen_user[-1][:200])
# 8d 殘缺 session（結尾是沒有結果的工具呼叫）→ 下一則自動壓縮換新，不接在後面
sid4 = br.state.sid(1)
open(f"{sess_dir}/z_{sid4}.jsonl", "w").write("\n".join(json.dumps(r) for r in [
    {"message": {"role": "user", "content": [{"type": "text", "text": "幫我找檔案"}]}},
    {"message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "find ~"}}]}}]))
br.llm_text = lambda *a, **k: "【大方向】使用者要找檔案；上次 find ~ 卡住被中止"
check("8d dangling detected", br.cx.dangling(B.SESSIONS_DIR, sid4))
new_sid = br.pick_sid(1)
check("8d dangling → compacted + rotated before next message", new_sid != sid4 and br.state.d["fresh"].get("1") and "find ~" in br.state.d["summary"]["1"], (new_sid, br.state.d.get("summary")))
check("8d healthy session not touched", not br.cx.dangling(B.SESSIONS_DIR, "tg-9-0"))
br.command("/new", 1, 1, True); check("8 /new clears carried summary", "1" not in br.state.d["summary"] and not br.state.d["fresh"].get("1"))
# 9 訂閱失敗（401/403/逾時，且還沒動工具）→ 前景與背景都改用地端重跑，log 一行 WARNING 說明原因
B.sync_pi_provider = lambda *a, **k: None            # 不碰使用者的 ~/.pi/agent/models.json
class Grab(logging.Handler):
    def __init__(s): super().__init__(); s.msgs = []
    def emit(s, rec): s.msgs.append(rec.getMessage())
grab = Grab(); logging.getLogger("pi-tg").addHandler(grab)
def err_ev(msg): return ev(type="turn_end", message={"role": "assistant", "content": [], "stopReason": "error", "errorMessage": msg})
def sub_pi(name, grok_body): return fake(name, f'case "$*" in *"--provider grok"*) {grok_body} ;; *) echo \'{final_ev("LOCAL_OK")}\' ;; esac')
def mk_sub(pi, fg_wall=None):
    br, tg = mk(pi); br.env["XAI_API_KEY"] = "xai-test"; br.orch = FakeOrch()
    br.llm_text = lambda *a, **k: '{"say":"","work":true,"know":false}'
    if fg_wall:
        orig = br.tune; br.tune = lambda: dict(orig(), fg_wall=fg_wall)
    return br, tg
os.environ["PITG_PROVIDER"] = "grok"
M403 = '403 "The OAuth2 access token could not be validated."'
for label, msg, wall in (("403", M403, None), ("401", "401 Unauthorized", None), ("timeout", None, 2)):
    br, tg = mk_sub(sub_pi("sub_" + label, "sleep 30" if msg is None else f"echo '{err_ev(msg)}'"), wall)
    grab.msgs.clear()
    br.foreground(1, 1, "owner", "幫我整理今天的待辦清單", [], 21)
    want = "auth" if msg else "timeout"
    check(f"9 {label} on grok → foreground answered by local", bool(tg.sent) and tg.sent[-1][1] == "LOCAL_OK", tg.sent)
    check(f"9 {label} → one WARNING with reason '{want}', no key in log", sum("grok run failed (" in m for m in grab.msgs) == 1 and any(f"grok run failed ({want}:" in m for m in grab.msgs) and not any("xai-test" in m for m in grab.msgs), grab.msgs)
br, tg = mk_sub(sub_pi("sub_run403", f"echo '{err_ev(M403)}'"))
r = br.run_pi(None, "sid-sub-run", "hi", B.pirun.Handle(), 0, 20)
check("9 orchestrator path: 403 → local final", r.kind == "final" and r.text == "LOCAL_OK", (r.kind, r.text))
br, tg = mk_sub(sub_pi("sub_runto", "sleep 30"))
r = br.run_pi(None, "sid-sub-to", "hi", B.pirun.Handle(), 0, 2)
check("9 orchestrator path: timeout → local final", r.kind == "final" and r.text == "LOCAL_OK", (r.kind, r.text))
tool_then = fake("sub_tool", f'case "$*" in *"--provider grok"*) echo \'{tool_ev(1)}\'; echo \'{err_ev(M403)}\' ;; *) echo \'{final_ev("LOCAL_OK")}\' ;; esac')
br, tg = mk_sub(tool_then)
r = br.run_pi(None, "sid-sub-tool", "hi", B.pirun.Handle(), 0, 20)
check("9 failure after a tool call is not retried (side effects)", r.kind == "error" and r.rounds == 1 and "403" in r.text, (r.kind, r.rounds, r.text))
# 10 首字逾時（PITG_FIRST_TOKEN_S，測試縮成 2 秒）：grok 一個字都沒出來 → 前景與背景都改用地端；有輸出或動了工具後不套
delta10 = ev(type="message_update", assistantMessageEvent={"type": "text_delta", "delta": "嗨"})
tool10 = ev(type="tool_execution_start")
def mk_ft(pi):
    br, tg = mk_sub(pi); br.env["PITG_FIRST_TOKEN_S"] = "2"; return br, tg
br, tg = mk_ft(sub_pi("ft_silent", "sleep 30"))
grab.msgs.clear()
br.foreground(1, 1, "owner", "幫我整理今天的待辦清單", [], 22)
check("10 silent grok → foreground answered by local", bool(tg.sent) and tg.sent[-1][1] == "LOCAL_OK", tg.sent)
check("10 one WARNING with reason 'first-token timeout 2s'", sum("grok run failed (" in m for m in grab.msgs) == 1 and any(m.startswith("grok run failed (first-token timeout 2s), retrying on local") for m in grab.msgs), grab.msgs)
br, tg = mk_ft(sub_pi("ft_run", "sleep 30"))
t0 = time.time(); r = br.run_pi(None, "sid-ft-run", "hi", B.pirun.Handle(), 0, 20)
check("10 orchestrator path: silent grok → local final, cut at ~2s not wall", r.kind == "final" and r.text == "LOCAL_OK" and time.time() - t0 < 15, (r.kind, r.text, round(time.time() - t0, 1)))
streaming = f"sleep 0.5; echo '{delta10}'; sleep 4; echo '{final_ev('GROK_OK')}'"
br, tg = mk_ft(sub_pi("ft_streaming", streaming))
r = br.run_pi(None, "sid-ft-ok", "hi", B.pirun.Handle(), 0, 20)
check("10 grok output at 0.5s, silent 4s later → no first-token fallback", r.kind == "final" and r.text == "GROK_OK", (r.kind, r.text))
tooled = f"echo '{tool10}'; sleep 4; echo '{final_ev('GROK_OK')}'"
br, tg = mk_ft(sub_pi("ft_tool", tooled))
r = br.run_pi(None, "sid-ft-tool", "hi", B.pirun.Handle(), 0, 20)
check("10 tool started, silent 4s later → no first-token fallback", r.kind == "final" and r.text == "GROK_OK", (r.kind, r.text))
os.environ.pop("PITG_PROVIDER", None); os.environ.pop("PITG_FG_WALL", None)
br, tg = mk(fake("noop9", "true"))
check("9 default with no state file: provider local, model spark-main", br.tune()["provider"] == "local" and br.route_public()["provider"] == "local" and br.route_public()["model"] == "spark-main", (br.tune()["provider"], br.route_public()))
logging.getLogger("pi-tg").removeHandler(grab)
# 11 對話體驗：合併連發、平行不排隊、前文、問進度、喊停、同一件的催促/補充、去重、Telegram 原地更新
def nlines(p): return len(open(p).read().splitlines()) if os.path.exists(p) else 0
B.DEBOUNCE = 0.4
br, tg = mk(fake("noop11", "true")); got = []
br.process_message = lambda *a, **k: got.append(a)
br.dispatch(1, True, "owner", "差我今天行事曆", [], 1, 1); time.sleep(0.15); br.dispatch(1, True, "owner", "查", [], 2, 1)
br.dispatch(5, True, "chat", "別人", [], 3, 5); time.sleep(1.2)
check("11a burst merged into one message (replies to the last)", [g for g in got if g[0] == 1] == [(1, 1, "owner", "差我今天行事曆\n查", [], 2)], got)
check("11a another chat is not merged in", any(g[0] == 5 and g[3] == "別人" for g in got), got)
sids_l = f"{tmp}/sids11l"
br, tg = mk(fake("cal11", f'''echo "$*" | grep -o -- '--session-id [^ ]*' >> {sids_l}; echo '{final_ev("今天 10:00 週會")}' '''))
br.llm_text = lambda *a, **k: '{"say":"我先看今天的行事曆。","work":true,"know":false}'
br.dispatch(1, True, "owner", "差我今天行事曆", [], 41, 1); time.sleep(0.25); br.dispatch(1, True, "owner", "查", [], 42, 1)
wait(tg, lambda: any("週會" in t for _, t in tg.sent), 10)
check("11l 14:53 replay: one ack naming the first step, one answer, one pi run, no queue notice or ask-back",
      [t for _, t in tg.sent] == ["我先看今天的行事曆。", "今天 10:00 週會"] and nlines(sids_l) == 1, (tg.sent, nlines(sids_l)))
B.DEBOUNCE = 0

sids = f"{tmp}/sids11"
conc = f'''echo "$*" | grep -o -- '--session-id [^ ]*' >> {sids}
last="${{@: -1}}"
case "$last" in
  *慢工作) sleep 3; echo '{final_ev("SLOW_DONE")}' ;;
  *只要下午的) echo '{final_ev("FOLLOW_DONE")}' ;;
  *) echo '{final_ev("FAST_DONE")}' ;;
esac'''
pi11 = fake("conc11", conc)
def mk11():
    br, tg = mk(pi11); br.orch = FakeOrch(); br.llm_text = lambda *a, **k: '{"say":"","work":true,"know":false}'; return br, tg
br, tg = mk11()
th = threading.Thread(target=br.foreground, args=(1, 1, "owner", "幫我跑一個慢工作", [], 30)); th.start(); time.sleep(0.8)
t0 = time.time(); br.foreground(1, 1, "owner", "今天星期幾", [], 31); dt = time.time() - t0
texts = [t for _, t in tg.sent]
check("11b unrelated message answered while the first still runs (parallel, not queued)", "FAST_DONE" in texts and "SLOW_DONE" not in texts and dt < 2.5, (texts, round(dt, 1)))
check("11b no 'previous one still running' notice", not any("前一件" in t for t in texts), texts)
th.join(10); ids = open(sids).read()
check("11b slow one finishes in the main session; the other used a side session", "SLOW_DONE" in [t for _, t in tg.sent] and ids.count("--session-id tg-1-") == 1 and ids.count("--session-id side-1-") == 1, ids)

br, tg = mk11(); runs0 = nlines(sids)
th = threading.Thread(target=br.foreground, args=(1, 1, "owner", "幫我跑一個慢工作", [], 32)); th.start(); time.sleep(0.8)
check("11c 好了沒 → instant status of the running turn", br.route(1, "好了沒", [], 33) and "幫我跑一個慢工作" in tg.sent[-1][1] and "處理中" in tg.sent[-1][1], tg.sent[-1:])
br.llm_json = lambda s, u, m=300, tier="fast": {"route": "same", "item": 1, "say": "還在跑那個慢工作，好了就給你。"}
check("11d nudge about the running turn → one short ack, no new run", br.route(1, "快點", [], 34) and tg.sent[-1][1].startswith("還在跑") and nlines(sids) == runs0 + 1, (tg.sent[-1:], nlines(sids)))
br.llm_json = lambda s, u, m=300, tier="fast": {"route": "addto", "item": 1}
check("11e supplement to the running turn is noted, not run separately", br.route(1, "只要下午的", [], 35) and "記下了" in tg.sent[-1][1] and nlines(sids) == runs0 + 1, tg.sent[-1:])
th.join(15); texts = [t for _, t in tg.sent]
check("11e ...and handled right after the turn finishes", "SLOW_DONE" in texts and "FOLLOW_DONE" in texts and texts.index("SLOW_DONE") < texts.index("FOLLOW_DONE") and not br.fg, texts)

br, tg = mk11(); runs0 = nlines(sids)
th = threading.Thread(target=br.foreground, args=(1, 1, "owner", "幫我跑一個慢工作", [], 36)); th.start(); time.sleep(0.8)
check("11f 停 stops the running foreground turn", br.route(1, "停", [], 37) and "停下了" in tg.sent[-1][1], tg.sent[-1:])
th.join(10)
check("11f ...and it never answers afterwards", "SLOW_DONE" not in [t for _, t in tg.sent] and not br.fg, tg.sent)
check("11f 停車場 is not a stop command", not B.STOP_SHORT.fullmatch("停車場在哪") and not B.STOP_RE.search("停車場在哪"))

br, tg = mk(fake("noop11g", "true")); seen = []
br.convo.add(1, "u", "差我今天行事曆"); br.convo.add(1, "a", "今天 10:00 週會、14:00 客戶會議"); br.convo.add(1, "u", "那明天呢")
br.llm_text = lambda system, user, *a, **k: (seen.append(user), '{"say":"查什麼呢？","work":false,"know":false}')[1]
say, work, _ = br.first_words("那明天呢", {"provider": "local", "model": "spark-main"}, 1)
check("11g fast model sees the recent conversation", "差我今天行事曆" in seen[-1] and "14:00 客戶會議" in seen[-1] and seen[-1].count("那明天呢") == 1, seen[-1])
check("11g a short follow-up is not answered with a question back; the main model works", work is True and say == "", (say, work))
p_side = br.build_prompt(1, "owner", "那明天呢", side=True)
check("11h side-session prompt carries the recent turns", "最近對話" in p_side and "14:00 客戶會議" in p_side and p_side.rstrip().endswith("那明天呢"), p_side[:300])
br.convo.mark(1); br.convo.add(1, "a", "做完：『週報』草稿在 /tmp/report.md"); br.convo.add(1, "a", "主 session 自己的回覆", "main"); br.convo.add(1, "u", "好")
p1 = br.build_prompt(1, "owner", "好"); p2 = br.build_prompt(1, "owner", "再來")
check("11h main session gets what happened outside it, once, without its own replies", "週報" in p1 and "另外發生" in p1 and "主 session 自己的回覆" not in p1 and "週報" not in p2, (p1[:300], p2[:200]))
n = len(tg.sent); br.send(1, "同一句話"); br.send(1, "同一句話")
check("11i identical message within a minute is sent once", [t for _, t in tg.sent[n:]] == ["同一句話"], tg.sent[n:])
br.process_message = B.Bridge.process_message.__get__(br); br.route = lambda *a: False; br.foreground = lambda c, u, m, t, f, i: br.send(c, "同一句話")
br.process_message(1, 1, "owner", "再說一次", [], 50)
check("11i ...but when he asks again, the same answer is sent again", [t for _, t in tg.sent[n:]] == ["同一句話", "同一句話"], tg.sent[n:])

class OrchK:
    def __init__(s): s.ctrl = type("C", (), {"supplements": []})(); s.jobs = []
    def start(s, job, trace=None, resumed=False): s.jobs.append(job); return s.ctrl
    def describe(s, j): return ""
    def get(s, i): return s.ctrl
br, tg = mk(fake("noop11k", "true")); br.orch = OrchK(); sid0 = br.state.sid(1)
w = {"label": "那明天呢", "text": "那明天呢", "started": time.time(), "handle": B.pirun.Handle(), "extras": ["只要下午"], "side": True, "job": None}
br.fg[1] = [w]; br.convo.add(1, "u", "差我今天行事曆"); br.convo.add(1, "u", "那明天呢")
br.promote(1, "那明天呢", "side-1-x", [], w)
check("11k promote from a side session: main session kept, supplements carried, short goal gets context",
      br.state.sid(1) == sid0 and br.orch.ctrl.supplements == ["只要下午"] and "差我今天行事曆" in br.orch.jobs[0].get("ctx", "") and not br.fg and w["job"] is br.orch.jobs[0],
      (br.state.sid(1), br.orch.ctrl.supplements, br.orch.jobs[0].get("ctx"), br.fg))

class TGK(B.Telegram):
    def __init__(s): super().__init__("x"); s.log = []; s.n = 100
    def call(s, method, _t=30, **p):
        s.log.append((method, p.get("text"), p.get("message_id")))
        if method == "sendMessage":
            s.n += 1; return {"result": {"message_id": s.n}}
        return {"result": True}
tk = TGK(); tk.send(1, "開始", key="j"); tk.send(1, "進度 1/3", key="j")
check("11j keyed progress edits the same Telegram message", [m for m, *_ in tk.log] == ["sendMessage", "editMessageText"] and tk.log[1][2] == 101, tk.log)
tk.send(1, "別的回覆"); tk.send(1, "進度 2/3", key="j")
check("11j once buried under another message, progress becomes a fresh message", [m for m, *_ in tk.log][-2:] == ["sendMessage", "sendMessage"] and tk.keyed[(1, "j")] == 103, tk.log)
tk.send(1, "進度 3/3", key="j")
check("11j ...and later progress edits that newest one", tk.log[-1][0] == "editMessageText" and tk.log[-1][2] == 103, tk.log)
# 12 opt5：查詢類在前景答完（PROMOTE_AT=6）、轉背景帶意圖與前文、寄信硬擋
import mailgate as M, subprocess, pirun as P
def rounds_pi(name, n, tail):
    say = "我先看明天的行事曆"
    one = ev(type="message_end", message={"role": "assistant", "stopReason": "toolUse", "content": [
        {"type": "text", "text": say}, {"type": "toolCall", "name": "bash", "arguments": {"command": "gog calendar events --tomorrow #N"}}]})
    return fake(name, "for i in " + " ".join(str(i) for i in range(1, n + 1)) + "; do echo '" + one.replace("#N", "'$i'") + "'; sleep 0.05; done; " + tail)
class FakeOrch2:
    def __init__(s): s.calls = []
    def start(s, job, trace=None, resumed=False): s.calls.append((job, trace))
    def describe(s, j): return "規劃中"
    def get(s, i): return None
br, tg = mk(rounds_pi("five", 5, f"echo '{final_ev('明天有三個行程')}'")); br.orch = FakeOrch2()
br.llm_text = lambda *a, **k: '{"say":"我先看明天的行事曆。","work":true,"know":false}'
br.foreground(1, 1, "owner", "明天行事曆", [], 31)
check("12a calendar query with 5 tool rounds is answered in the foreground (PROMOTE_AT=6, no background job)",
      B.PROMOTE_AT == 6 and br.orch.calls == [] and tg.sent[-1][1] == "明天有三個行程", (tg.sent, br.orch.calls))
br, tg = mk(rounds_pi("seven", 7, "sleep 30")); br.orch = FakeOrch2()
br.convo.add(1, "u", "Costco 行程有兩筆重複，那封信也寄了兩次"); br.convo.add(1, "a", "我看到兩筆 Costco 行程。")
br.llm_text = lambda *a, **k: '{"say":"我先看 Costco 那兩筆行程。","work":true,"know":false}'
br.convo.add(1, "u", "刪除重複的，cc 改 loop 進行程")
br.foreground(1, 1, "owner", "刪除重複的，cc 改 loop 進行程", [], 32)
job = br.orch.calls[0][0] if br.orch.calls else {}
check("12b round 6 promotes; job carries intent (opener + model's last words) and recent turns for references",
      len(br.orch.calls) == 1 and "我先看 Costco 那兩筆行程" in job.get("intent", "") and "我先看明天的行事曆" in job.get("intent", "")
      and "Costco 行程有兩筆重複" in job.get("ctx", "") and len(br.orch.calls[0][1]) >= 5, (job, tg.sent))

os.environ["PITG_HOME"] = tmp                                     # pitg-mail 跟橋接層看同一個 state/mail
maild = os.path.join(B.STATE_DIR, "mail"); gl = tmp + "/gog.log"
os.environ["PITG_REAL_GOG"] = fake("gog", f'echo "$@" >> {gl}; echo \'{{"id":"m1"}}\'')
d = M.new_draft("sammi@example.com", "Costco 行程", "Hi\\n明天見", cc="h@example.com", d=maild)
ok, msg = M.send(d["id"], d=maild)
rc = subprocess.run([sys.executable, os.path.join(os.path.dirname(B.__file__), "mailgate.py"), "send", "--confirmed", d["id"]], capture_output=True, text=True)
check("12c no confirmation in chat → --confirmed alone does not send (gog never called)",
      not ok and "沒寄" in msg and rc.returncode == 3 and "沒寄" in rc.stdout and not os.path.exists(gl) and d["body"] == "Hi\n明天見", (msg, rc.stdout, rc.returncode))
out = subprocess.run([sys.executable, os.path.join(os.path.dirname(B.__file__), "mailgate.py"), "draft", "--to", "x@example.com", "--subject", "s", "--body", "b"],
                     capture_output=True, text=True).stdout
check("12d draft prints the whole draft and how to send after confirmation, sends nothing", "還沒寄" in out and "--confirmed" in out and not os.path.exists(gl), out)

import shutil; shutil.rmtree(maild)                                 # 上面兩份是單元測試用的，這段從乾淨的草稿夾開始
br, tg = mk(fake("noop", "exit 0")); seen = []
br.foreground = lambda chat_id, uid, mode, text, *a, **k: seen.append(text)
br.convo.add(1, "u", "幫我寫信跟 Sammi 說明天 Costco 的事"); time.sleep(0.02)
d2 = M.new_draft("sammi@example.com", "Costco", "明天十點", d=maild); time.sleep(0.02)
br.process_message(1, 1, "owner", "不要寄出", [], 41)
check("12e a negative ('不要寄出') approves nothing", not M._load(maild, d2["id"])["approved"] and "確認寄出草稿" not in seen[-1], seen)
br.process_message(1, 1, "owner", "時間改成十一點", [], 42); time.sleep(0.02)
d3 = M.new_draft("sammi@example.com", "Costco", "明天十一點見", d=maild); time.sleep(0.02)
br.process_message(1, 1, "owner", "看起來不錯", [], 43); time.sleep(0.02)
br.process_message(1, 1, "owner", "好，寄出", [], 44)
check("12f '好，寄出' (even one message later) approves the revised draft only, and tells the model its id",
      M._load(maild, d3["id"])["approved"] and not M._load(maild, d2["id"])["approved"] and d3["id"] in seen[-1] and d2["id"] not in seen[-1]
      and "--confirmed" in seen[-1], (seen[-1], M._load(maild, d2["id"])))
ok1, m1 = M.send(d3["id"], d=maild); ok2, m2 = M.send(d3["id"], d=maild)
sent = open(gl).read() if os.path.exists(gl) else ""
check("12h confirmed draft is sent once, verbatim recipients; a second send is refused",
      ok1 and "已寄出" in m1 and not ok2 and "寄過" in m2 and sent.count("send ") == 1 and "--to sammi@example.com" in sent, (m1, m2, sent))
M._save(maild, dict(M._load(maild, d2["id"]), sent=time.time()))       # 舊稿另外處理掉，只剩寄過的
br.process_message(1, 1, "owner", "寄出", [], 45)
check("12i sent drafts are never re-approved", "確認寄出草稿" not in seen[-1], seen[-1])
d4 = M.new_draft("x@example.com", "s", "b", d=maild); dr = M._load(maild, d4["id"]); dr["approved"] = dr["created"] + 1; dr["created"] -= 4000; dr["approved"] -= 4000; M._save(maild, dr)
check("12j an approval older than 30 minutes no longer sends", not M.send(d4["id"], d=maild)[0])

guard = os.path.join(os.path.dirname(B.__file__), "tools", "gog-guard"); open(gl, "w").close()
blocked = [subprocess.run(["bash", guard] + a, capture_output=True, text=True) for a in (["send", "--to", "x"], ["gmail", "send", "--to", "x"], ["gmail", "drafts", "send", "d"], ["--gmail-no-send=false", "send"],
                                                                                  ["calendar", "create", "primary", "--send-updates=all"], ["calendar", "update", "p", "e", "--send-updates", "externalOnly"])]
passed = subprocess.run(["bash", guard, "-a", "acct", "send", "--to", "x"], capture_output=True, text=True)
cal = subprocess.run(["bash", guard, "calendar", "events", "--tomorrow"], capture_output=True, text=True)
calls = open(gl).read()
check("12k gog guard: direct sends / invite mails never reach gog; anything else always carries --gmail-no-send",
      all(r.returncode == 3 and ("pitg-mail" in r.stderr or "--send-updates=none" in r.stderr) for r in blocked) and calls.count("--gmail-no-send") == 2
      and "--gmail-no-send -a acct send" in calls and "--gmail-no-send calendar events --tomorrow" in calls, (calls, [r.stderr[:40] for r in blocked]))
pp = fake("pathpi", f'echo "$PATH" > {tmp}/path.txt; echo \'{final_ev("ok")}\'')
P.run(P.Spec(pp, "x", "m", "sidp", "hi", tmp, tmp), P.Handle())
check("12l pi tools get /usr/sbin and /sbin (md5 lives in /sbin)", {"/sbin", "/usr/sbin"} <= set(open(tmp + "/path.txt").read().strip().split(":")))

print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
