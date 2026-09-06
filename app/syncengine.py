"""同步引擎。

参照群晖 USB Copy 提供三种复制模式：
- mirror（镜像）：使目标成为源的完整镜像，删除源中已不存在的文件。
- incremental（增量）：仅复制新增/修改的文件，不删除目标中的其它文件。
- multiversion（多版本）：每次运行在目标下新建以时间命名的子目录并完整复制。

支持文件过滤（include/exclude glob）、冲突策略（overwrite/skip/rename）、
完成后自动弹出U盘与手机推送（微信 PushPlus / SMTP 邮件，见 notify.py）。

日志约定（前端据首字符决定是否显示时间列）：
    "▶ …"  同步开始里程碑，含完整日期时间    → 显示时间
    "■ …"  同步结束里程碑，含完整日期时间与耗时 → 显示时间
    "· …"  过程/配对信息                    → 不显示时间
    "  …"  变更明细、出错路径清单子行         → 不显示时间
出错路径清单在每次同步的最后写入，界面按最新在前展示，因此位于该次同步最上方。
"""

import os
import fnmatch
import shutil
import time
import subprocess


def _should_include(rel_key, include, exclude):
    if include:
        ok = any(
            fnmatch.fnmatch(rel_key, p) or fnmatch.fnmatch(os.path.basename(rel_key), p)
            for p in include
        )
        if not ok:
            return False
    if exclude:
        if any(
            fnmatch.fnmatch(rel_key, p) or fnmatch.fnmatch(os.path.basename(rel_key), p)
            for p in exclude
        ):
            return False
    return True


def _human(n):
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.2f MB" % (n / 1024 ** 2)
    if n >= 1024:
        return "%.1f KB" % (n / 1024)
    return "%d B" % n


def _dur(sec):
    """把秒数格式化为「X 小时 Y 分 Z 秒」的可读耗时。"""
    try:
        sec = max(0, int(round(float(sec))))
    except Exception:
        return "-"
    if sec < 60:
        return "%d 秒" % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return "%d 分 %02d 秒" % (m, s)
    h, m = divmod(m, 60)
    return "%d 小时 %d 分 %02d 秒" % (h, m, s)


def _now():
    """可读的本地日期时间，用于日志的开始/结束时间戳。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _push_mobile(state, title, body):
    """按设置推送微信/邮件到手机。结果记入日志。"""
    try:
        from notify import send_push
        results = send_push(state.settings, title, body)
        for c, r in results:
            if r == "ok":
                state.log("info", "手机推送(%s)成功" % c)
            else:
                state.log("warn", "手机推送(%s)失败: %s" % (c, r))
    except Exception as e:
        state.log("warn", "手机推送异常: %s" % e)


def _notify(state, title, body):
    """同步开始/完成/失败后的通知：手机推送（微信/邮件）。

    注：fnOS 未向第三方应用开放写入系统通知中心的接口（pyfnos Notify 仅有读取
    未读数），且系统无 /usr/bin/push 命令，故不再尝试系统级通知。
    """
    _push_mobile(state, title, body)


def _err_path(p):
    """超长路径截断（保留尾部文件名），避免撑爆推送正文与日志行。"""
    p = str(p)
    return p if len(p) <= 220 else "…" + p[-219:]


def _format_error_paths(error_paths, limit=20):
    """把出错路径清单格式化为「推送正文」片段。

    无错误时返回空串；有错误时以「出错文件（共 N 个）：」开头，逐条列出
    「路径 ｜ 原因」。推送内容不宜过长，默认最多列 20 条，其余提示看日志。
    """
    if not error_paths:
        return ""
    n = len(error_paths)
    lines = ["", "出错文件（共 %d 个）：" % n]
    for i, (p, why) in enumerate(error_paths[:limit], 1):
        lines.append("%d. %s ｜ %s" % (i, _err_path(p), why))
    if n > limit:
        lines.append("…其余 %d 个未列出，详见应用「运行日志」中的出错路径清单。" % (n - limit))
    return "\n".join(lines)


#: 出错清单的极端安全阀：正常不会触及，仅防止整盘读不了时一次写入天文数字的日志行。
ERR_HARD_LIMIT = 20000


def _log_error_paths(state, task_id, error_paths, title=None):
    """集中输出「出错文件路径清单」——完整列出，不截断。

    清单放在本次同步日志块的最后写入——界面「运行日志」按最新在前展示，
    因此它会出现在该次同步的最上方，不会被成百上千条变更明细淹没，
    便于一眼定位到底是哪几个路径出错、错在哪里。

    逐条完整输出（最多 ERR_HARD_LIMIT 条的病态保护），并使用 state.log_many
    一次性落盘，避免上千条日志反复开关文件。
    """
    n = len(error_paths)
    if not n:
        return
    state.log("error",
              "❗❗ %s共 %d 个路径出错，出错文件路径清单如下（全部列出，请重点排查）："
              % (title or "同步存在问题——", n), task_id)

    shown = error_paths[:ERR_HARD_LIMIT]
    entries = [("error", "  ✖ %s ｜ 原因：%s" % (_err_path(p), why), task_id)
               for p, why in shown]
    writer = getattr(state, "log_many", None)
    if callable(writer):
        writer(entries)
    else:                                    # 兼容未提供 log_many 的旧 state
        for level, msg, tid in entries:
            state.log(level, msg, tid)
    if n > ERR_HARD_LIMIT:
        state.log("error", "  …其余 %d 条出错路径未列出（超过 %d 条保护上限）"
                  % (n - ERR_HARD_LIMIT, ERR_HARD_LIMIT), task_id)
    state.log("error", "❗❗ 出错路径清单结束（共 %d 条）" % n, task_id)


def _fail(state, task, msg, error_paths=None):
    """统一的失败收尾：记录错误日志、发送失败通知（列出出错文件）、更新任务状态。"""
    state.log("error", msg, task["id"])
    if state.settings.get("notify_on_finish", True):
        body = "[%s] %s" % (task.get("name"), msg)
        body += _format_error_paths(error_paths)
        _notify(state, "USB Copy 同步失败", body)
    _finish(state, task, False, msg)


def _eject(state, device):
    mp = device.get("mountpoint")
    if not mp:
        return
    devpath = "/dev/" + (device.get("name") or "").strip()
    try:
        subprocess.run(["sync"], timeout=30)
        ok = subprocess.run(["umount", mp], capture_output=True, timeout=30).returncode == 0
        if not ok:
            # 设备忙时尝试延迟卸载
            ok = subprocess.run(["umount", "-l", mp], capture_output=True, timeout=30).returncode == 0
        # 尝试卸载/断电（部分系统没有 udisksctl / eject，失败可忽略）
        if devpath != "/dev/":
            for cmd in (["udisksctl", "power-off", "-b", devpath],
                        ["udisksctl", "unmount", "-b", devpath],
                        ["eject", devpath]):
                try:
                    subprocess.run(cmd, capture_output=True, timeout=10)
                except Exception:
                    pass
        if ok:
            state.log("info", "已安全弹出U盘: %s" % mp)
        else:
            state.log("warn", "弹出U盘失败(可能被占用): %s" % mp)
    except Exception as e:
        state.log("warn", "弹出U盘异常: %s" % e)


def _finish(state, task, ok, msg):
    with state.lock:
        task["last_run"] = int(time.time())
        task["last_status"] = "success" if ok else "failed"
        task["last_msg"] = msg
        state.save_tasks()


def run_sync(state, task, device):
    """供监控自动触发与手动「立即运行」调用。同一任务并发时直接跳过。"""
    task_id = task["id"]
    with state.lock:
        if task_id in state.running_syncs:
            return False
        state.running_syncs[task_id] = {
            "start": time.time(),
            "device": device.get("label") or device.get("name"),
        }
    try:
        _do_sync(state, task, device)
    finally:
        with state.lock:
            state.running_syncs.pop(task_id, None)
    return True


def _normalize_folders(task):
    """返回规整后的文件夹配对列表 [{usb, nas}]。"""
    out = []
    folders = task.get("folders") or []
    for f in folders:
        if isinstance(f, dict):
            usb = (f.get("usb") or "").strip().strip("/")
            nas = (f.get("nas") or "").strip().rstrip("/")
            if usb or nas:
                out.append({"usb": usb, "nas": nas})
    # 兼容旧版单文件夹字段
    if not out:
        src = (task.get("usb_source_folder") or "").strip().strip("/")
        dst = (task.get("nas_dest_folder") or "").strip().rstrip("/")
        if src or dst:
            out.append({"usb": src, "nas": dst})
    return out


def _do_sync(state, task, device):
    task_id = task["id"]
    detail = state.settings.get("log_file_detail", True)  # 是否记录文件明细
    folders = _normalize_folders(task)
    if not folders:
        _fail(state, task, "未配置任何文件夹配对")
        return

    mode = task.get("mode", "incremental")
    include = task.get("filters", {}).get("include", []) or []
    exclude = task.get("filters", {}).get("exclude", []) or []
    conflict = task.get("conflict", "overwrite")
    mp = device["mountpoint"].rstrip("/")

    ts = time.strftime("%Y%m%d-%H%M%S") if mode == "multiversion" else None

    # 统计：新增 / 更新 / 跳过 / 删除 / 错误 / 字节
    total_added = total_updated = total_skipped = total_deleted = total_errors = 0
    total_deleted_dirs = 0   # 镜像模式清理的「源中不存在」的空目录数
    total_bytes = 0
    changes = []          # 变更明细 (level, msg)，结束后集中输出到日志末尾
    error_paths = []      # 出错路径 [(path, reason)]，结束后集中输出到同步块最上方

    # ① 开始：记录完整时间戳（▶ 行，前端显示时间列）
    t0 = time.time()
    t0_str = _now()
    dev_label = device.get("label") or device.get("uuid") or device.get("name") or "-"
    state.log("info",
              "▶ 开始同步 %s ｜ 任务[%s] ｜ U盘[%s] %s ｜ 共 %d 对文件夹配对"
              % (t0_str, task.get("name"), dev_label, mp, len(folders)),
              task_id)

    # ② 同步「开始」推送（独立开关，默认关闭）
    if state.settings.get("notify_on_start", False):
        _notify(state, "USB Copy 已开始同步",
                "[%s]\n开始时间：%s\nU盘：%s（%s）\n文件夹配对：%d 对"
                % (task.get("name"), t0_str, dev_label, mp, len(folders)))

    try:
        for idx, pair in enumerate(folders, 1):
            usb_rel = pair["usb"]
            src_root = os.path.join(mp, usb_rel).rstrip("/")
            if not os.path.isdir(src_root):
                state.log("error", "源文件夹不存在: %s" % src_root, task_id)
                error_paths.append((src_root, "源文件夹不存在（U盘内未找到该目录）"))
                total_errors += 1
                continue

            if not pair["nas"]:
                state.log("error", "未配置目标文件夹（U盘:%s）" % (usb_rel or "(根)"), task_id)
                error_paths.append((os.path.join(mp, usb_rel).rstrip("/"),
                                    "未配置对应的 NAS 目标文件夹"))
                total_errors += 1
                continue
            dest_root = pair["nas"].rstrip("/")
            if mode == "multiversion":
                dest_root = os.path.join(dest_root, ts)

            try:
                os.makedirs(dest_root, exist_ok=True)
            except Exception as e:
                state.log("error", "无法创建目标文件夹 %s: %s" % (dest_root, e), task_id)
                error_paths.append((dest_root, "无法创建目标文件夹：%s" % e))
                total_errors += 1
                continue

            state.log("info", "· 配对 %d/%d：%s → %s"
                      % (idx, len(folders), src_root, dest_root), task_id)

            should_exist = set()        # 源中存在的文件（相对路径）
            should_exist_dirs = set()   # 源中存在的目录（相对路径，含空目录）
            added = updated = skipped = deleted = deleted_dirs = errors = 0
            bytes_copied = 0

            for root, dirs, files in os.walk(src_root):
                rel_root = os.path.relpath(root, src_root)
                if rel_root != ".":
                    should_exist_dirs.add(rel_root.replace(os.sep, "/"))
                dest_dir = dest_root if rel_root == "." else os.path.join(dest_root, rel_root)
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                except Exception:
                    pass
                for f in sorted(files):
                    rel = f if rel_root == "." else os.path.normpath(os.path.join(rel_root, f))
                    rel_key = rel.replace(os.sep, "/")
                    if not _should_include(rel_key, include, exclude):
                        continue
                    s = os.path.join(root, f)
                    d = os.path.join(dest_dir, f)
                    should_exist.add(rel_key)
                    try:
                        need = True
                        existed = os.path.exists(d)
                        if existed:
                            if conflict == "skip":
                                skipped += 1
                                continue
                            try:
                                ss = os.stat(s)
                                ds = os.stat(d)
                                if ss.st_size == ds.st_size and int(ss.st_mtime) <= int(ds.st_mtime):
                                    need = False
                            except Exception:
                                pass
                        if need:
                            target = d
                            renamed = False
                            if conflict == "rename" and existed:
                                target = d + ".conflict-" + time.strftime("%Y%m%d%H%M%S")
                                renamed = True
                            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                            shutil.copy2(s, target)
                            try:
                                bytes_copied += os.path.getsize(s)
                            except Exception:
                                pass
                            if existed:
                                updated += 1
                            else:
                                added += 1
                            if detail:
                                src_disp = os.path.relpath(s, mp).replace(os.sep, "/")
                                if renamed:
                                    changes.append(("warn", "冲突重命名: %s → %s" % (d, target)))
                                else:
                                    changes.append(("info", "复制: %s → %s" % (src_disp, target)))
                    except Exception as e:
                        errors += 1
                        state.log("error", "复制失败 %s: %s" % (s, e), task_id)
                        error_paths.append((s, "复制失败：%s" % e))

            if mode == "mirror":
                for root, dirs, files in os.walk(dest_root, topdown=False):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), dest_root).replace(os.sep, "/")
                        if rel not in should_exist:
                            try:
                                os.remove(os.path.join(root, f))
                                deleted += 1
                                if detail:
                                    changes.append(("info", "删除(镜像): %s" % os.path.join(root, f)))
                            except Exception:
                                pass
                for root, dirs, files in os.walk(dest_root, topdown=False):
                    if root != dest_root and not os.listdir(root):
                        # 镜像语义：与源一致的目录结构应保留。
                        # 只删除「源中不存在」的空目录，否则U盘里的空目录
                        # 会每次同步都被「创建→删除」反复折腾（日志刷屏）。
                        rel = os.path.relpath(root, dest_root).replace(os.sep, "/")
                        if rel in should_exist_dirs:
                            continue
                        try:
                            os.rmdir(root)
                            deleted_dirs += 1
                            if detail:
                                changes.append(("info", "删除空目录(镜像): %s" % root))
                        except Exception:
                            pass

            total_added += added
            total_updated += updated
            total_skipped += skipped
            total_deleted += deleted
            total_deleted_dirs += deleted_dirs
            total_errors += errors
            total_bytes += bytes_copied
            state.log("info",
                      "· 配对完成（%s→%s）：新增 %d，更新 %d，跳过 %d，删除 %d，错误 %d，共 %s%s"
                      % (usb_rel or "(根)", dest_root, added, updated, skipped,
                         deleted, errors, _human(bytes_copied),
                         ("，空目录 %d" % deleted_dirs) if deleted_dirs else ""),
                      task_id)

        ok = total_errors == 0
        msg = "同步完成：新增 %d，更新 %d，跳过 %d，删除 %d，错误 %d，共 %s%s" % (
            total_added, total_updated, total_skipped, total_deleted,
            total_errors, _human(total_bytes),
            ("，空目录 %d" % total_deleted_dirs) if total_deleted_dirs else "")

        # ③ 结束：记录完整时间戳 + 本次耗时（■ 行，前端显示时间列）
        t1_str = _now()
        dur = _dur(time.time() - t0)
        state.log("info" if ok else "warn",
                  "■ %s %s（耗时 %s）：新增 %d，更新 %d，跳过 %d，删除 %d，错误 %d，共 %s%s"
                  % ("同步完成" if ok else "同步结束（有错误）", t1_str, dur,
                     total_added, total_updated, total_skipped,
                     total_deleted, total_errors, _human(total_bytes),
                     ("，空目录 %d" % total_deleted_dirs) if total_deleted_dirs else ""),
                  task_id)

        # ④ 变更明细集中输出（缩进两格＝明细行，前端不显示时间列）
        if detail and changes:
            MAX_DETAIL = 500
            state.log("info", "  —— 变更明细（共 %d 条）：" % len(changes), task_id)
            for lvl, m in changes[:MAX_DETAIL]:
                state.log(lvl, "  " + m, task_id)
            if len(changes) > MAX_DETAIL:
                state.log("info",
                          "  …其余 %d 条变更未列出（超过 %d 条上限）"
                          % (len(changes) - MAX_DETAIL, MAX_DETAIL),
                          task_id)

        # ⑤ 出错路径清单：最后写入＝界面显示在该次同步的最上方
        if error_paths:
            _log_error_paths(state, task_id, error_paths)

        # ⑥ 同步「完成/失败」推送（独立开关，默认开启）
        #    有错误时，正文中逐条列出出错的文件路径与原因
        if state.settings.get("notify_on_finish", True):
            body = "[%s]\n%s\n开始：%s\n结束：%s（耗时 %s）" % (
                task.get("name"), msg, t0_str, t1_str, dur)
            body += _format_error_paths(error_paths)
            _notify(state,
                    "USB Copy 同步完成" if ok else "USB Copy 同步失败（存在错误）",
                    body)
        _finish(state, task, ok, msg)

        if task.get("eject_after") and ok:
            _eject(state, device)

    except Exception as e:
        error_paths.append(("（同步过程中断）", str(e)))
        _fail(state, task, "同步异常: %s" % e, error_paths)
        # 清单最后写入，保证界面倒序展示时位于该次同步最上方
        _log_error_paths(state, task_id, error_paths, title="同步异常中断——")
