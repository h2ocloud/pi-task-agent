#!/usr/bin/env python3
"""pirun 防線測試：用假 pi（bash 腳本吐 JSON 事件）驗證——單一工具卡死只殺工具、重複空轉、輪數上限、上下文爆量、牆鐘、取消。"""
import json, os, stat, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pirun
tmp = tempfile.mkdtemp(prefix="pirun-test-")
def ev(**k): return json.dumps(k, ensure_ascii=False)
def call(cmd, tok=100): return ev(type="message_end", message={"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": cmd}}], "stopReason": "toolUse", "usage": {"totalTokens": tok}})
def res(txt): return ev(type="message_end", message={"role": "toolResult", "content": [{"type": "text", "text": txt}]})
def endturn(): return ev(type="turn_end", message={"role": "assistant", "content": [], "stopReason": "toolUse"})
def final(txt, tok=120): return ev(type="turn_end", message={"role": "assistant", "content": [{"type": "text", "text": txt}], "stopReason": "stop", "usage": {"totalTokens": tok}})
def fake(name, body):
    p = f"{tmp}/{name}"; open(p, "w").write("#!/bin/bash\n" + body); os.chmod(p, 0o755); return p
def spec(pi, **k): return pirun.Spec(pi, "x", "m", "sid", "hi", tmp, tmp, **k)
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:200])); (not cond) and fails.append(name)

# 1 正常
pi = fake("ok", f"echo '{call('date')}'; echo '{res('Sun')}'; echo '{endturn()}'; echo '{final('現在是週日')}'")
r = pirun.run(spec(pi), pirun.Handle())
check("1 normal final + trace", r.kind == "final" and r.text == "現在是週日" and r.rounds == 1 and r.trace[0]["result"] == "Sun", r)

# 2 單一工具卡死：只殺那個工具（子行程），pi 本身繼續並完成
pi = fake("stall", f"echo '{call('find ~')}'; echo '{ev(type='tool_execution_start')}'; sleep 600 & wait $!; echo '{ev(type='tool_execution_end')}'; echo '{res('aborted')}'; echo '{endturn()}'; echo '{final('換個方式完成了')}'")
t0 = time.time(); r = pirun.run(spec(pi, stall=3), pirun.Handle())
check("2 stalled tool killed alone, run continues to final", r.kind == "final" and r.stalls == 1 and "換個方式" in r.text and time.time() - t0 < 20, (r.kind, r.stalls, time.time() - t0))

# 3 同一個工具卡死兩次 → stalled
pi = fake("stall2", f"for i in 1 2; do echo '{call('find ~')}'; echo '{ev(type='tool_execution_start')}'; sleep 600 & wait $!; echo '{ev(type='tool_execution_end')}'; done; sleep 60")
r = pirun.run(spec(pi, stall=2), pirun.Handle()); check("3 stalled twice → kind=stalled", r.kind == "stalled" and r.stalls == 2, (r.kind, r.stalls))

# 4 重複同樣呼叫 → loop
pi = fake("loop", f"for i in 1 2 3 4 5; do echo '{call('ls /nope')}'; echo '{res('err')}'; echo '{endturn()}'; sleep 0.2; done; sleep 30")
r = pirun.run(spec(pi, loop_n=3), pirun.Handle()); check("4 repeated call → loop", r.kind == "loop" and r.rounds == 3, (r.kind, r.rounds))

# 5 輪數上限
pi = fake("long", "for i in $(seq 1 20); do echo '" + call("echo $i") .replace("$i", "'\"$i\"'") + "'; echo '" + endturn() + "'; sleep 0.1; done; sleep 30")
# call args 每輪不同以免觸發 loop
pi = fake("long2", "for i in $(seq 1 20); do echo '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"toolCall\",\"name\":\"bash\",\"arguments\":{\"command\":\"echo '$i'\"}}],\"stopReason\":\"toolUse\"}}'; sleep 0.1; done; sleep 30")
r = pirun.run(spec(pi, max_rounds=5), pirun.Handle()); check("5 max_rounds → too_long", r.kind == "too_long", (r.kind, r.rounds))

# 6 上下文爆量
pi = fake("ctx", f"echo '{call('a', 100)}'; sleep 0.1; echo '{call('b', 60000)}'; sleep 30")
r = pirun.run(spec(pi, max_ctx=50000), pirun.Handle()); check("6 ctx over limit → ctx_full with ctx_tokens", r.kind == "ctx_full" and r.ctx_tokens == 60000, (r.kind, r.ctx_tokens))

# 7 牆鐘
pi = fake("wall", "sleep 60"); t0 = time.time(); r = pirun.run(spec(pi, wall=3), pirun.Handle())
check("7 wall clock → timeout", r.kind == "timeout" and time.time() - t0 < 15, (r.kind, time.time() - t0))

# 8 取消
pi = fake("cancel", "sleep 60"); h = pirun.Handle(); threading.Timer(1.0, h.kill).start(); t0 = time.time(); r = pirun.run(spec(pi), h)
check("8 cancel → cancelled quickly", r.kind == "cancelled" and time.time() - t0 < 10, (r.kind, time.time() - t0))

# 9 abort hook（前景第 3 輪轉背景用）
pi = fake("promote", "for i in 1 2 3 4; do echo '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"先看\"},{\"type\":\"toolCall\",\"name\":\"read\",\"arguments\":{\"path\":\"/x'$i'\"}}],\"stopReason\":\"toolUse\"}}'; sleep 0.1; done; sleep 30")
seen = []
r = pirun.run(spec(pi), pirun.Handle(), on_round_start=lambda text, calls, n: (seen.append((text, n)), "abort" if n >= 3 else None)[1])
check("9 on_round_start abort at round 3 with text preserved", r.kind == "aborted" and seen[0] == ("先看", 1) and r.rounds == 3, (r.kind, seen))

# 10 非零結束 → error（含 stderr，且金鑰被遮蔽）
pi = fake("err", "echo '401: Bearer thk_live_SECRETKEY123 bad' >&2; exit 1"); r = pirun.run(spec(pi), pirun.Handle())
check("10 error text scrubbed", r.kind == "error" and "SECRETKEY123" not in r.text and "401" in r.text, r.text)

# 11 只載選中的 skill：--no-skills，每個目錄各一次 --skill
pi = fake("skills", f"printf '%s\\n' \"$@\" > {tmp}/argv; echo '{final('ok')}'")
r = pirun.run(spec(pi, skills=[tmp + "/sk-a", tmp + "/sk-b"]), pirun.Handle())
argv = open(f"{tmp}/argv").read().splitlines()
check("11 --no-skills and one --skill per directory", r.kind == "final" and "--no-skills" in argv and argv.count("--skill") == 2 and (tmp + "/sk-a") in argv and (tmp + "/sk-b") in argv, argv)
print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
