# -*- coding: utf-8 -*-
"""v1.3.6 自测：换插口（设备名变化）能重新触发同步；未挂载U盘有一次性提醒。"""
import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

import usbmonitor

FAILS = []


def ck(cond, name):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def dev(name, mp, uuid="U1", label="DISK"):
    return {"name": name, "uuid": uuid, "label": label, "serial": "S1",
            "model": "M", "fstype": "exfat", "size": 1000, "mountpoint": mp}


class FakeState:
    def __init__(self, tasks):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.settings = {"poll_interval": 5}
        self.tasks = tasks
        self.devices = []
        self.logs = []
        self._script = []
        self._calls = 0

    def set_script(self, states):
        self._script = list(states)
        self._calls = 0

    def log(self, level, msg, task_id=None):
        self.logs.append({"ts": int(time.time()), "level": level,
                          "task_id": task_id, "msg": str(msg)})


def run_monitor(state, on_insert):
    """脚本化 _enumerate_partitions：每轮返回下一组设备，用尽后停止监控。"""
    orig_enum = usbmonitor._enumerate_partitions

    def scripted():
        i = state._calls
        state._calls += 1
        if i >= len(state._script):
            state.stop.set()
            return []
        return state._script[i]

    usbmonitor._enumerate_partitions = scripted
    orig_wait = state.stop.wait
    state.stop.wait = lambda t: (time.sleep(0.03), False)[1]   # 加速轮询
    try:
        usbmonitor.start_monitor(state, on_insert)
    finally:
        usbmonitor._enumerate_partitions = orig_enum


def msgs(st):
    return [l["msg"] for l in st.logs]


TASK = {"id": "t1", "name": "T", "enabled": True,
        "usb_match": {"type": "uuid", "value": "u1"},
        "folders": [{"usb": "", "nas": "/vol1/1000/bak"}]}

print("=" * 72)
print("1) 换插口场景：同盘换口（设备名 sdb1→sdc1）应重新触发同步")
print("=" * 72)

st = FakeState([dict(TASK)])
hits = []
hlock = threading.Lock()


def on_insert(task, d):
    with hlock:
        hits.append((task["id"], d["name"]))


st.set_script([
    [dev("sdb1", "/media/sdb1")],   # ① 插入 A 口 → 触发
    [dev("sdb1", "/media/sdb1")],   # ② 仍在 A 口 → 不重复
    [dev("sdc1", "/media/sdc1")],   # ③ 换到 B 口（设备名变化）→ 再次触发
    [dev("sdc1", "/media/sdc1")],   # ④ 仍在 B 口 → 不重复
])
run_monitor(st, on_insert)
time.sleep(0.2)
for x in hits:
    print("   触发: task=%s dev=%s" % x)
ck(len(hits) == 2, "同盘换口触发 2 次（实际 %d）" % len(hits))
ck([h[1] for h in hits] == ["sdb1", "sdc1"], "两次分别在 sdb1 与 sdc1 上触发")

print()
print("2) 拔出重插：轮询间隙拔出（未捕捉到消失）+ 换口 → 必须触发")
print("=" * 72)

st = FakeState([dict(TASK)])
hits2 = []


def on_insert2(task, d):
    with hlock:
        hits2.append(d["name"])


st.set_script([
    [dev("sdb1", "/media/sdb1")],   # ① 插入 → 触发
    [dev("sdb1", "/media/sdb1")],   # ② 仍在（此时用户拔出又插到 B 口，轮询未捕捉到空窗）
    [dev("sdc1", "/media/sdc1")],   # ③ B 口 → 应触发
])
run_monitor(st, on_insert2)
time.sleep(0.2)
ck(hits2 == ["sdb1", "sdc1"], "快速拔插+换口也触发（实际 %s）" % hits2)

print()
print("3) 同一次插入期间（设备与挂载点不变）不重复触发")
print("=" * 72)

st = FakeState([dict(TASK)])
hits3 = []


def on_insert3(task, d):
    with hlock:
        hits3.append(d["name"])


st.set_script([
    [dev("sdb1", "/media/sdb1")],
    [dev("sdb1", "/media/sdb1")],
    [dev("sdb1", "/media/sdb1")],
    [dev("sdb1", "/media/sdb1")],
])
run_monitor(st, on_insert3)
time.sleep(0.2)
ck(len(hits3) == 1, "4 轮轮询只触发 1 次（实际 %d）" % len(hits3))

print()
print("4) 拔出后再插回：正常重新触发")
print("=" * 72)

st = FakeState([dict(TASK)])
hits4 = []


def on_insert4(task, d):
    with hlock:
        hits4.append(d["name"])


st.set_script([
    [dev("sdb1", "/media/sdb1")],   # ① 触发
    [],                              # ② 拔出（轮询捕捉到空窗）
    [dev("sdb1", "/media/sdb1")],   # ③ 重新插入 → 触发
])
run_monitor(st, on_insert4)
time.sleep(0.2)
ck(hits4 == ["sdb1", "sdb1"], "拔出后插回重新触发（实际 %s）" % hits4)

print()
print("5) U盘存在但未挂载：一次性提醒，不触发同步也不刷屏")
print("=" * 72)

st = FakeState([dict(TASK)])
hits5 = []


def on_insert5(task, d):
    with hlock:
        hits5.append(d["name"])


st.set_script([
    [dev("sdb1", None)],            # ① 插入但未挂载 → 提醒，不触发
    [dev("sdb1", None)],            # ② 仍未挂载 → 不重复提醒
    [dev("sdb1", "/media/sdb1")],   # ③ 挂载成功 → 触发
])
run_monitor(st, on_insert5)
time.sleep(0.2)
m = msgs(st)
ck(len(hits5) == 1, "挂载成功后正常触发（实际 %s）" % hits5)
ck(sum(1 for x in m if "尚未挂载" in x) == 1, "未挂载仅提醒一次（实际 %d）"
   % sum(1 for x in m if "尚未挂载" in x))
ck(any("尚未挂载" in x and "sdb1" in x for x in m), "提醒内容含设备名")
ck(any("按 uuid=u1 识别" in x for x in m), "触发日志写明识别依据")
ck(any("挂载于 /media/sdb1" in x for x in m), "触发日志写明挂载点")

print()
print("=" * 72)
if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for x in FAILS:
        print("  - " + x)
    sys.exit(1)
print("全部通过")
print("=" * 72)
