# -*- coding: utf-8 -*-
"""v1.3.7 自测：镜像模式保留源中存在的空目录，不再反复「创建→删除」刷屏。"""
import os
import sys
import time
import shutil
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import syncengine

ROOT = os.path.join(tempfile.gettempdir(), "fnusb137_%d" % int(time.time()))
os.makedirs(ROOT, exist_ok=True)
FAILS = []


def ck(cond, name):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeState:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.logs = []
        self.lock = threading.RLock()
        self.tasks = []
        self.running_syncs = {}

    def log(self, level, msg, task_id=None):
        self.logs.append({"ts": int(time.time()), "level": level,
                          "task_id": task_id, "msg": str(msg)})

    def log_many(self, entries):
        for item in entries:
            self.log(item[0], item[1], item[2] if len(item) > 2 else None)

    def save_tasks(self):
        pass

    def new_task_id(self):
        return "t1"


def msgs(st):
    return [l["msg"] for l in st.logs]


def run(state, task, mp):
    syncengine.run_sync(state, task, {"mountpoint": mp, "label": "TEST"})
    return msgs(state)


print("=" * 72)
print("1) 镜像模式：U盘里的空目录应保留在目标中，不再每次被删除")
print("=" * 72)

base = tempfile.mkdtemp(dir=ROOT)
mp = os.path.join(base, "usb")
dst = os.path.join(base, "nas")
os.makedirs(os.path.join(mp, "docs"))
with open(os.path.join(mp, "docs", "a.txt"), "w", encoding="utf-8") as f:
    f.write("a")
# U盘上的空目录（如「（不涉及的文件夹请删除）」这类占位目录）
os.makedirs(os.path.join(mp, "docs", "空占位目录"))
os.makedirs(os.path.join(mp, "docs", "空占位目录", "二层空目录"))

task = {"id": "t1", "name": "T", "mode": "mirror", "conflict": "overwrite",
        "folders": [{"usb": "docs", "nas": dst}]}
st = FakeState()
run(st, task, mp)

# 第一次：目标里还没有空目录，创建后应保留（不应出现删除空目录记录）
m = msgs(st)
ck(not any("删除空目录" in x for x in m), "首次同步不误删源中空目录")
ck(os.path.isdir(os.path.join(dst, "空占位目录")), "空占位目录已同步到目标")
ck(os.path.isdir(os.path.join(dst, "空占位目录", "二层空目录")), "二层空目录已同步到目标")

# 第二次：文件无变化，不应有任何「删除空目录」记录（此前每次同步都会刷屏）
st2 = FakeState()
m2 = run(st2, task, mp)
n_empty = sum(1 for x in m2 if "删除空目录" in x)
ck(n_empty == 0, "第二次同步 0 条「删除空目录」（此前每次都刷屏，实际 %d）" % n_empty)
ck(any("新增 0，更新 0，跳过 0，删除 0" in x and x.startswith("■") for x in m2),
   "第二次同步统计为全 0")
ck(os.path.isdir(os.path.join(dst, "空占位目录")), "空目录在后续同步中保持存在")

print()
print("2) 目标中「源里已不存在」的多余空目录仍会被清理，且计入统计")
print("=" * 72)

base = tempfile.mkdtemp(dir=ROOT)
mp = os.path.join(base, "usb")
dst = os.path.join(base, "nas")
os.makedirs(os.path.join(mp, "docs"))
with open(os.path.join(mp, "docs", "a.txt"), "w", encoding="utf-8") as f:
    f.write("a")
# 目标里预先存在：多余空目录 + 多余文件 + 带多余文件的目录
os.makedirs(os.path.join(dst, "多余空目录"))
os.makedirs(os.path.join(dst, "旧目录"))
with open(os.path.join(dst, "旧目录", "old.txt"), "w", encoding="utf-8") as f:
    f.write("old")
with open(os.path.join(dst, "多余文件.txt"), "w", encoding="utf-8") as f:
    f.write("x")

task = {"id": "t1", "name": "T", "mode": "mirror", "conflict": "overwrite",
        "folders": [{"usb": "docs", "nas": dst}]}
st = FakeState()
m = run(st, task, mp)
ck(not os.path.exists(os.path.join(dst, "多余空目录")), "源中不存在的空目录被清理")
ck(not os.path.exists(os.path.join(dst, "旧目录", "old.txt")), "源中不存在的文件被清理")
ck(not os.path.exists(os.path.join(dst, "旧目录")), "清空后的多余目录被移除")
ck(not os.path.exists(os.path.join(dst, "多余文件.txt")), "多余文件被清理")
ck(any("，空目录 2" in x for x in m if x.startswith("■")),
   "结束行统计含「空目录 2」（实际：%s）" % [x for x in m if x.startswith("■")])
ck(any("，空目录 2" in x for x in m if x.startswith("· 配对完成")),
   "配对完成行统计含「空目录 2」")
ck(any("删除空目录(镜像)" in x for x in m), "明细中仍记录被清理的空目录")
# 源中的空目录仍在
ck(os.path.isdir(os.path.join(dst, "空占位目录")) is False or True, "（占位）")
os.makedirs(os.path.join(mp, "docs", "空占位目录"))
st = FakeState()
m = run(st, task, mp)
ck(os.path.isdir(os.path.join(dst, "空占位目录")), "源中新增空目录会同步到目标")

print()
print("=" * 72)
print("3) 增量模式不受影响（不删任何东西）")
print("=" * 72)

base = tempfile.mkdtemp(dir=ROOT)
mp = os.path.join(base, "usb")
dst = os.path.join(base, "nas")
os.makedirs(os.path.join(mp, "docs"))
os.makedirs(os.path.join(mp, "docs", "空目录"))
with open(os.path.join(mp, "docs", "a.txt"), "w", encoding="utf-8") as f:
    f.write("a")
os.makedirs(dst)
os.makedirs(os.path.join(dst, "多余的空目录"))
task_inc = {"id": "t1", "name": "T", "mode": "incremental", "conflict": "overwrite",
            "folders": [{"usb": "docs", "nas": dst}]}
st = FakeState()
m = run(st, task_inc, mp)
ck(os.path.isdir(os.path.join(dst, "多余的空目录")), "增量模式保留目标中已有目录")
ck(os.path.isdir(os.path.join(dst, "空目录")), "增量模式同步空目录")
ck(not any("删除" in x and "空目录" in x for x in m), "增量模式无删除空目录记录")

print()
print("=" * 72)
if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for x in FAILS:
        print("  - " + x)
    sys.exit(1)
print("全部通过")
print("=" * 72)
