"use strict";

const byId = (id) => document.getElementById(id);
const clamp = (value, min = 0, max = 100) => Math.min(max, Math.max(min, Number(value) || 0));

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}

function replaceChildren(target, children) {
  target.replaceChildren(...children);
  target.classList.remove("empty-state");
}

function setText(id, value, fallback = "—") {
  byId(id).textContent = value === undefined || value === null || value === "" ? fallback : String(value);
}

function setMeter(id, value) {
  byId(id).style.width = `${clamp(value)}%`;
}

function percent(value) {
  return value === undefined || value === null ? "—" : `${Number(value).toFixed(1)}%`;
}

function formatCollectedAt(value) {
  if (!value) return "未知时间";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date(value)).replaceAll("/", "-");
  } catch (_error) {
    return value;
  }
}

function detailRow(label, value) {
  const row = node("div", "detail-row");
  row.append(node("span", "", label), node("strong", "", value || "—"));
  return row;
}

function renderSummary(data) {
  const summary = data.summary || {};
  const system = data.system || {};
  const pill = byId("health-pill");
  pill.className = `health-pill ${summary.level === "warning" ? "warning" : "healthy"}`;
  pill.lastElementChild.textContent = summary.title || "已采集";
  setText("hero-subtitle", `${summary.message || "状态快照已更新"} · 采集于 ${formatCollectedAt(data.collected_at)}`);
  setText("system-device", `${system.device || system.hostname || "T470s"}${system.machine_type ? ` · ${system.machine_type}` : ""}`);
  setText("system-uptime", system.uptime_human);
  setText("system-load", Array.isArray(system.load) ? system.load.join(" / ") : "—");
  setText("system-processes", `${system.processes ?? "—"} / ${system.threads ?? "—"}`);
  setText("collection-duration", `本次采集耗时 ${data.collection_ms ?? "—"} ms`);
}

function renderMetrics(data) {
  const cpu = data.cpu || {};
  const memory = (data.memory || {}).ram || {};
  const temperatures = Array.isArray(data.temperatures) ? data.temperatures : [];
  const hottest = temperatures.reduce((best, item) => !best || item.celsius > best.celsius ? item : best, null);
  const disks = Array.isArray(data.disks) ? data.disks : [];
  const root = disks.find((item) => item.mountpoint === "/") || disks[0] || {};

  setText("cpu-value", percent(cpu.usage_percent));
  setText("cpu-detail", `${cpu.logical_cores ?? "—"} 线程 · 空闲 ${percent(cpu.idle_percent)}`);
  setMeter("cpu-meter", cpu.usage_percent);

  setText("temp-value", hottest ? `${hottest.celsius.toFixed(1)}°` : "—");
  setText("temp-detail", hottest ? hottest.name : "暂无温度数据");
  setMeter("temp-meter", hottest ? hottest.celsius : 0);

  setText("memory-value", percent(memory.percent));
  setText("memory-detail", `${memory.used_human || "—"} / ${memory.total_human || "—"}`);
  setMeter("memory-meter", memory.percent);

  setText("disk-value", percent(root.percent));
  setText("disk-detail", `${root.used_human || "—"} / ${root.total_human || "—"}`);
  setMeter("disk-meter", root.percent);
}

function renderBatteries(data) {
  const batteryData = data.batteries || {};
  const items = Array.isArray(batteryData.items) ? batteryData.items : [];
  const totals = batteryData.totals || {};
  setText("battery-total", `合计 ${totals.full_wh ?? "—"} / ${totals.design_wh ?? "—"} Wh · 健康 ${percent(totals.health_percent)}`);
  const cards = items.map((battery) => {
    const card = node("article", "battery-card");
    const top = node("div", "battery-top");
    const name = node("div", "battery-name");
    name.append(node("span", "battery-symbol"), node("span", "", `${battery.name} · ${battery.model || "内置电池"}`));
    top.append(name, node("strong", "battery-percent", percent(battery.percent)));

    const meta = node("div", "battery-meta");
    [["当前容量", `${battery.now_wh ?? "—"} Wh`], ["满充容量", `${battery.full_wh ?? "—"} Wh`], ["设计容量", `${battery.design_wh ?? "—"} Wh`]].forEach(([label, value]) => {
      const cell = node("div");
      cell.append(node("span", "", label), node("strong", "", value));
      meta.append(cell);
    });

    const health = node("div", "battery-health");
    const healthLabel = node("div");
    healthLabel.append(node("span", "", "健康度"), node("strong", "", percent(battery.health_percent)));
    const meter = node("div", "meter");
    const fill = node("span");
    fill.style.width = `${clamp(battery.health_percent)}%`;
    fill.style.color = battery.health_percent < 50 ? "var(--amber)" : "var(--green)";
    meter.append(fill);
    health.append(healthLabel, meter);
    card.append(top, meta, health);
    return card;
  });
  replaceChildren(byId("battery-list"), cards.length ? cards : [node("p", "empty-state", "未检测到电池")]);
}

function renderNetwork(data) {
  const network = data.network || {};
  const connectivity = network.connectivity || {};
  const tag = byId("network-state");
  tag.textContent = connectivity.online ? "外网正常" : "外网异常";
  tag.className = `status-tag ${connectivity.online ? "good" : "bad"}`;
  const rows = [];
  (network.interfaces || []).forEach((item) => rows.push(detailRow(item.name, `${(item.ipv4 || []).join(", ")} · ${item.up ? "已连接" : "未连接"}`)));
  rows.push(detailRow("默认网关", network.gateway));
  rows.push(detailRow("外网检测", connectivity.online ? `HTTP ${connectivity.status} · ${connectivity.latency_ms} ms` : "连接失败"));
  rows.push(detailRow("本地代理", network.proxy?.listening ? `${network.proxy.host}:${network.proxy.port} · 正常` : "未监听"));
  replaceChildren(byId("network-list"), rows);
}

function renderHardware(data) {
  const cpu = data.cpu || {};
  const fan = data.fan || {};
  setText("cpu-model", `${cpu.model || "未知处理器"} · ${cpu.physical_cores ?? "—"} 核 ${cpu.logical_cores ?? "—"} 线程 · ${cpu.power_profile || "unknown"}`);
  setText("fan-summary", fan.rpm !== null && fan.rpm !== undefined ? `${fan.rpm} RPM · ${fan.level}` : fan.status || "—");
  const usage = cpu.per_core_percent || [];
  const mhz = cpu.per_core_mhz || [];
  const cores = usage.map((value, index) => {
    const card = node("div", "core-card");
    card.append(node("span", "", `线程 ${index + 1}`), node("strong", "", percent(value)), node("small", "", mhz[index] ? `${mhz[index]} MHz` : "频率未知"));
    return card;
  });
  replaceChildren(byId("core-list"), cores);

  const chips = (data.temperatures || []).map((item) => {
    const chip = node("div", "temperature-chip", item.name);
    chip.append(node("strong", "", `${item.celsius.toFixed(1)}°C`));
    return chip;
  });
  replaceChildren(byId("temperature-list"), chips);
}

function renderModel(data) {
  const model = data.model || {};
  const container = byId("model-details");
  const current = node("div", "model-current");
  current.append(node("span", "", "CURRENT SESSION"), node("strong", "", model.current || "unknown"), node("small", "", model.runtime || "OpenClaw"));
  const rows = [current, detailRow("配置主模型", model.primary), detailRow("备用模型", (model.fallbacks || []).join("、") || "未配置")];
  replaceChildren(container, rows);
}

function renderDisks(data) {
  const cards = (data.disks || []).map((disk) => {
    const card = node("article", "storage-card");
    const head = node("div", "storage-head");
    head.append(node("strong", "", disk.mountpoint), node("span", "", disk.filesystem));
    const meter = node("div", "meter");
    const fill = node("span");
    fill.style.width = `${clamp(disk.percent)}%`;
    fill.style.color = disk.percent >= 90 ? "var(--red)" : "var(--cyan)";
    meter.append(fill);
    const meta = node("div", "storage-meta");
    meta.append(node("span", "", `已用 ${disk.used_human}`), node("span", "", `可用 ${disk.free_human} · ${percent(disk.percent)}`));
    card.append(head, meter, meta);
    return card;
  });
  replaceChildren(byId("disk-list"), cards);
}

function renderProcesses(id, items) {
  const rows = [];
  const header = node("div", "process-row process-header");
  header.append(node("span", "", "#"), node("span", "", "进程"), node("span", "process-value", "CPU"), node("span", "process-value", "内存"));
  rows.push(header);
  (items || []).forEach((item, index) => {
    const row = node("div", "process-row");
    row.append(
      node("span", "process-rank", String(index + 1).padStart(2, "0")),
      node("span", "process-name", `${item.name} · ${item.pid}`),
      node("span", "process-value", `${item.cpu.toFixed(1)}%`),
      node("span", "process-value", item.rss_human),
    );
    rows.push(row);
  });
  replaceChildren(byId(id), rows);
}

function renderCron(data) {
  const schedule = data.schedule && typeof data.schedule === "object" ? data.schedule : {};
  const jobs = Array.isArray(schedule.items) ? schedule.items : [];
  const summary = byId("schedule-summary");
  if (summary) {
    summary.textContent = `${jobs.length} 个用户任务 · ${schedule.system_timer_count || 0} 个系统计时器`;
  }
  const rows = jobs.map((job) => {
    const row = node("article", "schedule-item");
    const head = node("div", "schedule-item-head");
    head.append(node("strong", "schedule-name", job.name || "未命名任务"), node("span", "schedule-source", job.source || "任务"));
    const meta = node("div", "schedule-meta");
    meta.append(node("span", "schedule-expression", job.schedule || "自动调度"));
    if (job.next_run) {
      meta.append(node("span", "schedule-next", `下次 ${formatCollectedAt(job.next_run)}`));
    } else {
      meta.append(node("span", "schedule-next", job.status === "disabled" ? "已停用" : "已启用"));
    }
    row.append(head, meta);
    return row;
  });
  replaceChildren(byId("cron-list"), rows.length ? rows : [node("p", "empty-state", "当前没有用户级定时任务")]);
}

function renderSnapshot(data) {
  if (!data) return;
  renderSummary(data);
  renderMetrics(data);
  renderBatteries(data);
  renderNetwork(data);
  renderHardware(data);
  renderModel(data);
  renderDisks(data);
  renderProcesses("cpu-process-list", data.processes?.cpu || []);
  renderProcesses("memory-process-list", data.processes?.memory || []);
  renderCron(data);
}

function showBanner(kind, title, detail) {
  const banner = byId("refresh-banner");
  banner.className = `refresh-banner ${kind || ""}`.trim();
  setText("refresh-title", title);
  setText("refresh-detail", detail);
}

async function refreshOnce() {
  const button = byId("refresh-button");
  button.disabled = true;
  button.textContent = "采集中…";
  try {
    const response = await fetch("/api/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-Requested-With": "status-dashboard" },
    });
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    const payload = await response.json();
    if (!response.ok || !payload.snapshot) throw new Error(payload.error || "refresh_failed");
    renderSnapshot(payload.snapshot);
    if (payload.source === "stale-fallback") {
      showBanner("warning", "最新采集失败", payload.warning || "继续显示上次快照，请稍后刷新重试。");
    } else if (payload.source === "recent-cache") {
      showBanner("complete", "已显示刚刚采集的快照", "10 秒内重复刷新已复用缓存，避免重复运行系统检查。");
    } else {
      showBanner("complete", "状态采集完成", `快照时间：${formatCollectedAt(payload.snapshot.collected_at)}。页面不会在后台自动刷新。`);
    }
  } catch (_error) {
    showBanner("warning", "暂时无法获取最新状态", "已保留上次快照；请检查网络后刷新页面重试。");
  } finally {
    button.disabled = false;
    button.textContent = "刷新状态";
  }
}

byId("refresh-button").addEventListener("click", () => window.location.reload());

try {
  const initial = JSON.parse(byId("initial-snapshot").textContent || "null");
  if (initial) {
    renderSnapshot(initial);
    showBanner("", "正在获取最新状态", `当前先显示 ${formatCollectedAt(initial.collected_at)} 的缓存快照。`);
  }
} catch (_error) {
  // A missing/corrupt cache is expected on first run; the refresh below fills it.
}

refreshOnce();
