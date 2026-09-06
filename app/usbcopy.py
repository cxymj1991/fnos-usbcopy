#!/usr/bin/env python3
"""USB Copy 同步 —— 主入口。

创建共享 AppState，启动 USB 监控线程与内置 Web 服务。由 cmd/main 以
nohup 方式拉起；收到 SIGTERM/SIGINT 时优雅退出。
"""

import os
import sys
import time
import signal
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state import AppState
import usbmonitor
import syncengine
import server


def main():
    state = AppState()
    httpd = server.make_server(state, state.service_port)

    def on_insert(task, device):
        syncengine.run_sync(state, task, device)

    mon = threading.Thread(target=usbmonitor.start_monitor, args=(state, on_insert), daemon=True)
    mon.start()

    def handle_term(signum, frame):
        state.stop.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    state.log("info", "USB Copy 同步服务已启动，监听端口 %d" % state.service_port)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        state.stop.set()
        state.log("info", "USB Copy 同步服务已停止")


if __name__ == "__main__":
    main()
