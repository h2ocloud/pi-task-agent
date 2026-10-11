#!/usr/bin/env python3
"""按需載入：always 常駐、中文二字對上、上限只算關鍵字、all / off。不碰網路。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import skills

tmp = tempfile.mkdtemp(prefix="skills-test-")
def write(name, body):
    d = f"{tmp}/{name}"; os.makedirs(d); open(f"{d}/SKILL.md", "w").write(body)
write("weather", "---\nname: weather\ndescription: 查天氣、預報、weather\nalways: false\n---\n# weather\n")
write("always-note", "---\nname: always-note\ndescription: 每次都帶\nalways: true\n---\n")
write("mail", "---\nname: mail\ndescription: 寄信 send email\n---\n")
write("cal", "---\nname: cal\ndescription: 行事曆 calendar booking\n---\n")
write("files", "---\nname: files\ndescription: 讀寫檔案\ntriggers: 檔案\n---\n")
os.makedirs(f"{tmp}/empty-dir")
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)[:200])); (not cond) and fails.append(name)
def base(paths):
    return [os.path.basename(p) for p in paths]

os.environ["PITG_SKILLS"] = "ondemand"
os.environ["PITG_SKILL_LIMIT"] = "3"
got = base(skills.skills_for("今天天氣如何", tmp))
check("1 查天氣對上描述裡的天氣，always 在最前", got[:1] == ["always-note"] and "weather" in got and "mail" not in got, got)
got = base(skills.skills_for("hello there", tmp))
check("2 沒有關鍵字就只留常駐", got == ["always-note"], got)
os.environ["PITG_SKILL_LIMIT"] = "2"
got = base(skills.skills_for("weather email calendar 讀寫檔案", tmp))
check("3 上限只限制按需的，常駐不拿掉", got[0] == "always-note" and len(got) == 3, got)
os.environ["PITG_SKILLS"] = "all"
check("4 all 載入每一個有 SKILL.md 的目錄", len(skills.skills_for("x", tmp)) == 5, base(skills.skills_for("x", tmp)))
os.environ["PITG_SKILLS"] = "off"
check("5 off 一個都不載", skills.skills_for("今天天氣如何", tmp) == [])
os.environ["PITG_SKILLS"] = "ondemand"
os.environ["PITG_SKILL_LIMIT"] = "3"
check("6 triggers 對得上", "files" in base(skills.skills_for("請看這個檔案", tmp)), base(skills.skills_for("請看這個檔案", tmp)))
print("\nRESULT:", "ALL PASS" if not fails else f"FAILED {fails}"); sys.exit(1 if fails else 0)
