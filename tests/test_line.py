#!/usr/bin/env python3
"""LINE 通道測試（假 LINE API，不碰網路）：驗簽、webhook 伺服器、reply token 免費路徑、推播額度節制、
非必要訊息在 LINE 不推、輸入中動畫節流、身份對應與權限、圖片/語音/貼圖處理、markdown 轉純文字。"""
import base64, hashlib, hmac, json, os, sys, tempfile, threading, time, urllib.request, urllib.error
os.environ["PITG_LANG"] = "zh"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp(prefix="pitg-line-test-")
import line as L, pi_tg_bridge as B
B.STATE_DIR = tmp; B.MEDIA_DIR = tmp + "/media"; B.SESSIONS_DIR = tmp + "/s"; B.CHAT_CWD = tmp + "/c"; B.DATA = tmp; B.SKILLS_DIR = tmp
SECRET = "s3cr3t"
class FakeClient(L.LineClient):
    def __init__(s, limit=200, used=0):
        super().__init__("tok", SECRET); s.calls = []; s._q = (limit, used); s.fail_push = False
    def quota(s): return s._q
    def reply(s, t, texts): s.calls.append(("reply", t, texts)); return True
    def push(s, to, texts):
        if s.fail_push: return False
        s.calls.append(("push", to, texts)); return True
    def loading(s, u, sec=30): s.calls.append(("loading", u))
    def content(s, mid): return b"\x89PNG-fake-" + mid.encode()
def sign(body): return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:250])); (not cond) and fails.append(name)

# 1 驗簽
c = FakeClient(); body = b'{"events":[]}'
check("1 signature ok / bad / empty", c.verify(body, sign(body)) and not c.verify(body, sign(b"x")) and not c.verify(body, ""))

# 2 webhook 伺服器：好簽章→200 並派送事件；壞簽章→401 不派送；GET→200；錯路徑→404
got = []; srv = L.WebhookServer(0, c, lambda ev: got.append(ev)); srv.start()
def post(path, body, sig):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=body, headers={"X-Line-Signature": sig, "Content-Type": "application/json"})
    try: return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e: return e.code
ev_body = json.dumps({"events": [{"type": "message", "message": {"type": "text", "text": "hi"}}, {"type": "follow"}]}).encode()
check("2 good signature → 200, events dispatched", post("/line/webhook", ev_body, sign(ev_body)) == 200 and (time.sleep(0.3) or len(got) == 2), got)
n = len(got); check("2 bad signature → 401, nothing dispatched", post("/line/webhook", ev_body, "bad") == 401 and (time.sleep(0.2) or len(got) == n))
check("2 LINE console 'Verify' (empty events) → 200", post("/line/webhook", b'{"events":[]}', sign(b'{"events":[]}')) == 200)
check("2 wrong path 404 / GET health 200", post("/x", ev_body, sign(ev_body)) == 404 and urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5).status == 200)

# 3 reply token 免費路徑 + 推播策略
c = FakeClient(); ch = L.LineChannel(c, tmp, tmp + "/m"); chat = "line-Uabc"
ch.note_event(chat, "RT1")
ch.send(chat, "先回一句", essential=False)
check("3 first message uses free reply token", c.calls[-1][0] == "reply" and c.calls[-1][1] == "RT1", c.calls)
n = len(c.calls); ch.send(chat, "拆成 3 步…", essential=False)
check("3 non-essential w/o token is dropped (no push cost)", len(c.calls) == n, c.calls[n:])
ch.send(chat, "做完：結果在 /tmp/x", essential=True)
check("3 essential w/o token → push (counted)", c.calls[-1][0] == "push" and c.calls[-1][1] == "Uabc" and ch.used == 1, (c.calls[-1], ch.used))
ch.note_event(chat, "RT2"); ch.tokens[chat][1] -= 60        # token 過期
ch.send(chat, "晚到的結果", essential=True)
check("3 expired reply token → push instead", c.calls[-1][0] == "push", c.calls[-1])

# 4 額度見底：不推，改通知（結果不丟）
notes = []; c = FakeClient(limit=200, used=197); ch = L.LineChannel(c, tmp, tmp + "/m", notify=lambda t: notes.append(t))
ch.send(chat, "重要結果 ABC", essential=True)
check("4 quota nearly exhausted → no push, owner notified with the content", not any(x[0] == "push" for x in c.calls) and notes and "重要結果 ABC" in notes[0], (c.calls, notes))

# 5 推播失敗 → 存 outbox → 之後補送
c = FakeClient(); ch = L.LineChannel(c, tmp, tmp + "/m"); c.fail_push = True
ch.send(chat, "會失敗的結果", essential=True)
check("5 failed push parked", os.path.exists(ch.outbox) and "會失敗的結果" in open(ch.outbox).read())
c.fail_push = False; ch.flush_outbox()
check("5 flushed after recovery", c.calls[-1][0] == "push" and c.calls[-1][2] == ["會失敗的結果"] and not os.path.exists(ch.outbox), c.calls[-1])

# 6 輸入中動畫節流（每 25 秒一次）
c = FakeClient(); ch = L.LineChannel(c, tmp, tmp + "/m")
for _ in range(5): ch.typing(chat)
check("6 loading animation throttled", sum(1 for x in c.calls if x[0] == "loading") == 1, c.calls)

# 7 markdown → 純文字、超長切段
check("7 plain(): bold/fence/heading/backtick removed", L.plain("## 標題\n**重點** `cmd`\n```bash\nls\n```") == "標題\n重點 cmd\nls", repr(L.plain("## 標題\n**重點** `cmd`\n```bash\nls\n```")))
parts = L.split_text("甲" * 12000); check("7 long text split under 5000 per bubble", len(parts) == 3 and all(len(p) <= 4900 for p in parts), [len(p) for p in parts])

# 8 Bridge：身份對應與權限
class TG:
    def __init__(s): s.sent = []; s.line = None
    def send(s, c, t, r=None, essential=True): s.sent.append((c, t, essential))
    def typing(s, c): pass
    def flush_outbox(s): pass
tg = TG(); c = FakeClient(); tg.line = L.LineChannel(c, tmp, tmp + "/m")
br = B.Bridge(tg, {"OWNER_ID": "1", "LINE_OWNER_ID": "Uowner", "LINE_CHAT_ONLY_USERS": "Uwife"}, B.State(tmp + "/st.json"), B.Jobs(tmp + "/jobs.json"))
br.backend_ok = lambda: True; seen = []
br.process_message = lambda chat_id, uid, mode, text, files, msg_id: seen.append((chat_id, mode, text, files))
def evt(uid, mtype="text", **m): return {"type": "message", "replyToken": "RT" + uid, "source": {"type": "user", "userId": uid}, "message": {"type": mtype, **m}}
br.handle_line_event(evt("Uowner", text="你好")); time.sleep(0.2)
check("8 owner text → owner mode, namespaced chat id", seen[-1][:3] == ("line-Uowner", "owner", "你好"), seen)
br.handle_line_event(evt("Uwife", text="嗨")); time.sleep(0.2)
check("8 allow-listed other user → chat-only mode", seen[-1][:2] == ("line-Uwife", "chat"), seen)
n = len(seen); br.handle_line_event(evt("Ustranger", text="x")); time.sleep(0.2)
check("8 stranger ignored", len(seen) == n)
br.handle_line_event({"type": "message", "source": {"type": "group", "groupId": "G1", "userId": "Uowner"}, "message": {"type": "text", "text": "x"}}); time.sleep(0.2)
check("8 group messages ignored", len(seen) == n)
br.handle_line_event({"type": "follow", "source": {"type": "user", "userId": "Uowner"}}); time.sleep(0.2); check("8 non-message events ignored", len(seen) == n)
br.handle_line_event(evt("Uowner", "image", id="IMG1")); time.sleep(0.3)
check("8 image downloaded and passed as file", seen[-1][3] and os.path.exists(seen[-1][3][0]) and open(seen[-1][3][0], "rb").read().endswith(b"IMG1"), seen[-1])
B.transcribe = lambda path, url: "這是語音轉出來的文字"
br.handle_line_event(evt("Uowner", "audio", id="AUD1")); time.sleep(0.3)
check("8 audio → transcribed text", seen[-1][2] == "這是語音轉出來的文字", seen[-1])
br.handle_line_event(evt("Uowner", "sticker", packageId="1", stickerId="1")); time.sleep(0.2)
check("8 sticker → polite note", "文字、圖片和語音" in tg.sent[-1][1], tg.sent[-1])
br.handle_line_event(evt("Uowner", text="/help")); time.sleep(0.2)
check("8 slash command works over LINE (not sent to model)", "/jobs" in tg.sent[-1][1] and seen[-1][2] != "/help", tg.sent[-1][1][:40])
br.handle_line_event(evt("Uwife", text="/model main")); time.sleep(0.2)
check("8 non-owner blocked from owner-only command on LINE", "只有擁有者" in tg.sent[-1][1], tg.sent[-1])

# 9 Router：依聊天 id 分流
sent_tg, sent_line = [], []
class T2:
    def send(s, c, t, r=None): sent_tg.append((c, t))
    def typing(s, c): pass
    def flush_outbox(s): pass
class L2:
    owns = staticmethod(L.LineChannel.owns)
    def send(s, c, t, r=None, essential=True): sent_line.append((c, t, essential))
    def typing(s, c): pass
    def flush_outbox(s): pass
rt = L.Router(T2(), L2()); rt.send(123, "tg"); rt.send("line-U1", "line", essential=False)
check("9 router: int→Telegram, 'line-…'→LINE (flag carried)", sent_tg == [(123, "tg")] and sent_line == [("line-U1", "line", False)], (sent_tg, sent_line))
srv.httpd.shutdown()
print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
