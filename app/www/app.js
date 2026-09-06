"use strict";

const api = {
  get: (p) => fetch(p).then(r => r.json()),
  post: (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }).then(r => r.json()),
  put: (p, b) => fetch(p, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }).then(r => r.json()),
  del: (p) => fetch(p, { method: "DELETE" }).then(r => r.json()),
};

let editingId = null;
let deviceCache = [];          // 当前已插入设备
let taskFolders = [];          // [{usb, nas}] 配对数组
let pickerState = null;        // {scope, uuid, mp, cur, parent, onPick}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

function fmtTime(ts) {
  if (!ts) return "从未";
  const d = new Date(ts * 1000);
  const p = (x) => (x < 10 ? "0" + x : x);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 日志行是否隐藏时间列。
   与后端约定：▶/■ 开头＝开始/结束里程碑（带完整时间，显示时间列）；
   · 或空格开头＝过程明细、变更明细、出错路径清单子行（不逐条重复时间）。 */
function hideTs(msg) {
  const c = String(msg == null ? "" : msg).charAt(0);
  return c === "·" || c === " ";
}

/* 渲染一条日志行。full=true 时额外显示任务ID。 */
function renderLogLine(l, full) {
  const line = document.createElement("div");
  line.className = "log-line " + (l.level || "info");
  const m0 = String(l.msg || "").charAt(0);
  if (m0 === "▶") line.classList.add("start-line");
  else if (m0 === "■") line.classList.add("end-line");
  else if (String(l.msg || "").indexOf("❗❗") === 0) line.classList.add("err-title");
  const lv = (l.level || "INFO").toUpperCase();
  const tsHtml = hideTs(l.msg)
    ? '<span class="ts blank"></span>'
    : `<span class="ts">${fmtTime(l.ts)}</span>`;
  const tid = (full && l.task_id) ? ` <span class="tid">[${esc(l.task_id)}]</span>` : "";
  line.innerHTML = `${tsHtml}<span class="lv">[${lv}]</span>${tid} ${esc(l.msg)}`;
  return line;
}

function usbRel(mp, abs) {
  if (!abs || abs === mp) return "";
  return abs.slice(mp.length).replace(/^\//, "");
}

function deviceLabel(d) {
  return d.label || d.uuid || d.name || "(未知U盘)";
}

/* 设备唯一键：优先 UUID，无 UUID 时用 "name:设备名"（如 name:sdb1） */
function devKey(d) {
  return d.uuid || ("name:" + (d.name || ""));
}
function findDevice(key) {
  return deviceCache.find(d => devKey(d) === key) || null;
}

/* ---------- 状态栏 ---------- */
async function refreshStatus() {
  try {
    const s = await api.get("/api/status");
    document.getElementById("statusdot").className = "dot on";
    document.getElementById("status-text").textContent = "服务运行中 v" + (s.version || "");
    document.getElementById("device-count").textContent = "已连接U盘 " + (s.devices || []).length;
    // 实时刷新设备缓存与设备列表（弹出/拔出U盘后自动更新）
    deviceCache = s.devices || [];
    if (document.getElementById("tab-devices").classList.contains("active")) {
      renderDevices(deviceCache);
    }
  } catch (e) {
    document.getElementById("statusdot").className = "dot off";
    document.getElementById("status-text").textContent = "无法连接服务";
  }
}

/* ---------- 任务列表 ---------- */
function modeLabel(m) {
  return { mirror: "镜像", incremental: "增量", multiversion: "多版本" }[m] || m;
}

function renderTaskPath(t) {
  const folders = (t.folders && t.folders.length) ? t.folders : null;
  if (folders) {
    if (folders.length === 1) {
      const f = folders[0];
      return `U盘[${esc((t.usb_match && t.usb_match.value) || "任意")}] / ${esc(f.usb || "(根)")}  →  ${esc(f.nas || "(未设置)")}`;
    }
    return `U盘[${esc((t.usb_match && t.usb_match.value) || "任意")}] · 共 ${folders.length} 对文件夹配对`;
  }
  return `U盘[${esc((t.usb_match && t.usb_match.value) || "任意")}] / ${esc(t.usb_source_folder || "(根目录)")}  →  ${esc(t.nas_dest_folder)}`;
}

function renderTasks(tasks) {
  const box = document.getElementById("task-list");
  const empty = document.getElementById("task-empty");
  box.innerHTML = "";
  if (!tasks.length) { empty.style.display = "block"; return; }
  empty.style.display = "none";

  tasks.forEach(t => {
    const card = document.createElement("div");
    card.className = "task-card";
    const enabled = t.enabled !== false;
    const statusCls = !enabled ? "disabled" : (t.last_status === "failed" ? "fail" : (t.last_status === "success" ? "ok" : ""));
    const statusTxt = !enabled ? "已停用" : (t.last_status === "failed" ? "上次失败" : (t.last_status === "success" ? "上次成功" : "未运行"));
    card.innerHTML = `
      <div class="task-head">
        <span class="task-name">${esc(t.name)}</span>
        <span class="badge ${t.mode}">${modeLabel(t.mode)}</span>
        <span class="badge ${statusCls}">${statusTxt}</span>
        <div class="task-actions">
          <button class="btn" data-act="run" data-id="${t.id}">立即运行</button>
          <button class="btn" data-act="edit" data-id="${t.id}">编辑</button>
          <button class="btn danger" data-act="del" data-id="${t.id}">删除</button>
        </div>
      </div>
      <div class="task-path">${renderTaskPath(t)}</div>
      <div class="task-meta">
        <span>过滤: ${t.filters && t.filters.include.length ? esc(t.filters.include.join(",")) : "全部"}</span>
        <span>排除: ${t.filters && t.filters.exclude.length ? esc(t.filters.exclude.join(",")) : "无"}</span>
        <span>冲突: ${esc({ overwrite: "覆盖", skip: "跳过", rename: "重命名" }[t.conflict] || t.conflict)}</span>
        <span>${t.eject_after ? "完成后弹出" : "不弹出"}</span>
        <span>上次运行: ${fmtTime(t.last_run)}</span>
      </div>`;
    box.appendChild(card);
  });

  box.querySelectorAll("button[data-act]").forEach(b => {
    b.onclick = () => {
      const id = b.dataset.id, act = b.dataset.act;
      if (act === "run") runTask(id);
      else if (act === "edit") openModal(id);
      else if (act === "del") delTask(id);
    };
  });
}

async function loadTasks() {
  const r = await api.get("/api/tasks");
  if (r.ok) renderTasks(r.tasks || []);
}

async function runTask(id) {
  const r = await api.post("/api/tasks/" + id + "/run", {});
  toast(r.msg || (r.ok ? "已触发" : "失败"));
  if (r.ok) setTimeout(refreshStatus, 500);
}

async function delTask(id) {
  if (!confirm("确定删除该同步任务？")) return;
  const r = await api.del("/api/tasks/" + id);
  if (r.ok) { toast("已删除"); loadTasks(); }
}

/* ---------- 设备 ---------- */
async function loadDevices() {
  const r = await api.get("/api/devices");
  if (r.ok) {
    deviceCache = r.devices || [];
    renderDevices(deviceCache);
  }
}

function renderDevices(devices) {
  const box = document.getElementById("device-list");
  const empty = document.getElementById("device-empty");
  box.innerHTML = "";
  if (!devices.length) { empty.style.display = "block"; return; }
  empty.style.display = "none";
  devices.forEach(d => {
    const card = document.createElement("div");
    card.className = "device-card";
    card.innerHTML = `
      <div class="row"><span class="k">设备</span><span class="v">/dev/${esc(d.name)}</span></div>
      <div class="row"><span class="k">UUID</span><span class="v">${esc(d.uuid || "-")}</span></div>
      <div class="row"><span class="k">卷标</span><span class="v">${esc(d.label || "-")}</span></div>
      <div class="row"><span class="k">序列号</span><span class="v">${esc(d.serial || "-")}</span></div>
      <div class="row"><span class="k">型号</span><span class="v">${esc(d.model || "-")}</span></div>
      <div class="row"><span class="k">挂载点</span><span class="v">${esc(d.mountpoint)}</span></div>
      <div class="row"><span class="k">容量</span><span class="v">${fmtSize(d.size)} (${esc(d.fstype)})</span></div>
      <div class="act"><button class="btn primary" data-use="${esc(devKey(d))}">用此U盘新建任务</button></div>`;
    box.appendChild(card);
  });
  box.querySelectorAll("button[data-use]").forEach(b => {
    b.onclick = () => openModalWithDevice(b.dataset.use);
  });
}

function populateDeviceSelect() {
  const sel = document.getElementById("f-device");
  const prev = sel.value;
  sel.innerHTML = '<option value="">— 任意U盘 —</option>';
  deviceCache.forEach(d => {
    const o = document.createElement("option");
    o.value = devKey(d);
    o.textContent = `${deviceLabel(d)}（${d.uuid || d.name}）`;
    sel.appendChild(o);
  });
  if (prev) sel.value = prev;
}

function currentDevice() {
  const key = document.getElementById("f-device").value;
  if (!key) return null;
  return findDevice(key);
}

/* ---------- 文件夹配对 ---------- */
function renderFolderPairs() {
  const box = document.getElementById("folder-pairs");
  box.innerHTML = "";
  if (!taskFolders.length) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "尚未添加配对，点击下方「添加文件夹配对」。";
    box.appendChild(hint);
    return;
  }
  taskFolders.forEach((pair, idx) => {
    const row = document.createElement("div");
    row.className = "pair-row";
    row.innerHTML = `
      <div class="pair-col">
        <div class="pair-label">U盘文件夹</div>
        <div class="pair-input">
          <input type="text" data-side="usb" data-idx="${idx}" value="${esc(pair.usb)}" placeholder="(根目录)">
          <button class="btn" data-browse="usb" data-idx="${idx}" type="button">浏览</button>
        </div>
      </div>
      <div class="pair-arrow">→</div>
      <div class="pair-col">
        <div class="pair-label">NAS 文件夹</div>
        <div class="pair-input">
          <input type="text" data-side="nas" data-idx="${idx}" value="${esc(pair.nas)}" placeholder="/vol1/1000/...">
          <button class="btn" data-browse="nas" data-idx="${idx}" type="button">浏览</button>
        </div>
      </div>
      <button class="btn danger pair-del" data-del="${idx}" type="button">×</button>`;
    box.appendChild(row);
  });

  box.querySelectorAll("input[data-side]").forEach(inp => {
    inp.oninput = () => {
      const i = Number(inp.dataset.idx), side = inp.dataset.side;
      taskFolders[i][side] = inp.value.trim();
    };
  });
  box.querySelectorAll("button[data-browse]").forEach(b => {
    b.onclick = () => {
      const i = Number(b.dataset.idx), side = b.dataset.browse;
      openPicker(side, i);
    };
  });
  box.querySelectorAll("button[data-del]").forEach(b => {
    b.onclick = () => {
      const i = Number(b.dataset.del);
      taskFolders.splice(i, 1);
      renderFolderPairs();
    };
  });
}

/* ---------- 文件夹选择器（二级弹窗） ---------- */
async function loadPicker(base) {
  const st = pickerState;
  let url;
  if (st.scope === "usb") {
    const rel = usbRel(st.mp, base);
    url = `/api/browse?scope=usb&uuid=${encodeURIComponent(st.uuid)}&path=${encodeURIComponent(rel)}`;
  } else {
    url = `/api/browse?scope=nas&path=${encodeURIComponent(base)}`;
  }
  const r = await api.get(url);
  if (!r.ok) { toast(r.msg || "无法浏览目录"); closePicker(); return; }
  st.cur = r.base;
  st.parent = r.parent;
  document.getElementById("picker-path").textContent = r.base || "/";
  document.getElementById("picker-up").disabled = !r.parent;
  document.getElementById("picker-ok").disabled = !!r.homes;
  document.getElementById("picker-title").textContent =
    (st.scope === "usb" ? "选择 U 盘文件夹" : "选择 NAS 文件夹") +
    (st.label ? " · " + st.label : "");

  const list = document.getElementById("picker-list");
  list.innerHTML = "";
  if (!r.dirs.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "该目录下没有子文件夹。";
    list.appendChild(empty);
  }
  r.dirs.forEach(d => {
    const item = document.createElement("div");
    item.className = "picker-item";
    item.innerHTML = `<span class="pi-name">📁 ${esc(d.name)}</span><span class="pi-go">进入 ›</span>`;
    item.onclick = () => loadPicker(d.path);
    list.appendChild(item);
  });
}

function openPicker(side, idx) {
  if (side === "usb") {
    const dev = currentDevice();
    if (!dev) { toast("请先在上方选择要使用的U盘"); return; }
    pickerState = {
      scope: "usb", uuid: dev.uuid, mp: dev.mountpoint,
      label: deviceLabel(dev), cur: dev.mountpoint, parent: "",
      onPick: (path) => { taskFolders[idx].usb = usbRel(dev.mountpoint, path); renderFolderPairs(); },
    };
  } else {
    pickerState = {
      scope: "nas", uuid: "", mp: "", label: "NAS", cur: "/", parent: "",
      onPick: (path) => { taskFolders[idx].nas = path; renderFolderPairs(); },
    };
  }
  // 打开前清空上一次内容，避免短暂显示/残留上一侧的目录
  document.getElementById("picker-path").textContent = "…";
  document.getElementById("picker-list").innerHTML = "";
  document.getElementById("picker-mask").classList.add("open");
  loadPicker(pickerState.cur);
}

function closePicker() {
  document.getElementById("picker-mask").classList.remove("open");
  pickerState = null;
}

/* ---------- 弹窗 ---------- */
function openModal(id) {
  editingId = id || null;
  window.__editingTask = null;
  document.getElementById("modal-title").textContent = id ? "编辑任务" : "新建任务";
  populateDeviceSelect();
  if (id) {
    api.get("/api/tasks").then(r => {
      const t = (r.tasks || []).find(x => x.id === id);
      if (t) { window.__editingTask = t; fillForm(t); }
    });
  } else {
    fillForm(null);
  }
  document.getElementById("modal-mask").classList.add("open");
}

function openModalWithDevice(key) {
  editingId = null;
  window.__editingTask = null;
  document.getElementById("modal-title").textContent = "新建任务";
  populateDeviceSelect();
  const dev = findDevice(key);
  const t = { usb_match: { type: "uuid", value: dev ? dev.uuid : "" }, folders: [] };
  fillForm(t);
  if (dev) document.getElementById("f-device").value = devKey(dev);
  document.getElementById("modal-mask").classList.add("open");
}

function fillForm(t) {
  // 设备与识别方式回填
  document.getElementById("f-match-field").value = "uuid";
  document.getElementById("f-device").value = "";
  if (t && t.usb_match && t.usb_match.type !== "any" && t.usb_match.value) {
    const field = t.usb_match.type;
    const dev = deviceCache.find(d => d[field] === t.usb_match.value);
    if (dev) {
      document.getElementById("f-device").value = devKey(dev);
      document.getElementById("f-match-field").value = field;
    }
  }

  // 文件夹配对回填
  taskFolders = [];
  if (t && t.folders && t.folders.length) {
    t.folders.forEach(f => taskFolders.push({ usb: f.usb || "", nas: f.nas || "" }));
  } else if (t && (t.usb_source_folder || t.nas_dest_folder)) {
    taskFolders.push({ usb: t.usb_source_folder || "", nas: t.nas_dest_folder || "" });
  }
  if (!taskFolders.length) taskFolders.push({ usb: "", nas: "" });
  renderFolderPairs();

  document.getElementById("f-name").value = (t && t.name) || "";
  document.getElementById("f-mode").value = (t && t.mode) || "incremental";
  document.getElementById("f-include").value = (t && t.filters && t.filters.include || []).join(",");
  document.getElementById("f-exclude").value = (t && t.filters && t.filters.exclude || []).join(",");
  document.getElementById("f-conflict").value = (t && t.conflict) || "overwrite";
  document.getElementById("f-eject").checked = !!(t && t.eject_after);
  document.getElementById("f-enabled").checked = !(t && t.enabled === false);
}

function closeModal() { document.getElementById("modal-mask").classList.remove("open"); }

function saveModal() {
  const dev = currentDevice();
  let usb_match;
  if (dev) {
    const field = document.getElementById("f-match-field").value;
    usb_match = { type: field, value: (dev[field] || "").trim() };
  } else if (editingId) {
    // 编辑时若当前未插入该U盘，保留原有识别方式以免误改
    const ex = (window.__editingTask && window.__editingTask.usb_match) || { type: "any", value: "" };
    usb_match = ex;
  } else {
    usb_match = { type: "any", value: "" };
  }

  const folders = taskFolders
    .map(f => ({ usb: (f.usb || "").trim().replace(/^\/+/, ""), nas: (f.nas || "").trim() }))
    .filter(f => f.usb || f.nas);

  const body = {
    name: document.getElementById("f-name").value.trim() || "未命名任务",
    usb_match: usb_match,
    folders: folders,
    mode: document.getElementById("f-mode").value,
    filters: {
      include: document.getElementById("f-include").value,
      exclude: document.getElementById("f-exclude").value,
    },
    conflict: document.getElementById("f-conflict").value,
    eject_after: document.getElementById("f-eject").checked,
    enabled: document.getElementById("f-enabled").checked,
  };
  const req = editingId ? api.put("/api/tasks/" + editingId, body) : api.post("/api/tasks", body);
  req.then(r => {
    if (r.ok) { toast("已保存"); closeModal(); loadTasks(); }
    else toast("保存失败：" + (r.msg || ""));
  });
}

/* ---------- 设置 ---------- */
async function loadSettings() {
  const r = await api.get("/api/settings");
  if (!r.ok) return;
  const s = r.settings || {};
  window.__settings = s;
  document.getElementById("set-poll").value = s.poll_interval || 5;
  document.getElementById("set-retention").value = s.log_retention_days || 30;
  document.getElementById("set-log-detail").value = s.log_file_detail === false ? "false" : "true";

  // 手机推送
  document.getElementById("set-push-enabled").checked = !!s.push_enabled;
  document.getElementById("set-notify-start").checked = !!s.notify_on_start;
  // 完成时推送默认开启（后端默认 True；旧配置无此字段时同样按开启处理）
  document.getElementById("set-notify-finish").checked = s.notify_on_finish !== false;
  const chs = Array.isArray(s.push_channels) ? s.push_channels : String(s.push_channels || "").split(",");
  document.querySelectorAll(".push-ch").forEach(c => {
    c.checked = chs.indexOf(c.value) !== -1;
  });
  document.getElementById("set-pushplus-token").value = s.pushplus_token || "";
  document.getElementById("set-smtp-host").value = s.smtp_host || "";
  document.getElementById("set-smtp-port").value = s.smtp_port || 465;
  document.getElementById("set-smtp-user").value = s.smtp_user || "";
  document.getElementById("set-smtp-pass").value = s.smtp_pass || "";
  document.getElementById("set-smtp-to").value = s.smtp_to || "";
}

function collectPushSettings() {
  const chs = [];
  document.querySelectorAll(".push-ch:checked").forEach(c => chs.push(c.value));
  return {
    push_enabled: document.getElementById("set-push-enabled").checked,
    notify_on_start: document.getElementById("set-notify-start").checked,
    notify_on_finish: document.getElementById("set-notify-finish").checked,
    push_channels: chs.join(","),
    pushplus_token: document.getElementById("set-pushplus-token").value.trim(),
    smtp_host: document.getElementById("set-smtp-host").value.trim(),
    smtp_port: Number(document.getElementById("set-smtp-port").value) || 465,
    smtp_user: document.getElementById("set-smtp-user").value.trim(),
    smtp_pass: document.getElementById("set-smtp-pass").value,
    smtp_to: document.getElementById("set-smtp-to").value.trim(),
  };
}

function saveSettings() {
  const body = {
    poll_interval: Number(document.getElementById("set-poll").value) || 5,
    log_retention_days: Number(document.getElementById("set-retention").value) || 30,
    log_file_detail: document.getElementById("set-log-detail").value === "true",
  };
  Object.assign(body, collectPushSettings());
  api.post("/api/settings", body).then(r => {
    if (r.ok) { window.__settings = r.settings || window.__settings; toast("设置已保存"); }
    else toast("保存失败：" + (r.msg || ""));
  });
}

function testPush() {
  const body = collectPushSettings();
  const box = document.getElementById("push-test-result");
  box.textContent = "正在发送测试推送…";
  api.post("/api/test_push", body).then(r => {
    if (!r.ok) { box.textContent = "请求失败：" + (r.msg || ""); return; }
    const rs = r.results || [];
    if (!rs.length) { box.textContent = "未选择任何推送渠道，或未启用手机推送。"; return; }
    box.innerHTML = rs.map(x => {
      const ok = x.result === "ok";
      const label = {pushplus: "PushPlus", smtp: "邮件"}[x.channel] || x.channel;
      return `<div>${ok ? "✅" : "❌"} ${label}：${esc(x.result)}</div>`;
    }).join("");
  }).catch(e => { box.textContent = "请求异常：" + e; });
}

/* ---------- 绑定 ---------- */
function bind() {
  document.querySelectorAll(".tab").forEach(t => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById("tab-" + t.dataset.tab).classList.add("active");
      if (t.dataset.tab === "devices") loadDevices();
      if (t.dataset.tab === "logs") loadLogs();
      if (t.dataset.tab === "settings") loadSettings();
    };
  });
  document.getElementById("new-task-btn").onclick = () => openModal(null);
  document.getElementById("modal-close").onclick = closeModal;
  document.getElementById("modal-cancel").onclick = closeModal;
  document.getElementById("modal-save").onclick = saveModal;
  document.getElementById("modal-mask").onclick = (e) => { if (e.target.id === "modal-mask") closeModal(); };

  document.getElementById("add-pair").onclick = () => {
    taskFolders.push({ usb: "", nas: "" });
    renderFolderPairs();
  };

  // 选择器
  document.getElementById("picker-close").onclick = closePicker;
  document.getElementById("picker-cancel").onclick = closePicker;
  document.getElementById("picker-mask").onclick = (e) => { if (e.target.id === "picker-mask") closePicker(); };
  document.getElementById("picker-up").onclick = () => {
    if (pickerState && pickerState.parent) loadPicker(pickerState.parent);
  };
  document.getElementById("picker-ok").onclick = () => {
    if (!pickerState) return;
    pickerState.onPick(pickerState.cur);
    closePicker();
  };

  document.getElementById("refresh-logs").onclick = loadLogs;
  document.getElementById("view-full-log").onclick = openFullLog;
  document.getElementById("full-log-close").onclick = closeFullLog;
  document.getElementById("full-log-download").onclick = downloadFullLog;
  document.getElementById("full-log-mask").onclick = (e) => { if (e.target.id === "full-log-mask") closeFullLog(); };
  document.getElementById("save-settings").onclick = saveSettings;
  document.getElementById("test-push").onclick = testPush;
}

/* ---------- 日志 ---------- */
async function loadLogs() {
  const r = await api.get("/api/logs");
  const box = document.getElementById("log-list");
  box.innerHTML = "";
  const frag = document.createDocumentFragment();
  (r.logs || []).slice().reverse().forEach(l => frag.appendChild(renderLogLine(l, false)));
  box.appendChild(frag);
}

/* ---------- 完整日志 ---------- */
async function openFullLog() {
  const mask = document.getElementById("full-log-mask");
  mask.classList.add("open");
  const info = document.getElementById("full-log-info");
  const box = document.getElementById("full-log-content");
  info.textContent = "加载中…";
  box.innerHTML = "";
  const r = await api.get("/api/logs/full?limit=20000");
  if (!r.ok) { info.textContent = "加载失败：" + (r.msg || ""); return; }
  const logs = r.logs || [];
  info.textContent = "已显示最近 " + logs.length + " 条"
    + (logs.length >= 20000 ? "（已达显示上限，可下载完整日志文件查看全部）"
                            : "（日志文件中的全部记录，最新在底部）");
  const frag = document.createDocumentFragment();
  logs.forEach(l => frag.appendChild(renderLogLine(l, true)));
  box.appendChild(frag);
  box.scrollTop = box.scrollHeight;   // 滚到底部（最新）
}

function closeFullLog() {
  document.getElementById("full-log-mask").classList.remove("open");
}

function downloadFullLog() {
  const a = document.createElement("a");
  a.href = "/api/logs/full?raw=1";
  a.download = "usbcopy.log";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

bind();
refreshStatus();
loadTasks();
loadDevices();
setInterval(refreshStatus, 5000);
