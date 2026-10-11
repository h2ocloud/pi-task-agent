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
    br.backend_ok = lambda: True; return br, tg
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

# 4 前景超過兩輪 → 轉背景（聊天空出來、session 換新、orchestrator 接手拿到前情 trace）
def tool_ev(i): return ev(type="message_end", message={"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": f"echo {i}"}}], "stopReason": "toolUse"})
pi = fake("many", "for i in 1 2 3 4 5; do echo '" + "'\"$(echo '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"toolCall\",\"name\":\"bash\",\"arguments\":{\"command\":\"echo '$i'\"}}],\"stopReason\":\"toolUse\"}}')\"'" + "'; sleep 0.2; done; sleep 30")
br, tg = mk(pi); started = {}
class FakeOrch:
    def __init__(s): s.calls = []
    def start(s, job, trace=None, resumed=False): s.calls.append((job, trace))
    def describe(s, j): return "規劃中"
    def get(s, i): return None
br.orch = FakeOrch(); sid0 = br.state.sid(1)
br.llm_text = lambda *a, **k: '{"say":"我先把九月的發票收成一張表。","work":true,"know":false}'
br.foreground(1, 1, "owner", "把九月發票整理成表", [], 11)
check("4 promoted: told user, session rotated, orchestrator got job+trace", any("多做幾步" in t for _, t in tg.sent) and br.state.sid(1) != sid0 and len(br.orch.calls) == 1 and len(br.orch.calls[0][1]) >= 2, (tg.sent, br.orch.calls))
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

# 5 路由：狀態（不經模型）/ 補充 / 無關 / 分不出來→問 / 停止
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
llm_answer["v"] = {"route": "unclear"}
check("5 unclear → asks once with options, holds the message", br.route(1, "改成十月", [], 8) and "加」或「新" in tg.sent[-1][1] and 1 in br.pending, tg.sent[-1])
check("5 answer 加 → applied to job", br.route(1, "加", [], 9) and "改成十月" in br.orch.sup, br.orch.sup)
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
print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
