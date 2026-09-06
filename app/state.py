"""共享状态、配置持久化与日志。

所有模块共享一个 AppState 实例（在 usbcopy.py 中创建并向下传递）。
目录位置优先使用 FnOS 注入的环境变量（TRIM_PKGVAR / TRIM_PKGETC），
在本地开发环境下回退到脚本附近的 var/ etc/ 目录。
"""

import os
import json
import time
import threading


APP_NAME = "fn-usbcopy"
VERSION = "1.3.8"


def _default_var():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "var")


def _default_etc():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "etc")


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.devices = []            # 当前已挂载的 USB 设备列表
        self.running_syncs = {}      # task_id -> {start, device}
        self.log_buffer = []         # 内存环形日志
        self.log_max = 5000          # 放大到 5000：出错清单可能很长，避免挤掉开始行
        self.app_dir = os.path.dirname(os.path.abspath(__file__))  # app/

        self.var_dir = os.environ.get("TRIM_PKGVAR") or _default_var()
        self.etc_dir = os.environ.get("TRIM_PKGETC") or _default_etc()

        port = os.environ.get("TRIM_SERVICE_PORT") or "8765"
        try:
            self.service_port = int(port)
        except Exception:
            self.service_port = 8765

        self.tasks = []
        self.settings = {}
        self._init_dirs()
        self.load()

    def _init_dirs(self):
        try:
            os.makedirs(self.var_dir, exist_ok=True)
            os.makedirs(os.path.join(self.var_dir, "logs"), exist_ok=True)
            os.makedirs(self.etc_dir, exist_ok=True)
        except Exception:
            pass

    def log(self, level, msg, task_id=None):
        entry = {
            "ts": int(time.time()),
            "level": level,
            "msg": str(msg),
            "task_id": task_id,
        }
        with self.lock:
            self.log_buffer.append(entry)
            if len(self.log_buffer) > self.log_max:
                self.log_buffer = self.log_buffer[-self.log_max:]
        try:
            p = os.path.join(self.var_dir, "logs", "usbcopy.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write("%d\t%s\t%s\t%s\n" % (entry["ts"], level, task_id or "-", msg))
        except Exception:
            pass

    def log_many(self, entries):
        """批量写日志：entries 为 [(level, msg, task_id), ...]。

        与逐条调用 log() 效果一致（内存缓冲 + 落盘同格式），但只开关一次
        日志文件，用于出错路径清单这类可能成百上千条的批量输出。
        """
        if not entries:
            return
        now = int(time.time())
        lines = []
        with self.lock:
            for item in entries:
                level, msg, task_id = item[0], str(item[1]), (item[2] if len(item) > 2 else None)
                self.log_buffer.append({"ts": now, "level": level,
                                        "msg": msg, "task_id": task_id})
                lines.append("%d\t%s\t%s\t%s\n" % (now, level, task_id or "-", msg))
            if len(self.log_buffer) > self.log_max:
                self.log_buffer = self.log_buffer[-self.log_max:]
        try:
            p = os.path.join(self.var_dir, "logs", "usbcopy.log")
            with open(p, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass

    def load(self):
        tp = os.path.join(self.etc_dir, "tasks.json")
        try:
            with open(tp, "r", encoding="utf-8") as f:
                self.tasks = json.load(f)
        except Exception:
            self.tasks = []
        sp = os.path.join(self.etc_dir, "settings.json")
        try:
            with open(sp, "r", encoding="utf-8") as f:
                self.settings = json.load(f)
        except Exception:
            self.settings = {}
        self.settings.setdefault("poll_interval", 5)
        self.settings.setdefault("log_retention_days", 30)
        self.settings.setdefault("log_file_detail", True)   # 日志记录变更文件明细
        self.settings.setdefault("push_enabled", False)     # 手机推送总开关
        self.settings.setdefault("notify_on_start", False)  # 同步开始时推送（默认关，避免打扰）
        self.settings.setdefault("notify_on_finish", True)  # 同步完成/失败时推送（默认开）
        self.settings.setdefault("push_channels", "")       # pushplus,smtp
        self.settings.setdefault("pushplus_token", "")
        self.settings.setdefault("smtp_host", "")
        self.settings.setdefault("smtp_port", 465)
        self.settings.setdefault("smtp_user", "")
        self.settings.setdefault("smtp_pass", "")
        self.settings.setdefault("smtp_from", "")
        self.settings.setdefault("smtp_to", "")

    def save_tasks(self):
        tp = os.path.join(self.etc_dir, "tasks.json")
        tmp = tp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, tp)

    def save_settings(self):
        sp = os.path.join(self.etc_dir, "settings.json")
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sp)

    def new_task_id(self):
        return "t" + str(int(time.time() * 1000))
