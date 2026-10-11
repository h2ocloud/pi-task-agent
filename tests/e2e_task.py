#!/usr/bin/env python3
"""用一則真實任務跑完整流程（假 Telegram＋真 pi＋真模型），印出使用者在 Telegram 會看到的每一則訊息與時間。
python3 tests/e2e_task.py "任務文字" [最長秒數]"""
import os, shutil, sys, tempfile, threading, time, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp(prefix="pitg-task-")
import pi_tg_bridge as B
B.DATA = tmp; B.STATE_DIR = tmp + "/state"; B.MEDIA_DIR = tmp + "/media"; B.SESSIONS_DIR = tmp + "/sessions"
B.SKILLS_DIR = os.path.expanduser("~/.pi-tg/skills"); B.CHAT_CWD = tmp + "/chat"
os.makedirs(B.STATE_DIR); shutil.copy(os.path.expanduser("~/.pi-tg/AGENTS.md"), tmp + "/AGENTS.md"); shutil.copytree(os.path.expanduser("~/.pi-tg/bin"), tmp + "/bin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
T0 = time.time()
class TG:
    def __init__(s): s.sent = []; s.cv = threading.Condition()
    def send(s, c, t, r=None, essential=True):
        with s.cv: s.sent.append(t); print(f"\n[+{time.time()-T0:4.0f}s TG] {t}\n", flush=True); s.cv.notify_all()
    def typing(s, c): pass
    def call(s, *a, **k): return {"result": {}}
    def download(s, *a, **k): return None
    def flush_outbox(s): pass
tg = TG(); br = B.Bridge(tg, {"OWNER_ID": "1"}, B.State(B.STATE_DIR + "/s.json"), B.Jobs(B.STATE_DIR + "/j.json")); br.username = "t"
br.handle_update({"update_id": 1, "message": {"message_id": 1, "from": {"id": 1, "first_name": "H"}, "chat": {"id": 1, "type": "private"}, "text": sys.argv[1]}})
cap = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
end = time.time() + cap
while time.time() < end:
    time.sleep(3)
    jobs = br.jobs.items
    if jobs and all(j["status"] not in B.ACTIVE for j in jobs) and any(t.startswith(("做完", "『")) or "被擋住" in t or "需要你" in t for t in tg.sent): break
    if not jobs and tg.sent and not br.fg: break
for j in br.jobs.items:
    print("JOB", j["name"], j["status"], br.orch.describe(j), "| note:", j["note"])
    def walk(steps, d=0):
        for s in steps:
            print("  " * d + f" - [{s['status']}] {s['id']} {s['title']} → {(s['result'] or s['note'])[:90]}")
            walk(s["sub"], d + 1)
    walk(j["plan"])
print(f"TOTAL {time.time()-T0:.0f}s, messages to user: {len(tg.sent)}")
