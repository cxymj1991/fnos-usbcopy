"""内置 Web 服务：提供管理界面静态资源与 REST API。

FnOS 桌面以 iframe 形式打开 http://<NAS>:8765/，由本服务直接托管。
所有接口返回 JSON（/ 与 /static 返回静态资源）。
"""

import os
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

import notify
import syncengine
import usbmonitor

VERSION = "1.3.8"

_CTYPE = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _ctype(name):
    ext = os.path.splitext(name)[1].lower()
    return _CTYPE.get(ext, "application/octet-stream")


def _as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]
    return []


def _normalize_folder_pair(f):
    """将任意来源的对象规整为一对 {usb, nas} 文件夹映射。"""
    if not isinstance(f, dict):
        return None
    usb = (f.get("usb") or "").strip()
    nas = (f.get("nas") or "").strip()
    if not usb and not nas:
        return None
    return {"usb": usb.strip("/"), "nas": nas.rstrip("/")}


def _normalize_task(data, state, existing=None):
    t = dict(existing) if existing else {}
    t["id"] = data.get("id") or t.get("id") or state.new_task_id()
    t["name"] = str(data.get("name") or t.get("name") or "未命名任务")
    t["enabled"] = bool(data.get("enabled", t.get("enabled", True)))

    # 识别方式（U盘）：优先取传入的 usb_match；否则由设备选择回填
    m = data.get("usb_match") or t.get("usb_match") or {}
    t["usb_match"] = {
        "type": (m.get("type") or "any").lower(),
        "value": (m.get("value") or "").strip(),
    }

    # 文件夹配对：folders:[{usb, nas}]；兼容旧的单一字段
    folders = []
    raw = data.get("folders")
    if raw is None:
        raw = t.get("folders")
    if isinstance(raw, list):
        for f in raw:
            pair = _normalize_folder_pair(f)
            if pair:
                folders.append(pair)
    if not folders:
        legacy_src = (data.get("usb_source_folder") if isinstance(data, dict) else None)
        if legacy_src is None:
            legacy_src = t.get("usb_source_folder")
        legacy_dst = (data.get("nas_dest_folder") if isinstance(data, dict) else None)
        if legacy_dst is None:
            legacy_dst = t.get("nas_dest_folder")
        if legacy_src or legacy_dst:
            pair = _normalize_folder_pair({"usb": legacy_src or "", "nas": legacy_dst or ""})
            if pair:
                folders.append(pair)
    t["folders"] = folders

    t["mode"] = data.get("mode") or t.get("mode") or "incremental"
    flt = data.get("filters") or t.get("filters") or {}
    t["filters"] = {
        "include": _as_list(flt.get("include")),
        "exclude": _as_list(flt.get("exclude")),
    }
    t["conflict"] = data.get("conflict") or t.get("conflict") or "overwrite"
    t["eject_after"] = bool(data.get("eject_after", t.get("eject_after", False)))
    t["notify"] = bool(data.get("notify", t.get("notify", True)))
    t["last_run"] = t.get("last_run")
    t["last_status"] = t.get("last_status")
    t["last_msg"] = t.get("last_msg")
    return t


def _find_task(state, tid):
    for t in state.tasks:
        if t["id"] == tid:
            return t
    return None


def _find_device_for_task(state, task):
    for d in state.devices:
        if usbmonitor._match(task, d):
            return d
    return None


def _netdisk_mounts():
    """返回应视为「网盘/远程挂载」的路径集合，浏览时排除。

    飞牛OS 的网盘挂载基于 rclone FUSE（/proc/mounts 中 fstype 为 fuse.rclone 等），
    挂载点可能在存储卷下（如 /vol02/1000-1-xxx）或用户自建目录里；SMB/NFS 等
    远程共享一并排除，保证选择器只列本地硬盘文件夹。
    """
    mounts = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1]
                fstype = parts[2].lower()
                if ("fuse" in fstype
                        or fstype in ("rclone", "webdav", "davfs",
                                      "baidunetdisk", "aliyunpan",
                                      "smbfs", "cifs", "nfs", "nfs4")):
                    mounts.add(os.path.normpath(mp))
    except Exception:
        pass
    return mounts


def _list_dirs(base):
    """返回 base 下的子目录列表（名称 + 绝对路径），排除网盘/远程挂载点。"""
    out = []
    net = _netdisk_mounts()
    try:
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if not os.path.isdir(full):
                continue
            if full in net:
                continue
            out.append({"name": name, "path": full})
    except Exception:
        pass
    return out


def _nas_homes():
    """自动识别用户「我的文件」的根目录列表（可跨多个存储空间）。

    扫描每个存储卷（/vol1、/vol2 …）下所有数字命名的用户目录（如 /vol1/1000、
    /vol2/1000），并排除网盘/远程挂载点（如 /vol02/1000-1-xxx 等 rclone FUSE 挂载）。
    """
    homes = []
    net = _netdisk_mounts()
    try:
        vols = [n for n in os.listdir("/") if n.startswith("vol") and n[3:].isdigit()]
        vols.sort(key=lambda n: int(n[3:]))
        for v in vols:
            try:
                for ent in sorted(os.listdir("/" + v)):
                    if ent.isdigit():
                        cand = os.path.normpath("/" + v + "/" + ent)
                        if cand not in homes and cand not in net and os.path.isdir(cand):
                            homes.append(cand)
            except Exception:
                pass
    except Exception:
        pass
    if not homes:
        homes = [os.path.normpath("/vol1/1000")]
    return homes


def _browse(scope, path, uuid, state):
    """目录浏览：scope=nas 时仅限 /vol*；scope=usb 时仅限对应设备挂载点内。"""
    if scope == "usb":
        dev = None
        for d in state.devices:
            if d.get("uuid") == uuid or d.get("name") == uuid:
                dev = d
                break
        if not dev or not dev.get("mountpoint"):
            return {"ok": False, "msg": "未找到该U盘或U盘未挂载",
                    "base": "", "parent": "", "dirs": [], "label": ""}
        mp = dev["mountpoint"].rstrip("/")
        base = os.path.normpath(os.path.join(mp, (path or "").lstrip("/")))
        if base != mp and not base.startswith(mp + os.sep):
            base = mp
        parent = os.path.dirname(base) if base != mp else ""
        return {
            "ok": True, "scope": "usb", "uuid": uuid,
            "base": base, "parent": parent, "dirs": _list_dirs(base),
            "label": dev.get("label") or dev.get("uuid"),
        }
    # nas：仅展示用户「我的文件」（自动扫描所有存储卷下的用户目录，如 /vol1/1000、
    # /vol2/1000）及其子文件夹，不暴露整个系统目录树，不列网盘/远程挂载。
    homes = _nas_homes()
    if not path or path.strip() == "/":
        dirs = [{"name": h, "path": h} for h in homes]
        return {
            "ok": True, "scope": "nas", "uuid": "",
            "base": "/", "parent": "", "dirs": dirs, "homes": True,
            "label": "我的文件（选择存储空间）",
        }
    base = os.path.normpath(path.strip())
    home = None
    for h in homes:
        if base == h or base.startswith(h + os.sep) or base.startswith(h + "/"):
            home = h
            break
    if home is None:
        home = homes[0]
        base = home
    while base != "/" and base != home and not os.path.isdir(base):
        base = os.path.dirname(base)
    parent = "/" if base == home else os.path.dirname(base)
    return {
        "ok": True, "scope": "nas", "uuid": "",
        "base": base, "parent": parent, "dirs": _list_dirs(base),
        "label": "我的文件",
    }


def _read_file_logs(state, limit=5000):
    """读取已保存的日志文件（var/logs/usbcopy.log），返回最近 limit 条结构化日志。

    文件行格式：<ts>\\t<level>\\t<task_id>\\t<msg>（与 state.log 的落盘格式一致）。
    """
    path = os.path.join(state.var_dir, "logs", "usbcopy.log")
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t", 3)
                ts = parts[0] if len(parts) > 0 else "0"
                level = parts[1] if len(parts) > 1 else "info"
                task_id = parts[2] if len(parts) > 2 else ""
                msg = parts[3] if len(parts) > 3 else ""
                try:
                    ts = int(ts)
                except Exception:
                    ts = 0
                out.append({"ts": ts, "level": level,
                            "task_id": task_id or None, "msg": msg})
    except Exception:
        pass
    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:]
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _state(self):
        return self.server.state

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        up = urllib.parse.urlparse(self.path)
        p = up.path
        st = self._state()
        if p in ("/", "/index.html"):
            self._send_file(os.path.join(st.app_dir, "www", "index.html"),
                            "text/html; charset=utf-8")
        elif p.startswith("/static/"):
            self._send_file(os.path.join(st.app_dir, "www", os.path.basename(p)), _ctype(p))
        elif p == "/api/status":
            self._send_json({
                "ok": True, "running": True, "version": VERSION,
                "service_port": st.service_port,
                "devices": st.devices,
                "running_syncs": list(st.running_syncs.keys()),
                "poll_interval": st.settings.get("poll_interval", 5),
            })
        elif p == "/api/devices":
            self._send_json({"ok": True, "devices": st.devices})
        elif p == "/api/tasks":
            self._send_json({"ok": True, "tasks": st.tasks})
        elif p == "/api/settings":
            self._send_json({"ok": True, "settings": st.settings})
        elif p == "/api/browse":
            q = urllib.parse.parse_qs(up.query)
            scope = (q.get("scope", ["nas"])[0] or "nas").lower()
            path = q.get("path", [""])[0] or ""
            uuid = q.get("uuid", [""])[0] or ""
            self._send_json(_browse(scope, path, uuid, st))
        elif p == "/api/logs":
            # 出错路径清单可能很长，默认放宽到 2000 条；?limit=N 可自行调整
            q = urllib.parse.parse_qs(up.query)
            try:
                limit = int(q.get("limit", ["2000"])[0] or 2000)
            except Exception:
                limit = 2000
            limit = max(1, min(limit, st.log_max))
            self._send_json({"ok": True, "logs": st.log_buffer[-limit:]})
        elif p == "/api/logs/full":
            # 完整已保存日志：?limit=N 限制条数；?raw=1 直接下载 usbcopy.log 文件
            q = urllib.parse.parse_qs(up.query)
            try:
                limit = int(q.get("limit", ["5000"])[0] or 5000)
            except Exception:
                limit = 5000
            if q.get("raw", ["0"])[0] in ("1", "true"):
                path = os.path.join(st.var_dir, "logs", "usbcopy.log")
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        data = f.read().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     'attachment; filename="usbcopy.log"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_error(404)
                return
            logs = _read_file_logs(st, limit)
            self._send_json({"ok": True, "logs": logs, "total": len(logs)})
        elif p.startswith("/api/tasks/") and p.endswith("/logs"):
            tid = p.split("/")[3]
            q = urllib.parse.parse_qs(up.query)
            try:
                limit = int(q.get("limit", ["200"])[0] or 200)
            except Exception:
                limit = 200
            limit = max(1, min(limit, st.log_max))
            logs = [l for l in st.log_buffer if l.get("task_id") == tid][-limit:]
            self._send_json({"ok": True, "logs": logs})
        else:
            self.send_error(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        st = self._state()
        data = self._read_body()
        if p == "/api/tasks":
            with st.lock:
                task = _normalize_task(data, st)
                st.tasks.append(task)
                st.save_tasks()
            st.log("info", "创建任务 [%s]" % task.get("name"), task["id"])
            self._send_json({"ok": True, "task": task})
        elif p.startswith("/api/tasks/") and p.endswith("/run"):
            tid = p.split("/")[3]
            task = _find_task(st, tid)
            if not task:
                self._send_json({"ok": False, "msg": "任务不存在"}, 400)
                return
            dev = _find_device_for_task(st, task)
            if not dev:
                self._send_json({"ok": False, "msg": "未检测到匹配的U盘，请插入对应U盘后再试"}, 400)
                return
            if tid in st.running_syncs:
                self._send_json({"ok": False, "msg": "该任务正在同步中"}, 400)
                return
            threading.Thread(target=syncengine.run_sync, args=(st, task, dev), daemon=True).start()
            self._send_json({"ok": True, "msg": "已触发同步"})
        elif p == "/api/settings":
            with st.lock:
                st.settings.update(data)
                st.save_settings()
            self._send_json({"ok": True, "settings": st.settings})
        elif p == "/api/test_push":
            # 测试推送：先保存传入配置（便于未保存即测试），再逐渠道发一条测试消息
            with st.lock:
                st.settings.update(data or {})
                st.save_settings()
            results = notify.send_push(
                st.settings, "USB Copy 同步 · 测试推送",
                "这是一条测试推送。如果你在手机上看到它，说明推送配置已生效。")
            self._send_json({
                "ok": True,
                "results": [{"channel": c, "result": r} for c, r in results],
            })
        else:
            self.send_error(404)

    def do_PUT(self):
        p = urllib.parse.urlparse(self.path).path
        st = self._state()
        parts = p.split("/")
        if p.startswith("/api/tasks/") and len(parts) == 4:
            tid = parts[3]
            data = self._read_body()
            with st.lock:
                task = _find_task(st, tid)
                if not task:
                    self._send_json({"ok": False, "msg": "任务不存在"}, 404)
                    return
                task.update(_normalize_task(data, st, existing=task))
                st.save_tasks()
            self._send_json({"ok": True, "task": task})
        else:
            self.send_error(404)

    def do_DELETE(self):
        p = urllib.parse.urlparse(self.path).path
        st = self._state()
        parts = p.split("/")
        if p.startswith("/api/tasks/") and len(parts) == 4:
            tid = parts[3]
            with st.lock:
                st.tasks = [t for t in st.tasks if t["id"] != tid]
                st.save_tasks()
            self._send_json({"ok": True})
        else:
            self.send_error(404)


def make_server(state, port):
    state.app_dir = os.path.dirname(os.path.abspath(__file__))  # app/
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.state = state
    return httpd
