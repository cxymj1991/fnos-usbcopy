# -*- coding: utf-8 -*-
"""v1.3.3 功能自测：开始/完成推送开关、起止时间戳+耗时、出错路径清单置顶。"""
import io
import os
import sys
import time
import shutil
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import state as state_mod
import syncengine
import notify

ROOT = os.path.join(tempfile.gettempdir(), "fnusb133_%d" % int(time.time()))
os.makedirs(ROOT, exist_ok=True)
FAILS = []


def ck(cond, name):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeState:
    """最小 AppState 替身：只保留 syncengine 用到的接口。"""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.logs = []
        self.lock = threading.RLock()
        self.tasks = []
        self.running_syncs = {}

    def log_many(self, entries):
        for item in entries:
            self.log(item[0], item[1], item[2] if len(item) > 2 else None)

    def log(self, level, msg, task_id=None):
        self.logs.append({"ts": int(time.time()), "level": level,
                          "task_id": task_id, "msg": str(msg)})

    def save_tasks(self):
        pass

    def new_task_id(self):
        return "t1"


def msgs(st):
    return [l["msg"] for l in st.logs]


def build_usb(base):
    """构造 U 盘目录：good/ 正常，bad/ 含一个不可读文件（模拟复制失败）。"""
    mp = os.path.join(base, "usb")
    os.makedirs(os.path.join(mp, "good"), exist_ok=True)
    os.makedirs(os.path.join(mp, "bad"), exist_ok=True)
    with open(os.path.join(mp, "good", "a.txt"), "w", encoding="utf-8") as f:
        f.write("hello")
    with open(os.path.join(mp, "good", "b.txt"), "w", encoding="utf-8") as f:
        f.write("world")
    with open(os.path.join(mp, "bad", "c.txt"), "w", encoding="utf-8") as f:
        f.write("bad")
    return mp


print("=" * 72)
print("1) 推送开关：开始/完成独立控制")
print("=" * 72)

calls = []
bodies = []
orig_send = notify.send_push


def fake_send(settings, title, content):
    calls.append(title)
    bodies.append(content)
    return [("fake", "ok")]

def reset():
    calls[:] = []
    bodies[:] = []


notify.send_push = fake_send
try:
    for flag_start, flag_finish, want in (
            (True, True, ["USB Copy 已开始同步", "USB Copy 同步完成"]),
            (False, True, ["USB Copy 同步完成"]),
            (True, False, ["USB Copy 已开始同步"]),
            (False, False, []),
    ):
        base = tempfile.mkdtemp(dir=ROOT)
        mp = build_usb(base)
        dst = os.path.join(base, "nas")
        reset()
        st = FakeState({"notify_on_start": flag_start, "notify_on_finish": flag_finish})
        task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
                "folders": [{"usb": "good", "nas": dst}]}
        syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
        ck(calls == want, "开关 start=%s finish=%s → 推送 %s" % (flag_start, flag_finish, calls))
finally:
    notify.send_push = orig_send

print()
print("=" * 72)
print("2) 日志时间戳：只在开始/结束，含耗时；明细不带时间")
print("=" * 72)

base = tempfile.mkdtemp(dir=ROOT)
mp = build_usb(base)
dst = os.path.join(base, "nas")
st = FakeState({"log_file_detail": True})
task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
        "folders": [{"usb": "good", "nas": dst}]}
syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
m = msgs(st)
for x in m:
    print("   | " + x)

starts = [x for x in m if x.startswith("▶")]
ends = [x for x in m if x.startswith("■")]
ck(len(starts) == 1, "恰好 1 条 ▶ 开始行")
ck(len(ends) == 1, "恰好 1 条 ■ 结束行")
import re
ck(bool(re.search(r"▶ 开始同步 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", starts[0])), "开始行含完整日期时间")
ck(bool(re.search(r"■ 同步完成 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}（耗时 .+?）", ends[0])), "结束行含日期时间与耗时")
ck("MYDISK" in starts[0] and "T" in starts[0], "开始行含任务名与U盘卷标")
ck("新增 2" in ends[0], "结束行统计正确（新增 2）")

# 明细/过程行必须以 · 或两个空格开头（前端据此隐藏时间列）
detail = [x for x in m if x.startswith("  ")]
sub = [x for x in m if x.startswith("·")]
ck(len(sub) >= 2, "配对过程行以 · 开头（%d 条）" % len(sub))
ck(len(detail) >= 3, "变更明细行以两空格开头（%d 条）" % len(detail))
ck(any("复制:" in x for x in detail), "明细中含复制记录")
ck(any(x.startswith("  —— 变更明细（共 2 条）") for x in detail), "变更明细标题行正确")
bad = [x for x in m if not (x[0] in ("▶", "■", "·", " ") or x.startswith("手机推送"))]
ck(not bad, "同步过程日志全部符合首字符约定（异常：%s）" % bad[:3])

print()
print("=" * 72)
print("3) 出错路径清单：错误时集中列出路径与原因，且位于该次同步最上方")
print("=" * 72)

base = tempfile.mkdtemp(dir=ROOT)
mp = build_usb(base)
dst = os.path.join(base, "nas")

orig_copy = shutil.copy2


def copy_that_fails(src, dst2, *a, **kw):
    if os.sep + "bad" + os.sep in src or "/bad/" in src:
        raise PermissionError("Permission denied: '%s'" % src)
    return orig_copy(src, dst2, *a, **kw)


shutil.copy2 = copy_that_fails
try:
    st = FakeState({"log_file_detail": True})
    task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
            "folders": [{"usb": "good", "nas": dst},
                        {"usb": "bad", "nas": dst + "_bad"},
                        {"usb": "notexist", "nas": dst + "_none"}]}
    syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
    m = msgs(st)
    for x in m:
        print("   | " + x)

    idx_title = [i for i, x in enumerate(m) if x.startswith("❗❗")]
    ck(len(idx_title) == 2, "出错清单有标题行与结束行（%d 条）" % len(idx_title))
    first, last = idx_title[0], idx_title[-1]
    paths = [x for x in m if x.startswith("  ✖")]
    ck(len(paths) == 2, "列出 2 条出错路径（实际 %d）" % len(paths))
    ck(any("c.txt" in p and "Permission denied" in p for p in paths), "含真实出错文件路径与原因")
    ck(any("notexist" in p and "源文件夹不存在" in p for p in paths), "含不存在的源文件夹路径与原因")
    # 结束行(■) 应在清单之前；清单之后不应再有非清单内容（这样界面倒序展示时清单位于最上方）
    i_end = [i for i, x in enumerate(m) if x.startswith("■")][0]
    ck(i_end < first, "清单写在结束行之后（界面最新在前 → 显示在最上方）")
    after = [x for x in m[last + 1:] if not x.startswith("手机推送")]
    ck(not after, "清单之后无其它日志，确保置顶（剩余：%s）" % after[:3])
    ck(any("■ 同步结束（有错误）" in x for x in m), "结束行标注有错误")
finally:
    shutil.copy2 = orig_copy

print()
print("=" * 72)
print("4) 异常中断：同样输出出错清单，且「完成推送」受开关控制")
print("=" * 72)

base = tempfile.mkdtemp(dir=ROOT)
mp = build_usb(base)
dst = os.path.join(base, "nas")
calls = []
notify.send_push = fake_send
orig_walk = os.walk


def walk_boom(*a, **kw):
    raise OSError("device gone")


os.walk = walk_boom
try:
    st = FakeState({"notify_on_start": True, "notify_on_finish": False})
    task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
            "folders": [{"usb": "good", "nas": dst}]}
    syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
    m = msgs(st)
    for x in m:
        print("   | " + x)
    ck(any("同步异常中断——" in x for x in m), "异常时输出中断标题")
    ck(any("device gone" in x for x in m), "异常原因写入清单")
    ck(calls == ["USB Copy 已开始同步"], "notify_on_finish=False 时失败不推送（实际 %s）" % calls)
    last_title = max(i for i, x in enumerate(m) if x.startswith("❗❗"))
    tail = [x for x in m[last_title + 1:] if not x.startswith("手机推送")]
    ck(not tail, "异常时清单同样位于最末尾（界面最上方）（剩余：%s）" % tail[:3])
finally:
    os.walk = orig_walk
    notify.send_push = orig_send

print()
print("=" * 72)
print("5) 完成/失败推送正文：逐条列出出错文件路径与原因")
print("=" * 72)

import json as _json
import urllib.request as _urllib

base = tempfile.mkdtemp(dir=ROOT)
mp = build_usb(base)
dst = os.path.join(base, "nas")

shutil.copy2 = copy_that_fails
notify.send_push = fake_send
try:
    st = FakeState({"notify_on_finish": True})
    task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
            "folders": [{"usb": "good", "nas": dst},
                        {"usb": "bad", "nas": dst + "_bad"},
                        {"usb": "notexist", "nas": dst + "_none"}]}
    reset()
    syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
    ck(len(bodies) == 1, "仅推送 1 条完成通知（实际 %d）" % len(bodies))
    body = bodies[0]
    print("--- 推送正文 ---")
    for ln in body.split("\n"):
        print("   | " + ln)
    ck(calls == ["USB Copy 同步失败（存在错误）"], "标题为「同步失败（存在错误）」")
    ck(body.startswith("[T]"), "正文以任务名开头")
    ck("耗时" in body, "正文含耗时")
    ck("出错文件（共 2 个）：" in body, "正文含出错总数")
    ck("1. " in body and "2. " in body, "出错文件按序号列出")
    ck("c.txt" in body and "Permission denied" in body, "正文含真实出错文件路径与原因")
    ck("notexist" in body and "源文件夹不存在" in body, "正文含不存在路径与原因")
finally:
    shutil.copy2 = orig_copy

# 成功时不应出现出错清单
base = tempfile.mkdtemp(dir=ROOT)
mp = build_usb(base)
dst = os.path.join(base, "nas")
try:
    st = FakeState({"notify_on_finish": True})
    task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
            "folders": [{"usb": "good", "nas": dst}]}
    reset()
    syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
    ck(calls == ["USB Copy 同步完成"], "成功时标题为「同步完成」")
    ck("出错文件" not in bodies[0], "成功时正文不含出错清单")
    ck("新增 2" in bodies[0], "成功时正文含统计")
finally:
    notify.send_push = orig_send

# 异常中断：失败推送正文同样带出错原因
notify.send_push = fake_send
os.walk = walk_boom
try:
    st = FakeState({"notify_on_finish": True})
    task = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
            "folders": [{"usb": "good", "nas": os.path.join(base, "nas2")}]}
    reset()
    syncengine.run_sync(st, task, {"mountpoint": mp, "label": "MYDISK"})
    ck(calls == ["USB Copy 同步失败"], "异常时标题为「同步失败」")
    ck("device gone" in bodies[0], "异常时正文含出错原因（device gone）")
finally:
    os.walk = orig_walk
    notify.send_push = orig_send

# 条数上限与超长路径截断
eps = [("/mnt/usb/%d.txt" % i, "复制失败：boom") for i in range(25)]
txt = syncengine._format_error_paths(eps)
lines = txt.split("\n")
ck("出错文件（共 25 个）：" in txt, "清单含总数 25")
ck(len(lines) == 23, "默认最多列 20 条（实际行数 %d）" % len(lines))
ck("其余 5 个未列出" in txt, "超出部分提示查看运行日志")
ck(syncengine._format_error_paths([]) == "", "无错误时返回空串")
long_txt = syncengine._format_error_paths([("/x" * 400, "err")])
ck(len(long_txt) < 300 and "…" in long_txt, "超长路径截断并保留尾部（长度 %d）" % len(long_txt))

# PushPlus：html 模板需把换行转成 <br> 并转义 HTML
captured = {}


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    captured["payload"] = _json.loads(req.data.decode("utf-8"))
    return _FakeResp(b'{"code":200,"msg":"success"}')


orig_urlopen = _urllib.urlopen
_urllib.urlopen = _fake_urlopen
try:
    r = notify._send_pushplus({"pushplus_token": "tk"}, "标题", "第一行\n第二行 & <b>")
    ck(r == "ok", "PushPlus 发送返回 ok")
    got = captured.get("payload", {}).get("content")
    ck(got == "第一行<br>第二行 &amp; &lt;b&gt;",
       "PushPlus 换行转 <br> 且转义 HTML（实际：%r）" % got)
finally:
    _urllib.urlopen = orig_urlopen

print()
print("=" * 72)
print("6) 日志中的出错清单：完整列出，不再截断到 100 条")
print("=" * 72)

# 6.1 直接构造 250 条出错路径，验证全部写入日志
eps = [("/mnt/usb/dir%03d/file%03d.txt" % (i // 10, i), "复制失败：Permission denied")
       for i in range(250)]
st = FakeState({})
syncengine._log_error_paths(st, "t1", eps)
paths = [x for x in msgs(st) if x.startswith("  ✖")]
ck(len(paths) == 250, "250 条出错路径全部写入日志（实际 %d）" % len(paths))
ck("共 250 个路径出错" in msgs(st)[0], "标题含总数 250")
ck(any("dir000/file000.txt" in x for x in paths), "含第 1 条")
ck(any("dir024/file249.txt" in x for x in paths), "含最后 1 条")
ck(not any("未列出" in x for x in msgs(st)), "无「其余未列出」截断提示")
ck(msgs(st)[-1] == "❗❗ 出错路径清单结束（共 250 条）", "结束行总数正确")
# 顺序与输入一致
idx = [i for i, x in enumerate(msgs(st)) if x.startswith("  ✖")]
ck(all("file%03d.txt" % k in msgs(st)[idx[k]] for k in (0, 99, 100, 249)),
   "清单顺序与收集顺序一致（抽查 0/99/100/249）")

# 6.2 log_many 批量写：格式与 log() 一致，且落盘仍是 4 段制表符
tmpetc2 = os.path.join(ROOT, "etc2")
tmpvar2 = os.path.join(ROOT, "var2")
os.makedirs(tmpetc2, exist_ok=True)
os.environ["TRIM_PKGETC"] = tmpetc2
os.environ["TRIM_PKGVAR"] = tmpvar2
st_real = state_mod.AppState()
st_real.log("info", "前一行", "t1")
st_real.log_many([("error", "  批量1", "t1"), ("error", "  批量2", "t1")])
st_real.log("info", "后一行", "t1")
ck([l["msg"] for l in st_real.log_buffer if "批量" in l["msg"]] == ["  批量1", "  批量2"],
   "log_many 写入内存缓冲且顺序正确")
ck(len(st_real.log_buffer) == 4, "log_many 与 log 共用同一缓冲（实际 %d）" % len(st_real.log_buffer))
with open(os.path.join(tmpvar2, "logs", "usbcopy.log"), encoding="utf-8") as f:
    raw = [l.rstrip("\n") for l in f if l.strip()]
ck(len(raw) == 4, "4 条全部落盘（实际 %d）" % len(raw))
ck(all(len(l.split("\t")) == 4 for l in raw), "log_many 落盘仍是 4 段制表符格式")
ck(raw[1].endswith("\t  批量1") and raw[2].endswith("\t  批量2"), "落盘内容与顺序正确")
ck(st_real.log_max >= 5000, "内存环形缓冲已放大到 >=5000（避免清单挤掉开始行）")

# 6.3 极端安全阀仍在（>20000 条才提示）
big = [("/p/%d" % i, "err") for i in range(syncengine.ERR_HARD_LIMIT + 10)]
st2 = FakeState({})
syncengine._log_error_paths(st2, "t1", big)
ck(len([x for x in msgs(st2) if x.startswith("  ✖")]) == syncengine.ERR_HARD_LIMIT,
   "超过 20000 条时按安全阀截断")
ck(any("保护上限" in x for x in msgs(st2)), "安全阀触发时给出明确提示")

print()
print("=" * 72)
print("7) 设置默认值：notify_on_start=False / notify_on_finish=True")
print("=" * 72)

tmpetc = os.path.join(ROOT, "etc")
os.makedirs(tmpetc, exist_ok=True)
os.environ["TRIM_PKGETC"] = tmpetc
os.environ["TRIM_PKGVAR"] = os.path.join(ROOT, "var")
st = state_mod.AppState()
ck(st.settings.get("notify_on_start") is False, "notify_on_start 默认 False")
ck(st.settings.get("notify_on_finish") is True, "notify_on_finish 默认 True")
ck(st.settings.get("push_enabled") is False, "push_enabled 默认 False")
ck(state_mod.VERSION == "1.3.8", "state.VERSION = 1.3.8")
import server
ck(server.VERSION == "1.3.8", "server.VERSION = 1.3.8")

# 旧配置（无这两个字段）升级后应补全
with open(os.path.join(tmpetc, "settings.json"), "w", encoding="utf-8") as f:
    f.write('{"poll_interval": 5, "push_enabled": true}')
st2 = state_mod.AppState()
ck(st2.settings.get("notify_on_start") is False, "旧配置补全 notify_on_start=False")
ck(st2.settings.get("notify_on_finish") is True, "旧配置补全 notify_on_finish=True")

print()
print("=" * 72)
if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
print("=" * 72)
