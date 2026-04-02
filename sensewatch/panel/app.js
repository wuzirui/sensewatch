/* SenseWatch Panel — fetches state from Python bridge, renders UI */

let state = null;
let pollInterval = null;
let expandedCards = new Set();  // track which cards are expanded by name
let runningOnly = false;        // filter to show only running jobs

// ── Init ──────────────────────────────────────────────────────────────────

async function init() {
  while (!window.pywebview || !window.pywebview.api) {
    await new Promise(r => setTimeout(r, 100));
  }
  await fetchState();
  pollInterval = setInterval(fetchState, 2000);
}

async function fetchState() {
  try {
    const newState = await pywebview.api.get_state();
    // Only re-render if data actually changed
    if (JSON.stringify(newState) !== JSON.stringify(state)) {
      state = newState;
      render();
    }
  } catch (e) {
    console.error("fetch failed:", e);
  }
}

// ── Render (preserves expanded state + scroll) ───────────────────────────

function render() {
  if (!state) return;

  // Never rebuild DOM while the log/events modal is open
  const modal = document.getElementById("log-modal");
  if (modal && !modal.classList.contains("hidden")) return;

  const scrollTop = document.getElementById("app").scrollTop;
  renderHealth();
  renderJobs();
  renderCCI();
  renderGPU();
  renderFlavor();
  document.getElementById("app").scrollTop = scrollTop;
}

function renderHealth() {
  setDot("h-aec2", state.health.aec2);
  setDot("h-cci", state.health.cci);
  setDot("h-monitor", state.health.monitor);
}

function setDot(id, ok) {
  const el = document.getElementById(id);
  if (el) el.className = "health-dot" + (ok ? " ok" : "");
}

// ── Jobs ──────────────────────────────────────────────────────────────────

function renderJobs() {
  const list = document.getElementById("jobs-list");
  const other = document.getElementById("jobs-other");

  const visibleJobs = runningOnly
    ? state.jobs.filter(j => j.state === "RUNNING" || j.state === "STARTING" || j.state === "CREATING" || j.state === "INIT")
    : state.jobs;
  const hiddenCount = state.jobs.length - visibleJobs.length;

  // Skip rebuild if any job card is expanded (user is interacting)
  const anyExpanded = visibleJobs.some(j => expandedCards.has(j.name));
  if (anyExpanded) {
    for (const job of visibleJobs) {
      if (job.last_log_line) {
        const cards = list.querySelectorAll('.card');
        for (const card of cards) {
          if (card.querySelector('.card-name')?.textContent === (job.display_name || job.name)) {
            const logEl = card.querySelector('.card-logs');
            if (logEl) logEl.textContent = job.last_log_line;
          }
        }
      }
    }
    return;
  }

  if (!visibleJobs.length) {
    list.innerHTML = '<div class="empty">No active jobs</div>';
  } else {
    list.innerHTML = visibleJobs.map(jobCard).join("");
  }

  let info = "";
  if (runningOnly && hiddenCount > 0) info += `${hiddenCount} non-running hidden`;
  if (state.other_jobs_count > 0) {
    if (info) info += " · ";
    info += `${state.other_jobs_count} other users' jobs hidden`;
  }
  other.textContent = info;
}

function jobCard(job) {
  const badge = badgeClass(job.state);
  const gpu = job.gpu_count > 0
    ? `${job.gpu_count}\u00d7 ${job.spec_name.split(".")[0] || "GPU"}`
    : "";
  const logHtml = job.last_log_line
    ? `<div class="card-logs">${esc(job.last_log_line)}</div>`
    : "";

  const schrodinger = job.state === "STARTING" && job.create_time && isStuckStarting(job)
    ? ' (or is it?) \ud83d\udc31' : '';

  const isExpanded = expandedCards.has(job.name);

  return `
    <div class="card${isExpanded ? ' expanded' : ''}" onclick="toggleCard(this, '${esc(job.name)}')">
      <div class="card-header">
        <span class="card-name">${esc(job.display_name)}</span>
        <span class="badge ${badge}">${esc(job.state)}${schrodinger}</span>
      </div>
      <div class="card-meta">${esc(gpu)}${gpu && job.pool_name ? " \u00b7 " : ""}${esc(job.pool_name)}</div>
      ${logHtml}
      <div class="card-actions">
        <button class="btn btn-sm" onclick="event.stopPropagation(); viewLogs('${esc(job.name)}', '${esc(job.workspace)}')">Logs</button>
        <button class="btn btn-sm" onclick="event.stopPropagation(); viewDetail('${esc(job.name)}', '${esc(job.workspace)}')">Events</button>
        <button class="btn btn-sm" onclick="event.stopPropagation(); copyName('${esc(job.name)}')">Copy</button>
        <button class="btn btn-sm" onclick="event.stopPropagation(); openConsole('acp')">Console</button>
      </div>
    </div>`;
}

function isStuckStarting(job) {
  try {
    const created = new Date(job.create_time);
    return (Date.now() - created.getTime()) > 600000;
  } catch { return false; }
}

// ── CCI ───────────────────────────────────────────────────────────────────

function renderCCI() {
  const list = document.getElementById("cci-list");
  const other = document.getElementById("cci-other");

  if (!state.cci.length) {
    list.innerHTML = '<div class="empty">No containers</div>';
  } else {
    list.innerHTML = state.cci.map(cciCard).join("");
  }

  other.textContent = state.other_cci_count > 0
    ? `${state.other_cci_count} other users' containers hidden`
    : "";
}

function cciCard(app) {
  const badge = badgeClass(app.state);
  const canStart = ["STOPPED", "SUSPENDED"].includes(app.state);
  const canStop = app.state === "RUNNING";
  const isExpanded = expandedCards.has(app.name);

  const actions = [];
  if (canStart) actions.push(`<button class="btn btn-sm btn-success" onclick="event.stopPropagation(); cciStart('${esc(app.name)}', '${esc(app.workspace)}')">Start</button>`);
  if (canStop) actions.push(`<button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); cciStop('${esc(app.name)}', '${esc(app.workspace)}')">Stop</button>`);
  actions.push(`<button class="btn btn-sm" onclick="event.stopPropagation(); openConsole('cci')">Console</button>`);

  return `
    <div class="card${isExpanded ? ' expanded' : ''}" onclick="toggleCard(this, '${esc(app.name)}')">
      <div class="card-header">
        <span class="card-name">${esc(app.display_name)}</span>
        <span class="badge ${badge}">${esc(app.state)}</span>
      </div>
      ${app.gpu_count > 0 ? `<div class="card-meta">${app.gpu_count} GPU</div>` : ''}
      <div class="card-actions">
        ${actions.join("")}
      </div>
    </div>`;
}

// ── GPU ───────────────────────────────────────────────────────────────────

function renderGPU() {
  // Skip full rebuild if any GPU card is expanded (breakdown open)
  const anyExpanded = state.gpus.some(g => expandedCards.has("gpu-" + g.cluster));
  if (anyExpanded) {
    // Only update the numbers in-place without rebuilding HTML
    for (const gpu of state.gpus) {
      const countEl = document.querySelector(`#gpu-bd-${gpu.cluster}`)?.closest('.gpu-card')?.querySelector('.gpu-count');
      if (countEl) countEl.textContent = `Idle: ${gpu.idle} / ${gpu.total}`;
    }
    return;
  }

  const list = document.getElementById("gpu-list");
  if (!state.gpus.length) {
    list.innerHTML = '<div class="empty">Loading...</div>';
    return;
  }
  list.innerHTML = state.gpus.map(gpuCard).join("");
}

function gpuCard(gpu) {
  const pct = gpu.total > 0 ? (gpu.used / gpu.total) * 100 : 0;
  const barClass = pct >= 95 ? "full" : pct >= 75 ? "high" : pct >= 50 ? "mid" : "low";
  const isExpanded = expandedCards.has("gpu-" + gpu.cluster);

  return `
    <div class="gpu-card clickable${isExpanded ? ' expanded' : ''}" onclick="toggleGPU(this, '${esc(gpu.cluster)}')">
      <div class="gpu-header">
        <span class="gpu-name">${esc(shortCluster(gpu.cluster))}</span>
        <span class="gpu-count">Idle: ${gpu.idle} / ${gpu.total}</span>
      </div>
      <div class="gpu-bar-bg">
        <div class="gpu-bar-fill ${barClass}" style="width: ${pct}%"></div>
      </div>
      <div class="gpu-meta">${esc(gpu.device)} ${gpu.vram_gb}GB</div>
      ${gpu.commentary ? `<div class="gpu-commentary">${esc(gpu.commentary)}</div>` : ''}
      <div class="gpu-breakdown" id="gpu-bd-${esc(gpu.cluster)}"></div>
    </div>`;
}

async function toggleGPU(el, cluster) {
  const key = "gpu-" + cluster;
  const bdEl = document.getElementById("gpu-bd-" + cluster);

  if (expandedCards.has(key)) {
    expandedCards.delete(key);
    el.classList.remove("expanded");
    if (bdEl) bdEl.innerHTML = "";
    return;
  }

  expandedCards.add(key);
  el.classList.add("expanded");
  if (bdEl) bdEl.innerHTML = '<div class="bd-loading">Loading node breakdown...</div>';

  try {
    const data = await pywebview.api.get_gpu_breakdown(cluster);
    if (!bdEl) return;

    // Filter out full nodes, sort by idle desc
    const nodesWithIdle = data.nodes.filter(n => n.idle > 0).sort((a, b) => b.idle - a.idle);
    const fullCount = data.full_nodes || 0;

    let html = '';
    if (data.visible_gaps !== data.sco_idle && data.sco_idle !== undefined) {
      html += `<div class="bd-summary">Gaps on visible nodes: ${esc(data.idle_summary)}</div>`;
      if (data.note) html += `<div class="bd-note">${esc(data.note)}</div>`;
    } else {
      html += `<div class="bd-summary">${data.sco_idle} = ${esc(data.idle_summary)}</div>`;
    }
    html += '<div class="bd-nodes">';
    for (const node of nodesWithIdle) {
      const blocks = [];
      for (let i = 0; i < node.total; i++) {
        blocks.push(i < node.used
          ? '<span class="bd-block used"></span>'
          : '<span class="bd-block idle"></span>');
      }
      const hostShort = node.host.startsWith("(") ? node.host : node.host.split(".").slice(-2).join(".");
      html += `<div class="bd-node">
        <span class="bd-host">${esc(hostShort)}</span>
        <span class="bd-blocks">${blocks.join("")}</span>
        <span class="bd-label">${node.idle}/${node.total}</span>
      </div>`;
    }
    if (fullCount > 0) {
      html += `<div class="bd-full-count">${fullCount} full node${fullCount > 1 ? 's' : ''} hidden</div>`;
    }
    html += '</div>';
    bdEl.innerHTML = html;
  } catch (e) {
    if (bdEl) bdEl.innerHTML = `<div class="bd-loading">Error: ${e}</div>`;
  }
}

function shortCluster(name) {
  return name.replace("computing-cluster-", "").replace("debug-cluster-", "debug-");
}

// ── Flavor ────────────────────────────────────────────────────────────────

function renderFlavor() {
  document.getElementById("flavor").textContent = state.flavor || "";
}

// ── Actions ───────────────────────────────────────────────────────────────

function toggleRunningOnly() {
  runningOnly = !runningOnly;
  const btn = document.getElementById("btn-running-only");
  if (btn) btn.classList.toggle("active", runningOnly);
  render();
}

function toggleCard(el, name) {
  el.classList.toggle("expanded");
  if (expandedCards.has(name)) {
    expandedCards.delete(name);
  } else {
    expandedCards.add(name);
  }
}

async function viewLogs(name, workspace) {
  const modal = document.getElementById("log-modal");
  const title = document.getElementById("log-modal-title");
  const body = document.getElementById("log-modal-body");

  title.textContent = `Logs: ${name}`;
  body.textContent = "Loading...";
  modal.classList.remove("hidden");

  try {
    const logs = await pywebview.api.get_job_logs(name, workspace);
    body.textContent = logs;
    await pywebview.api.copy_to_clipboard(logs);
  } catch (e) {
    body.textContent = `Error: ${e}`;
  }
}

function closeLogModal() {
  document.getElementById("log-modal").classList.add("hidden");
}

async function viewDetail(name, workspace) {
  const modal = document.getElementById("log-modal");
  const title = document.getElementById("log-modal-title");
  const body = document.getElementById("log-modal-body");

  title.textContent = `Events: ${name}`;
  body.textContent = "Loading...";
  modal.classList.remove("hidden");

  try {
    const detail = await pywebview.api.get_job_detail(name, workspace);
    let text = "";

    // Workers
    if (detail.workers.length) {
      text += "=== Workers ===\n";
      for (const w of detail.workers) {
        const short = w.name.split("-").slice(-2).join("-");
        text += `  ${short}  ${w.phase}  ${w.device_type}  host=${w.host_ip || "(none)"}\n`;
      }
      text += "\n";
    }

    // Job events
    if (detail.job_events.length) {
      text += "=== Job Events ===\n";
      for (const e of detail.job_events) {
        const tag = e.type === "Warning" ? "\u26a0" : "\u2713";
        text += `  ${tag} [${e.reason}] ${e.age} ago\n    ${e.message}\n`;
      }
      text += "\n";
    }

    // Worker events (FailedScheduling etc.)
    if (detail.worker_events.length) {
      text += "=== Worker Events ===\n";
      for (const e of detail.worker_events) {
        const tag = e.type === "Warning" ? "\u26a0" : "\u2713";
        text += `  ${tag} [${e.worker}] [${e.reason}] ${e.age} ago\n    ${e.message}\n`;
      }
    }

    if (!text) text = "(No events found)";
    body.textContent = text;
  } catch (e) {
    body.textContent = `Error: ${e}`;
  }
}

async function copyName(name) {
  await pywebview.api.copy_to_clipboard(name);
}

function openConsole(type) {
  const base = "https://console.sensecore.cn";
  if (type === "acp") pywebview.api.open_console(`${base}/acp`);
  else pywebview.api.open_console(`${base}/cci`);
}

async function cciStart(name, workspace) {
  const result = await pywebview.api.cci_start(name, workspace);
  if (result !== "ok") alert(result);
  await fetchState();
}

async function cciStop(name, workspace) {
  const result = await pywebview.api.cci_stop(name, workspace);
  if (result !== "ok") alert(result);
  await fetchState();
}

async function doRefresh() {
  const btn = document.getElementById("btn-refresh");
  btn.classList.add("loading");
  btn.innerHTML = '<span class="spinner"></span> Refreshing';
  try {
    state = await pywebview.api.refresh();
    render();
  } catch (e) {
    console.error(e);
  }
  btn.classList.remove("loading");
  btn.textContent = "Refresh";
}

function doQuit() {
  pywebview.api.quit_app();
}

// ── Helpers ───────────────────────────────────────────────────────────────

function badgeClass(state) {
  const s = state.toUpperCase();
  if (s === "RUNNING") return "badge-running";
  if (s === "STARTING" || s === "CREATING" || s === "INIT" || s === "PENDING" || s === "PROGRESSING" || s === "QUEUING") return "badge-starting";
  if (s === "SUCCEEDED") return "badge-succeeded";
  if (s === "FAILED") return "badge-failed";
  if (s === "STOPPED" || s === "DELETED") return "badge-stopped";
  if (s === "SUSPENDED") return "badge-suspended";
  return "badge-default";
}

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ── Keyboard ──────────────────────────────────────────────────────────────

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const modal = document.getElementById("log-modal");
    if (!modal.classList.contains("hidden")) {
      closeLogModal();
    } else {
      // Hide panel on Escape
      pywebview.api.hide_panel();
    }
  }
});

// ── Start ─────────────────────────────────────────────────────────────────

init();
