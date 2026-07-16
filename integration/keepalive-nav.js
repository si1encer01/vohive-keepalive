(function () {
  "use strict";

  const MODULE_ID = "vohive-keepalive-root";
  const API_BASE = "/keepalive-api";
  let active = new URL(location.href).searchParams.get("module") === "keepalive";
  let refreshTimer = null;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const formatTime = (value) => value ? new Date(value).toLocaleString() : "-";
  const formatBytes = (value) => {
    if (value === null || value === undefined) return "-";
    const n = Number(value);
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(2)} KiB`;
    return `${(n / 1024 / 1024).toFixed(2)} MiB`;
  };

  async function api(path, options) {
    const response = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      ...(options || {}),
    });
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.error || `请求失败（HTTP ${response.status}）`);
    return body;
  }

  function installStyle() {
    if (document.getElementById("vohive-keepalive-style")) return;
    const style = document.createElement("style");
    style.id = "vohive-keepalive-style";
    style.textContent = `
      html.vohive-keepalive-mode .main-inner > *:not(#${MODULE_ID}) { display:none !important; }
      #${MODULE_ID} { width:100%; color:#111827; }
      html.dark #${MODULE_ID} { color:#f3f4f6; }
      .vk-page { display:flex; flex-direction:column; gap:16px; }
      .vk-page-header { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
      .vk-page-title { margin:0; font-size:30px; line-height:1.2; font-weight:800; letter-spacing:-.02em; }
      .vk-page-subtitle { margin:6px 0 0; color:#6b7280; }
      html.dark .vk-page-subtitle { color:#9ca3af; }
      .vk-button { border:0; border-radius:9px; min-height:36px; padding:8px 15px; font-weight:700; cursor:pointer; color:white; background:#4f46e5; box-shadow:0 4px 14px rgba(79,70,229,.18); }
      .vk-button:hover { filter:brightness(1.07); }
      .vk-button:disabled { opacity:.5; cursor:not-allowed; }
      .vk-button-secondary { color:#374151; background:white; border:1px solid #d1d5db; box-shadow:none; }
      html.dark .vk-button-secondary { color:#e5e7eb; background:#1f2028; border-color:#3f414c; }
      .vk-button-danger { background:#dc2626; }
      .vk-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }
      .vk-card { background:rgba(255,255,255,.96); border:1px solid #e5e7eb; border-radius:14px; padding:18px; box-shadow:0 8px 24px rgba(15,23,42,.04); }
      html.dark .vk-card { background:rgba(24,24,29,.96); border-color:rgba(255,255,255,.09); box-shadow:0 12px 36px rgba(0,0,0,.18); }
      .vk-metric-label { color:#6b7280; font-size:13px; }
      html.dark .vk-metric-label { color:#9ca3af; }
      .vk-metric-value { display:block; margin-top:7px; font-size:18px; font-weight:800; overflow-wrap:anywhere; }
      .vk-good { color:#059669; } html.dark .vk-good { color:#34d399; }
      .vk-bad { color:#dc2626; } html.dark .vk-bad { color:#fb7185; }
      .vk-section-title { margin:0 0 16px; font-size:18px; font-weight:800; }
      .vk-form { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }
      .vk-field label { display:block; margin:0 0 6px; color:#6b7280; font-size:13px; }
      html.dark .vk-field label { color:#9ca3af; }
      .vk-field input,.vk-field select { width:100%; height:38px; padding:0 11px; border:1px solid #d1d5db; border-radius:8px; background:white; color:#111827; outline:none; }
      .vk-field input:focus,.vk-field select:focus { border-color:#6366f1; box-shadow:0 0 0 3px rgba(99,102,241,.12); }
      html.dark .vk-field input,html.dark .vk-field select { background:#111216; color:#f3f4f6; border-color:#3f414c; }
      .vk-check { min-height:38px; display:flex; align-items:center; gap:9px; padding-top:22px; }
      .vk-check input { width:16px; height:16px; accent-color:#4f46e5; }
      .vk-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:17px; }
      .vk-note { margin-top:12px; color:#6b7280; font-size:12px; }
      html.dark .vk-note { color:#9ca3af; }
      .vk-table-wrap { overflow:auto; }
      .vk-table { width:100%; min-width:850px; border-collapse:collapse; }
      .vk-table th,.vk-table td { padding:11px 9px; border-bottom:1px solid #e5e7eb; text-align:left; vertical-align:top; }
      html.dark .vk-table th,html.dark .vk-table td { border-color:rgba(255,255,255,.08); }
      .vk-table th { color:#6b7280; font-size:12px; font-weight:700; white-space:nowrap; }
      html.dark .vk-table th { color:#9ca3af; }
      .vk-table td { font-size:13px; }
      .vk-empty,.vk-loading { padding:36px; text-align:center; color:#6b7280; }
      .vk-menu-icon { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.9; stroke-linecap:round; stroke-linejoin:round; }
      @media(max-width:1000px){.vk-metrics{grid-template-columns:repeat(2,1fr)}.vk-form{grid-template-columns:repeat(2,1fr)}}
      @media(max-width:640px){.vk-metrics,.vk-form{grid-template-columns:1fr}.vk-page-title{font-size:25px}.vk-check{padding-top:0}}
    `;
    document.head.appendChild(style);
  }

  function menuIcon() {
    return `<svg class="vk-menu-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.9-4M4 13a8 8 0 0 0 14.9 4"/><path d="M5 3v4h4M19 21v-4h-4"/><path d="M12 8v4l2.5 1.5"/></svg>`;
  }

  function installMenus() {
    document.querySelectorAll(".sidebar-menu").forEach((menu) => {
      if (menu.querySelector("[data-vohive-keepalive-nav]")) return;
      const item = document.createElement("li");
      item.className = "el-menu-item";
      item.tabIndex = -1;
      item.setAttribute("role", "menuitem");
      item.setAttribute("data-vohive-keepalive-nav", "1");
      item.innerHTML = `<div class="el-icon">${menuIcon()}</div><span class="sidebar-menu-label">保号</span>`;
      item.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        activate(true);
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      menu.appendChild(item);
    });
    updateMenuState();
  }

  function updateMenuState() {
    document.querySelectorAll("[data-vohive-keepalive-nav]").forEach((item) => item.classList.toggle("is-active", active));
  }

  function updateUrl(on, push) {
    const url = new URL(location.href);
    if (on) url.searchParams.set("module", "keepalive");
    else url.searchParams.delete("module");
    history[push ? "pushState" : "replaceState"]({ ...(history.state || {}), vohiveKeepalive: on }, "", url);
  }

  function rootElement() {
    let root = document.getElementById(MODULE_ID);
    const main = document.querySelector(".main-inner");
    if (!main) return null;
    if (!root) {
      root = document.createElement("div");
      root.id = MODULE_ID;
      root.innerHTML = '<div class="vk-card vk-loading">正在加载保号模块…</div>';
      main.appendChild(root);
    } else if (root.parentElement !== main) {
      main.appendChild(root);
    }
    return root;
  }

  function activate(push) {
    active = true;
    installStyle();
    document.documentElement.classList.add("vohive-keepalive-mode");
    updateUrl(true, Boolean(push));
    installMenus();
    const root = rootElement();
    if (root) root.hidden = false;
    loadAll();
    if (!refreshTimer) refreshTimer = setInterval(() => { if (active) loadStatusOnly(); }, 10000);
  }

  function deactivate(updateHistory) {
    if (!active) return;
    active = false;
    document.documentElement.classList.remove("vohive-keepalive-mode");
    const root = document.getElementById(MODULE_ID);
    if (root) root.hidden = true;
    updateMenuState();
    if (updateHistory) updateUrl(false, false);
  }

  function renderShell(root) {
    if (root.querySelector(".vk-page")) return;
    root.innerHTML = `
      <div class="vk-page">
        <div class="vk-page-header"><div><h1 class="vk-page-title">保号</h1><p class="vk-page-subtitle">定时启用蜂窝数据，记录实际流量与成功时间</p></div><button class="vk-button vk-button-secondary" data-vk-refresh>刷新</button></div>
        <div class="vk-metrics">
          <div class="vk-card"><span class="vk-metric-label">服务状态</span><strong class="vk-metric-value" data-vk-state>-</strong></div>
          <div class="vk-card"><span class="vk-metric-label">下次执行</span><strong class="vk-metric-value" data-vk-next>-</strong></div>
          <div class="vk-card"><span class="vk-metric-label">上次成功</span><strong class="vk-metric-value" data-vk-last>-</strong></div>
          <div class="vk-card"><span class="vk-metric-label">上次流量</span><strong class="vk-metric-value" data-vk-bytes>-</strong></div>
        </div>
        <section class="vk-card"><h2 class="vk-section-title">策略配置</h2>
          <div class="vk-form">
            <div class="vk-field"><label>设备 ID</label><input data-vk-field="device_id"></div>
            <div class="vk-field"><label>蜂窝网卡</label><input data-vk-field="interface"></div>
            <div class="vk-field"><label>执行间隔（天，最大179）</label><input type="number" min="1" max="179" data-vk-field="interval_days"></div>
            <div class="vk-field"><label>验证网址</label><input data-vk-field="target_url"></div>
            <div class="vk-field"><label>等待数据连接（秒）</label><input type="number" data-vk-field="network_connect_timeout_seconds"></div>
            <div class="vk-field"><label>请求超时（秒）</label><input type="number" data-vk-field="request_timeout_seconds"></div>
            <div class="vk-field"><label>单次最长时间（秒）</label><input type="number" data-vk-field="max_session_seconds"></div>
            <div class="vk-field"><label>单次流量上限（KiB）</label><input type="number" data-vk-field="max_session_kib"></div>
            <div class="vk-field"><label>失败后重试（小时）</label><input type="number" data-vk-field="failure_retry_hours"></div>
            <div class="vk-field"><label>完成后空闲模式</label><select data-vk-field="idle_mode"><option value="cellular_sms">蜂窝驻网接短信（推荐）</option><option value="vowifi">VoWiFi</option><option value="airplane">飞行模式</option></select></div>
            <label class="vk-check"><input type="checkbox" data-vk-field="enabled">启用定时保号</label>
            <label class="vk-check"><input type="checkbox" data-vk-field="notify_on_success">成功时 PushDeer</label>
            <label class="vk-check"><input type="checkbox" data-vk-field="notify_on_failure">失败时 PushDeer</label>
          </div>
          <div class="vk-actions"><button class="vk-button" data-vk-save>保存配置</button><button class="vk-button vk-button-danger" data-vk-run>立即保号</button></div>
          <div class="vk-note">立即保号会真实打开蜂窝数据并产生少量资费；请求强制绑定所选蜂窝网卡，任务结束后自动恢复空闲策略。</div>
        </section>
        <section class="vk-card"><h2 class="vk-section-title">执行历史</h2><div class="vk-table-wrap"><table class="vk-table"><thead><tr><th>开始时间</th><th>结果</th><th>HTTP</th><th>接收</th><th>发送</th><th>总流量</th><th>耗时</th><th>说明</th></tr></thead><tbody data-vk-history><tr><td colspan="8" class="vk-empty">暂无记录</td></tr></tbody></table></div></section>
      </div>`;
    root.querySelector("[data-vk-refresh]").addEventListener("click", loadAll);
    root.querySelector("[data-vk-save]").addEventListener("click", saveConfig);
    root.querySelector("[data-vk-run]").addEventListener("click", runNow);
  }

  function setText(root, selector, value, className) {
    const node = root.querySelector(selector);
    if (!node) return;
    node.textContent = value;
    if (className) node.className = `vk-metric-value ${className}`;
  }

  function renderStatus(root, status) {
    setText(root, "[data-vk-state]", status.running ? "执行中" : (status.enabled ? "已启用" : "已停用"), status.enabled ? "vk-good" : "");
    setText(root, "[data-vk-next]", formatTime(status.next_run_at));
    setText(root, "[data-vk-last]", formatTime(status.last_success_at));
    setText(root, "[data-vk-bytes]", formatBytes(status.last_success_bytes));
    const run = root.querySelector("[data-vk-run]");
    if (run) run.disabled = Boolean(status.running);
  }

  function renderConfig(root, config) {
    Object.entries(config).forEach(([key, value]) => {
      const node = root.querySelector(`[data-vk-field="${CSS.escape(key)}"]`);
      if (!node) return;
      if (node.type === "checkbox") node.checked = Boolean(value);
      else node.value = value ?? "";
    });
    const limit = root.querySelector('[data-vk-field="max_session_kib"]');
    if (limit) limit.value = Math.round(Number(config.max_session_bytes || 0) / 1024);
  }

  function renderHistory(root, items) {
    const tbody = root.querySelector("[data-vk-history]");
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="vk-empty">暂无执行记录</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((item) => `<tr>
      <td>${escapeHtml(formatTime(item.started_at))}</td>
      <td class="${item.status === "success" ? "vk-good" : "vk-bad"}">${escapeHtml(item.status)}</td>
      <td>${escapeHtml(item.http_status ?? "-")}</td>
      <td>${escapeHtml(formatBytes(item.session_rx_bytes))}</td>
      <td>${escapeHtml(formatBytes(item.session_tx_bytes))}</td>
      <td>${escapeHtml(formatBytes(item.session_total_bytes))}</td>
      <td>${escapeHtml(item.duration_seconds == null ? "-" : `${item.duration_seconds}s`)}</td>
      <td>${escapeHtml(item.error || item.restore_status || "")}</td>
    </tr>`).join("");
  }

  async function loadAll() {
    if (!active) return;
    const root = rootElement();
    if (!root) return;
    renderShell(root);
    try {
      const [config, status, history] = await Promise.all([
        api("/config"), api("/status"), api("/history?limit=50"),
      ]);
      renderConfig(root, config);
      renderStatus(root, status);
      renderHistory(root, history.items || []);
    } catch (error) {
      console.error("VoHive 保号模块加载失败", error);
      alert(`保号模块加载失败：${error.message}`);
    }
  }

  async function loadStatusOnly() {
    if (!active) return;
    const root = document.getElementById(MODULE_ID);
    if (!root) return;
    try { renderStatus(root, await api("/status")); } catch (_) {}
  }

  function collectConfig(root) {
    const result = {};
    root.querySelectorAll("[data-vk-field]").forEach((node) => {
      const key = node.getAttribute("data-vk-field");
      result[key] = node.type === "checkbox" ? node.checked : node.value;
    });
    ["interval_days", "network_connect_timeout_seconds", "request_timeout_seconds", "max_session_seconds", "failure_retry_hours"].forEach((key) => result[key] = Number(result[key]));
    result.max_session_bytes = Number(result.max_session_kib) * 1024;
    delete result.max_session_kib;
    return result;
  }

  async function saveConfig() {
    const root = document.getElementById(MODULE_ID);
    if (!root) return;
    try {
      await api("/config", { method: "PUT", body: JSON.stringify(collectConfig(root)) });
      alert("保号配置已保存");
      await loadAll();
    } catch (error) { alert(`保存失败：${error.message}`); }
  }

  async function runNow() {
    if (!confirm("这会真实使用少量蜂窝流量，确定立即执行保号？")) return;
    try {
      await api("/run", { method: "POST", body: JSON.stringify({ confirm: true }) });
      alert("保号任务已开始");
      await loadAll();
    } catch (error) { alert(`启动失败：${error.message}`); }
  }

  document.addEventListener("click", (event) => {
    const item = event.target.closest && event.target.closest(".el-menu-item");
    if (item && !item.hasAttribute("data-vohive-keepalive-nav") && active) deactivate(true);
  }, true);

  window.addEventListener("popstate", () => {
    const shouldActivate = new URL(location.href).searchParams.get("module") === "keepalive";
    if (shouldActivate && !active) activate(false);
    if (!shouldActivate && active) deactivate(false);
  });

  installStyle();
  const observer = new MutationObserver(() => {
    installMenus();
    if (active) {
      document.documentElement.classList.add("vohive-keepalive-mode");
      const root = rootElement();
      if (root && !root.querySelector(".vk-page")) loadAll();
    }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  installMenus();
  if (active) setTimeout(() => activate(false), 300);
})();
