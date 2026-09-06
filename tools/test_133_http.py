# -*- coding: utf-8 -*-
"""v1.3.3 接口自测：设置读写（两个新开关）+ 日志接口返回结构与内容标记。"""
import json
import os
import sys
import time
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

ROOT = os.path.join(tempfile.gettempdir(), "fnusb133http_%d" % int(time.time()))
ETC = os.path.join(ROOT, "etc")
VAR = os.path.join(ROOT, "var")
os.makedirs(ETC, exist_ok=True)
os.environ["TRIM_PKGETC"] = ETC
os.environ["TRIM_PKGVAR"] = VAR

import state as state_mod
import server
import syncengine

FAILS = []


def ck(cond, name):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


st = state_mod.AppState()
httpd = server.make_server(st, 0)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

BASE = "http://127.0.0.1:%d" % port


def req(path, method="GET", body=None):
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


print("1) 设置接口：两个新开关可写可读")
r = req("/api/settings", "POST", {
    "poll_interval": 5,
    "log_retention_days": 30,
    "log_file_detail": True,
    "push_enabled": True,
    "notify_on_start": True,
    "notify_on_finish": False,
    "push_channels": "pushplus",
    "pushplus_token": "tk",
})
ck(r["ok"], "POST /api/settings 成功")
ck(st.settings["notify_on_start"] is True, "notify_on_start 已持久化 True")
ck(st.settings["notify_on_finish"] is False, "notify_on_finish 已持久化 False")
with open(os.path.join(ETC, "settings.json"), encoding="utf-8") as f:
    disk = json.load(f)
ck(disk.get("notify_on_start") is True, "settings.json 落盘含 notify_on_start")
ck(disk.get("notify_on_finish") is False, "settings.json 落盘含 notify_on_finish")
r2 = req("/api/settings")
ck(r2["settings"].get("notify_on_start") is True, "GET /api/settings 回读 notify_on_start")
ck(r2["settings"].get("notify_on_finish") is False, "GET /api/settings 回读 notify_on_finish")

print()
print("2) 日志接口：▶/■/·/明细 标记与顺序")
import shutil
base = os.path.join(ROOT, "fs")
mp = os.path.join(base, "usb", "good")
os.makedirs(mp, exist_ok=True)
with open(os.path.join(mp, "x.txt"), "w", encoding="utf-8") as f:
    f.write("x" * 100)
dst = os.path.join(base, "nas")
task = {"id": "tA", "name": "接口测试", "mode": "incremental", "conflict": "overwrite",
        "folders": [{"usb": "good", "nas": dst}]}
syncengine.run_sync(st, task, {"mountpoint": os.path.join(base, "usb"), "label": "DISK"})

r = req("/api/logs")
logs = r["logs"]
msgs = [l["msg"] for l in logs]
ck(any(m.startswith("▶") for m in msgs), "/api/logs 含 ▶ 开始行")
ck(any(m.startswith("■") for m in msgs), "/api/logs 含 ■ 结束行")
ck(any(m.startswith("·") for m in msgs), "/api/logs 含 · 过程行")
ck(any(m.startswith("  ") for m in msgs), "/api/logs 含缩进明细行")
ck(all("ts" in l and "level" in l for l in logs), "每条日志含 ts/level 字段")

r = req("/api/logs/full?limit=500")
ck(r["ok"], "/api/logs/full 成功")
fmsgs = [l["msg"] for l in r["logs"]]
ck(any(m.startswith("▶") for m in fmsgs), "完整日志含 ▶ 开始行（文件落盘正确）")
ck(any(m.startswith("■") for m in fmsgs), "完整日志含 ■ 结束行（文件落盘正确）")
# 日志文件的行格式仍为 4 段（ts/level/task_id/msg），未被破坏
p = os.path.join(VAR, "logs", "usbcopy.log")
bad_lines = []
with open(p, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        if len(line.split("\t")) != 4:
            bad_lines.append(line)
ck(not bad_lines, "日志文件每行仍为 4 段制表符分隔（异常 %s）" % bad_lines[:2])

print()
print("3) 日志读取窗口：默认 2000 条，支持 ?limit=")
for i in range(60):
    st.log("info", "填充 %d" % i, "tFill")
r = req("/api/logs?limit=50")
ck(r["ok"] and len(r["logs"]) == 50, "/api/logs?limit=50 返回 50 条（实际 %s）" % len(r["logs"]))
r = req("/api/logs")
ck(r["ok"] and len(r["logs"]) == min(200, len(st.log_buffer)),
   "/api/logs 默认返回最近 200 条（实际 %s / 缓冲 %s）" % (len(r["logs"]), len(st.log_buffer)))
r = req("/api/logs?limit=99999")
ck(r["ok"] and len(r["logs"]) == len(st.log_buffer),
   "/api/logs?limit 超过上限时收敛到环形缓冲容量（实际 %s）" % len(r["logs"]))
r = req("/api/tasks/tFill/logs?limit=10")
ck(r["ok"] and len(r["logs"]) == 10, "/api/tasks/<id>/logs?limit=10 生效（实际 %s）" % len(r["logs"]))
r = req("/api/logs/full?limit=20000")
ck(r["ok"], "/api/logs/full?limit=20000 可用")

print()
print("4) 前端约定自检（app.js 中的 hideTs 规则）")
js = open(os.path.join(APP, "www", "app.js"), encoding="utf-8").read()
ck('function hideTs(' in js, "app.js 定义 hideTs")
ck('c === "·" || c === " "' in js, "hideTs 识别 · 与空格开头")
ck("set-notify-start" in js and "set-notify-finish" in js, "app.js 绑定两个新开关")
html = open(os.path.join(APP, "www", "index.html"), encoding="utf-8").read()
ck('id="set-notify-start"' in html, "index.html 含 同步开始时推送 开关")
ck('id="set-notify-finish"' in html, "index.html 含 同步完成时推送 开关")
css = open(os.path.join(APP, "www", "style.css"), encoding="utf-8").read()
ck(".log-line.err-title" in css, "style.css 含 err-title 高亮")
ck(".ts.blank" in css, "style.css 含空时间列占位")

httpd.shutdown()
print()
print("=" * 60)
if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for x in FAILS:
        print("  - " + x)
    sys.exit(1)
print("全部通过")
