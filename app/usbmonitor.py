"""USB 设备检测与插入监控。

通过 lsblk 枚举块设备，结合 /sys 中的 removable / usb 信息识别可移动 USB
分区，并读取其挂载点、UUID、卷标、序列号等。监控线程周期性轮询，在检测到
与某个已启用任务匹配的U盘插入时回调 on_insert(task, device)。
"""

import os
import re
import json
import subprocess
import threading


_DISK_RE = re.compile(r"p?\d+$")


def _disk_name(name):
    """从分区名得到磁盘名：sdb1 -> sdb, nvme0n1p1 -> nvme0n1, mmcblk0p1 -> mmcblk0"""
    return _DISK_RE.sub("", name)


def _is_removable(disk):
    for base in ("/sys/block/", "/sys/class/block/"):
        p = base + disk + "/removable"
        try:
            with open(p) as f:
                if f.read().strip() == "1":
                    return True
        except Exception:
            pass
    # 通过设备路径判断是否挂在 usb 总线下
    try:
        real = os.path.realpath("/sys/block/" + disk + "/device")
        if "/usb" in real:
            return True
    except Exception:
        pass
    return False


def _iter(nodes):
    for n in nodes:
        yield n
        for c in n.get("children", []):
            yield from _iter([c])


def _enumerate_partitions():
    """枚举当前所有可移动 USB 分区（含未挂载的，mountpoint 可能为 None）。"""
    parts = []
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,TRAN,TYPE,FSTYPE,UUID,LABEL,SERIAL,MODEL,SIZE,MOUNTPOINT"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return parts
        data = json.loads(out.stdout)
        nodes = data.get("blockdevices", [])
    except Exception:
        return parts

    for dev in _iter(nodes):
        if dev.get("type") != "part":
            continue
        name = dev.get("name", "")
        tran = (dev.get("tran") or "").lower()
        removable = _is_removable(_disk_name(name))
        if tran not in ("usb", "esata") and not removable:
            continue
        parts.append({
            "name": name,
            "uuid": (dev.get("uuid") or "").strip(),
            "label": (dev.get("label") or "").strip(),
            "serial": (dev.get("serial") or "").strip(),
            "model": (dev.get("model") or "").strip(),
            "fstype": (dev.get("fstype") or "").strip(),
            "size": dev.get("size") or 0,
            "mountpoint": (dev.get("mountpoint") or "").strip() or None,
        })
    return parts


def list_usb_devices():
    """返回当前已挂载的可移动 USB 分区信息列表。"""
    return [d for d in _enumerate_partitions() if d.get("mountpoint")]


def _dev_key(d):
    """轮询去重键：设备标识 + 设备名 + 挂载点。

    关键：换插口后设备名会变（如 sdb1 → sdc1）、挂载点随之变化，
    必须视为一次「新的插入」重新触发同步；否则若拔出/重插之间
    没赶上轮询周期，仅按 UUID 去重就会漏掉这次插入。
    """
    return "%s|%s|%s" % (d.get("uuid") or d.get("label")
                         or d.get("serial") or "-",
                         d.get("name"), d.get("mountpoint"))


def _match(task, device):
    m = task.get("usb_match") or {}
    t = (m.get("type") or "any").lower()
    v = (m.get("value") or "").strip().lower()
    if t == "any" or not v:
        return True
    if t == "uuid":
        return device.get("uuid", "").lower() == v
    if t == "label":
        return device.get("label", "").lower() == v
    if t == "serial":
        return device.get("serial", "").lower() == v
    return False


def _dev_desc(d):
    return d.get("label") or d.get("uuid") or d.get("name")


def start_monitor(state, on_insert):
    """周期性检测已插入且与任务匹配的U盘，触发同步（每个插入事件仅触发一次）。

    去重键含设备名与挂载点：U 盘换一个插口后设备名会变（sdb1→sdc1），
    挂载点随之改变，会被视为一次新的插入而重新触发；同一次插入期间
    （设备名与挂载点不变）不会重复触发。
    """
    seen = {}          # device_key -> set(task_id)   已触发过的（设备+任务）
    warned_mp = set()  # 已提示过「存在但未挂载」的设备名，避免刷屏
    while not state.stop.is_set():
        try:
            parts = _enumerate_partitions()
            devs = [d for d in parts if d.get("mountpoint")]
            with state.lock:
                state.devices = devs
            current = set()

            # 可移动 USB 分区存在但没有挂载点 → 记一次性提醒（换插口后
            # 个别口/读卡器可能不自动挂载，此时无法同步，需在 fnOS 文件管理挂载）
            for d in parts:
                if not d.get("mountpoint"):
                    if d["name"] not in warned_mp:
                        warned_mp.add(d["name"])
                        state.log("warn",
                                  "检测到U盘分区 %s（%s）尚未挂载，无法同步；"
                                  "请在飞牛「文件管理」中挂载该U盘后重试"
                                  % (d["name"], _dev_desc(d) or d["name"]))
                else:
                    warned_mp.discard(d["name"])

            for d in devs:
                key = _dev_key(d)
                current.add(key)
                for task in list(state.tasks):
                    if not task.get("enabled", True):
                        continue
                    if not _match(task, d):
                        continue
                    triggered = seen.setdefault(key, set())
                    if task["id"] in triggered:
                        continue
                    triggered.add(task["id"])
                    m = task.get("usb_match") or {}
                    state.log("info",
                              "检测到匹配的U盘 [%s]（按 %s=%s 识别，设备 %s，挂载于 %s），"
                              "触发任务 [%s]"
                              % (_dev_desc(d),
                                 m.get("type") or "any", m.get("value") or "任意",
                                 d.get("name"), d.get("mountpoint"),
                                 task.get("name")),
                              task["id"])
                    threading.Thread(target=on_insert, args=(task, d), daemon=True).start()
            for k in list(seen.keys()):
                if k not in current:
                    del seen[k]
        except Exception as e:
            state.log("error", "监控异常: %s" % e)
        state.stop.wait(max(1, int(state.settings.get("poll_interval", 5))))
