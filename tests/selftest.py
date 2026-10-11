#!/usr/bin/env python3
"""端到端自測：假 Telegram 傳輸＋真的 pi＋真的模型後端。需要後端可用，約 5–10 分鐘。python3 tests/selftest.py"""
import os, shutil, sys, tempfile, threading, time, logging
os.environ.setdefault("PITG_LANG", "zh")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp(prefix="pitg-e2e-")
import pi_tg_bridge as B
B.DATA = tmp; B.STATE_DIR = tmp + "/state"; B.MEDIA_DIR = tmp + "/media"; B.SESSIONS_DIR = tmp + "/sessions"
B.SKILLS_DIR = os.path.expanduser("~/.pi-tg/skills"); B.CHAT_CWD = tmp + "/chat"
os.makedirs(B.STATE_DIR); shutil.copy(os.path.expanduser("~/.pi-tg/AGENTS.md"), tmp + "/AGENTS.md"); shutil.copytree(os.path.expanduser("~/.pi-tg/bin"), tmp + "/bin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

class FakeTG:
    def __init__(s): s.sent = []; s.cv = threading.Condition()
    def send(s, chat_id, text, reply_to=None, essential=True):
        with s.cv: s.sent.append((chat_id, text)); print(f"   [TG→{chat_id}] {text[:110]!r}"); s.cv.notify_all()
    def typing(s, chat_id): pass
    def call(s, *a, **k): return {"result": {}}
    def download(s, *a, **k): return None
    def flush_outbox(s): pass
OWNER, GUEST, GROUP = 111, 222, -999
env = {"OWNER_ID": str(OWNER), "CHAT_ONLY_USERS": str(GUEST), "GROUPS": str(GROUP)}
tg = FakeTG(); br = B.Bridge(tg, env, B.State(B.STATE_DIR + "/settings.json"), B.Jobs(B.STATE_DIR + "/jobs.json")); br.username = "testbot"
n = [0]
def upd(uid, text, chat=None, chat_type="private"):
    n[0] += 1
    br.handle_update({"update_id": n[0], "message": {"message_id": n[0], "from": {"id": uid, "first_name": "T"}, "chat": {"id": chat or uid, "type": chat_type}, "text": text}})
def wait(pred, t=300):
    end = time.time() + t
    with tg.cv:
        while not pred() and time.time() < end: tg.cv.wait(1)
    return pred()
def msgs(chat, since=0): return [t for c, t in tg.sent[since:] if c == chat]
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:300])); (not cond) and fails.append(name)

# A 指令（不經模型）
for cmd, expect in (("/help", "/jobs"), ("/model list", "spark-think"), ("/thinking on", "spark-think"), ("/thinking off", "spark-main"), ("/jobs", "目前沒有背景工作"), ("/new", "已開新對話")):
    c = len(tg.sent); upd(OWNER, cmd); check(f"A {cmd}", expect in tg.sent[-1][1], tg.sent[c:])
c = len(tg.sent); upd(GUEST, "/model main"); check("A non-owner blocked", "只有擁有者" in tg.sent[-1][1])

# B 短問題：直接答，不派工、不多講
c = len(tg.sent); upd(OWNER, "你好，用一句話跟我打招呼"); wait(lambda: len(tg.sent) > c, 120); time.sleep(3)
check("B direct answer = exactly one message (no ack/dispatch)", len(msgs(OWNER, c)) == 1, msgs(OWNER, c))

# C 一輪工具：先回一句（做什麼＋第一步），再給答案
c = len(tg.sent); upd(OWNER, "用 bash 執行 echo pitg-ok，只回答輸出內容"); wait(lambda: len(msgs(OWNER, c)) >= 2, 200); m = msgs(OWNER, c)
check("C tool turn: ack first (what+first step) then the answer", len(m) >= 2 and "先執行" in m[0] + "先" and len(m[0]) > 6 and m[-1].strip() == "pitg-ok", m)

# D 長任務（強制要 ≥3 輪工具）：放背景 → 規劃 → 小步執行 → 做完；進行中仍可聊天、可查進度
shutil.rmtree("/tmp/pitg_e2e", ignore_errors=True)
c = len(tg.sent); t0 = time.time()
upd(OWNER, "請分四次工具呼叫，每次只執行一個 bash 指令（不要合併）：先 mkdir -p /tmp/pitg_e2e，再 date > /tmp/pitg_e2e/d.txt，再 hostname > /tmp/pitg_e2e/h.txt，最後 cat /tmp/pitg_e2e/d.txt /tmp/pitg_e2e/h.txt。然後把結果用一句話回報給我。")
check("D ack first (what+first step)", wait(lambda: len(msgs(OWNER, c)) >= 1, 120) and len(msgs(OWNER, c)[0]) > 6, msgs(OWNER, c))
promoted = wait(lambda: any("多做幾步" in t for t in msgs(OWNER, c)), 240)
check("D promoted to background (told user, chat freed)", promoted, msgs(OWNER, c))
if promoted:
    c2 = len(tg.sent); upd(OWNER, "現在在忙什麼"); wait(lambda: len(tg.sent) > c2, 30)
    check("E status while running (no model call)", any(("進行中" in t or "做完" in t) for t in msgs(OWNER, c2)), msgs(OWNER, c2))
    c3 = len(tg.sent); upd(OWNER, "順便問一下，一週有幾天？只回答數字"); got = wait(lambda: any("7" in t or "七" in t for t in msgs(OWNER, c3)), 240)
    check("E chat answered while job running", got, msgs(OWNER, c3))
    check("D job finishes: 做完 with a natural report", wait(lambda: any(t.startswith("做完：") for t in msgs(OWNER, c)), 900), msgs(OWNER, c)[-3:])
else:
    check("D (fallback) finished in foreground", wait(lambda: any(("d.txt" in t or "h.txt" in t) for t in msgs(OWNER, c)[1:]), 300), msgs(OWNER, c))
d_ok = os.path.exists("/tmp/pitg_e2e/d.txt") and os.path.exists("/tmp/pitg_e2e/h.txt")
check("D deliverables on disk", d_ok, os.listdir("/tmp/pitg_e2e") if os.path.isdir("/tmp/pitg_e2e") else "no dir")
print(f"   (D total {time.time() - t0:.0f}s)")

# F 壓縮上下文但保住大方向
CH = 333; OWN2 = OWNER
env2 = None
c = len(tg.sent); upd(OWNER, "請記住：我的專案代號是紫色獅子，專案資料夾是 /tmp/proj-lion。先不用做任何事，回我一句就好。"); wait(lambda: len(msgs(OWNER, c)) >= 1, 120)
c = len(tg.sent); upd(OWNER, "/compact"); wait(lambda: len(msgs(OWNER, c)) >= 1, 180)
check("F /compact reports summary keeping direction", "大方向" in msgs(OWNER, c)[-1] and "紫色獅子" in msgs(OWNER, c)[-1] and "/tmp/proj-lion" in msgs(OWNER, c)[-1], msgs(OWNER, c))
c = len(tg.sent); upd(OWNER, "我的專案代號和資料夾是什麼？只回答這兩個。"); wait(lambda: len(msgs(OWNER, c)) >= 1, 180)
check("F after compaction still remembers (new session, summary carried)", "紫色獅子" in msgs(OWNER, c)[-1] and "proj-lion" in msgs(OWNER, c)[-1], msgs(OWNER, c))

# G 唯讀聊天 / 群組
c = len(tg.sent); upd(GUEST, "你好"); wait(lambda: len(msgs(GUEST, c)) >= 1, 120); check("G chat-only reply", len(msgs(GUEST, c)) == 1, msgs(GUEST, c))
c = len(tg.sent); upd(OWNER, "大家好", chat=GROUP, chat_type="supergroup"); time.sleep(1.5); check("G group w/o mention ignored", len(tg.sent) == c)
print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
