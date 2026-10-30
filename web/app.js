/* 单页界面：纯原生 DOM，无构建步骤。所有来自模型/数据库的文本都经过 esc() 转义。 */

const state = {
  bootstrap: null,
  materialId: null,
  detailId: null,
  date: "",
  hour: null,
  slots: [],
  result: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  completed: "已保存草稿",
  needs_confirmation: "待确认",
  needs_input: "缺少信息",
  blocked: "已阻断",
  not_found: "素材不存在",
  tool_error: "调用失败",
  step_limit_reached: "达到步数上限（未完成）",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { error: text }; }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast" + (isError ? " err" : "");
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 3200);
}

function fmtSlot(iso) {
  if (!iso) return "—";
  const text = String(iso);
  return text.length >= 16 ? `${text.slice(0, 10)} ${text.slice(11, 16)}` : text;
}

/* ---------- 标签页 ---------- */
function switchTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  }
  for (const panel of ["result", "drafts", "material"]) {
    $(`tab-${panel}`).hidden = panel !== name;
  }
}

/* ---------- 素材库 ---------- */
function renderMaterials() {
  const box = $("materials");
  box.innerHTML = "";
  for (const item of state.bootstrap.materials) {
    const row = document.createElement("div");
    row.className = "material" + (item.id === state.materialId ? " selected" : "");
    const status = String(item.audit_status || "PENDING").toLowerCase();
    row.innerHTML = `
      <span class="mid">${esc(item.id)}</span>
      <span class="meta">
        <span class="title">${esc(item.title)}</span>
        <span class="tags">${esc((item.tags || []).join(" · ") || "无标签")}${item.duration_sec ? " · " + esc(item.duration_sec) + "s" : ""}</span>
      </span>
      <span class="badge ${esc(status)}">${esc(item.audit_status || "PENDING")}</span>`;
    row.title = item.risk_reason || "点击查看详情";
    row.onclick = () => openMaterial(item.id);
    box.appendChild(row);
  }
}

async function openMaterial(materialId) {
  state.materialId = materialId;
  renderMaterials();
  switchTab("material");
  await loadMaterialDetail(materialId);
}

/* ---------- 时段 ---------- */
function renderSlots() {
  const box = $("slots");
  box.innerHTML = "";
  if (!state.slots.length) {
    box.innerHTML = `<p class="muted" style="grid-column:1/-1">这一天没有可选时段</p>`;
    return;
  }
  for (const slot of state.slots) {
    const hour = Number(slot.slot.slice(11, 13));
    const node = document.createElement("button");
    node.type = "button";
    node.className = "slot " + slot.state + (hour === state.hour ? " selected" : "");
    node.textContent = String(hour).padStart(2, "0") + ":00";
    if (slot.state === "occupied") {
      node.title = `已被 ${slot.occupied_by || "其他素材"} 占用；仍可选中，Agent 会给出替代时段或阻断`;
    }
    if (slot.state === "closed") node.title = "超出发布窗口或已过期";
    // 占用时段也允许选中：用户完全可能就要这个时间，让 Agent 去解释冲突
    if (slot.state !== "closed") node.onclick = () => { state.hour = hour; renderSlots(); };
    else node.disabled = true;
    box.appendChild(node);
  }
}

async function loadSlots() {
  if (!state.date) return;
  const data = await api(`/api/slots?date=${encodeURIComponent(state.date)}`);
  state.slots = data.slots || [];
  const free = state.slots.find((slot) => slot.state === "free");
  const hourOf = (slot) => Number(slot.slot.slice(11, 13));
  const stillSelectable = state.slots.some(
    (slot) => hourOf(slot) === state.hour && slot.state !== "closed"
  );
  if (state.hour === null || !stillSelectable) {
    state.hour = free ? hourOf(free) : null;
  }
  renderSlots();
}

/* ---------- 运行结果 ---------- */
function renderResult(result) {
  state.result = result;
  $("empty").hidden = true;
  const box = $("result");
  box.hidden = false;

  const status = result.status || "unknown";
  const draft = result.draft || {};
  const usage = result.llm_usage || {};
  const tokens = usage.total_tokens ? `${usage.total_tokens}` : "—";
  const model = (result.evidence && result.evidence.model) || "—";
  const policy = (result.evidence && result.evidence.policy) || "—";

  const cells = [
    ["审核结论", result.review_status || "—"],
    ["期望时段", fmtSlot(result.requested_slot)],
    ["建议时段", fmtSlot(result.suggested_slot)],
    ["草稿", draft.saved ? `#${draft.draft_id} · ${draft.status}${draft.deduped ? " · 幂等命中" : ""}` : "未保存"],
    ["决策 / 工具调用", `${result.steps ?? 0} / ${result.tool_calls ?? 0}`],
    ["模型", `${policy} · ${model}`],
    ["token", tokens],
    ["是否发布", result.published ? "是" : "否（仅草稿）"],
  ];

  box.innerHTML = `
    <div class="result-head">
      <span class="badge ${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>
      <span class="mid">${esc(result.material_id || "")}</span>
      ${result.finished === false ? `<span class="badge step_limit_reached">finished=false</span>` : ""}
    </div>
    <p class="message">${esc(result.message || "")}</p>
    <div class="kv">
      ${cells.map(([k, v]) => `<div class="cell"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}
    </div>
    ${renderPending(result.pending_confirmations)}
    ${renderHits(result.rule_hits)}
    ${draft.saved ? `<button id="confirm" class="btn small">确认这份草稿（仍是草稿，不发布）</button>` : ""}
    ${renderTimeline(result.decisions)}
    ${renderRaw(result)}
  `;

  const confirmBtn = $("confirm");
  if (confirmBtn) confirmBtn.onclick = () => confirmDraft(draft.draft_id);
}

function renderPending(pending) {
  if (!pending || !pending.length) return "";
  return `<div class="pending"><h3>待确认项 ${pending.length}</h3><ul>${
    pending.map((item) => `<li>${esc(item)}</li>`).join("")
  }</ul></div>`;
}

function renderHits(hits) {
  if (!hits || !hits.length) return "";
  return `<div class="hits">${hits.map((hit) =>
    `<span class="hit ${esc(String(hit.level || "").toLowerCase())}" title="${esc(hit.suggestion || "")}">${esc(hit.code)} · ${esc(hit.message)}</span>`
  ).join("")}</div>`;
}

function renderTimeline(decisions) {
  if (!decisions || !decisions.length) return "";
  const items = decisions.map((step) => {
    const observation = step.observation;
    const obsLine = observation
      ? `<div class="obs ${observation.ok ? "" : "fail"}">${observation.ok ? "↳" : "✗"} ${esc(observation.summary)}</div>`
      : "";
    const args = Object.keys(step.args || {}).length ? `<div class="args">${esc(JSON.stringify(step.args))}</div>` : "";
    const why = step.rationale ? `<div class="why">${esc(step.rationale)}</div>` : "";
    return `<li data-step="${esc(step.step)}">
      <div class="action">${esc(step.action)}${step.next ? ` <span class="muted">→ ${esc(step.next)}</span>` : ""}</div>
      ${args}${obsLine}${why}
    </li>`;
  }).join("");
  return `<h2 style="margin-top:18px">决策过程</h2><ul class="timeline">${items}</ul>`;
}

function renderRaw(result) {
  return `<details class="trace"><summary>完整 JSON（trace / decisions / evidence）</summary>
    <pre>${esc(JSON.stringify(result, null, 2))}</pre></details>`;
}

/* ---------- 草稿箱 ---------- */
async function loadDrafts() {
  const box = $("drafts");
  try {
    const data = await api("/api/drafts");
    renderDrafts(data);
  } catch (error) {
    box.className = "empty";
    box.innerHTML = `<span class="error">草稿箱读取失败：${esc(error.message)}</span>`;
  }
}

function renderDrafts(data) {
  const badge = $("draft-badge");
  badge.textContent = data.active;
  badge.className = "pill" + (data.active ? " hot" : "");

  const box = $("drafts");
  if (!data.drafts.length) {
    box.className = "empty";
    box.textContent = "草稿箱是空的：选素材跑一次就有草稿了";
    return;
  }
  box.className = "";
  const counts = data.counts || {};
  const rows = data.drafts.map((draft) => {
    const conflict = draft.conflict_with.length
      ? `<span class="badge conflict">与 ${esc(draft.conflict_with.join("、"))} 冲突</span>`
      : "";
    const actions = draft.status === "DRAFT"
      ? `<button class="btn small" data-confirm="${draft.id}">确认</button>
         <button class="btn small" data-cancel="${draft.id}">取消</button>`
      : "";
    return `<div class="draft-row">
      <div class="draft-main">
        <span class="mid">#${esc(draft.id)}</span>
        <span class="title">${esc(draft.material_id)} · ${esc(draft.title)}</span>
        <span class="badge ${esc(String(draft.status).toLowerCase())}">${esc(draft.status)}</span>
        <span class="slot">${esc(fmtSlot(draft.date))}</span>
        ${conflict}
        ${draft.confirm_time ? `<span class="muted">确认于 ${esc(fmtSlot(draft.confirm_time))}</span>` : ""}
      </div>
      <div class="draft-actions">
        ${actions}
        <button class="btn small" data-detail="${esc(draft.material_id)}">看素材</button>
      </div>
    </div>`;
  }).join("");

  const conflictBox = Object.keys(data.conflicts || {}).length
    ? `<div class="warn-box">检测到时段冲突：${Object.entries(data.conflicts)
        .map(([slot, ids]) => `${esc(fmtSlot(slot))} 被 ${esc(ids.join("、"))} 同时占用`)
        .join("；")}</div>`
    : `<div class="ok-box">冲突扫描：当前没有同一时段被多个素材占用。</div>`;

  box.innerHTML = `
    <div class="drafts-head">
      <span>共 <b>${data.drafts.length}</b> 条</span>
      <span>DRAFT ${counts.DRAFT || 0} · CONFIRMED ${counts.CONFIRMED || 0} · CANCELLED ${counts.CANCELLED || 0}</span>
      <span>已发布 ${data.published_total || 0}（本期不发布）</span>
    </div>
    ${conflictBox}
    <div style="margin-top:12px">${rows}</div>`;

  box.querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.onclick = () => confirmDraft(Number(btn.dataset.confirm));
  });
  box.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.onclick = () => cancelDraft(Number(btn.dataset.cancel));
  });
  box.querySelectorAll("[data-detail]").forEach((btn) => {
    btn.onclick = () => openMaterial(btn.dataset.detail);
  });
}

/* ---------- 素材详情 ---------- */
async function loadMaterialDetail(materialId) {
  state.detailId = materialId;
  const box = $("material-detail");
  try {
    const data = await api(`/api/materials/${encodeURIComponent(materialId)}`);
    box.className = "";
    box.innerHTML = renderDetail(data);
    wireDetail(data);
  } catch (error) {
    box.className = "empty";
    box.innerHTML = `<span class="error">读取失败：${esc(error.message)}</span>`;
  }
}

function renderDetail(data) {
  const meta = data.metadata || {};
  const audit = data.review || {};
  const auditStatus = String(audit.status || "PENDING");

  const metaCells = [
    ["时长", meta.duration_sec ? `${meta.duration_sec}s` : "—"],
    ["格式", meta.format || "—"],
    ["版权", meta.copyright || "—"],
    ["作者", meta.author || "—"],
    ["标签", (meta.tags || []).join(" · ") || "—"],
  ];

  const schedules = data.schedules.length
    ? data.schedules.map((item) => {
        const conflict = item.conflict_with.length
          ? `<span class="badge conflict">与 ${esc(item.conflict_with.join("、"))} 冲突</span>`
          : `<span class="badge ok">无冲突</span>`;
        const actions = item.status === "DRAFT"
          ? `<span class="draft-actions">
               <button class="btn small" data-confirm="${item.id}">确认</button>
               <button class="btn small" data-cancel="${item.id}">取消</button>
             </span>`
          : "";
        return `<div class="sched-row">
          <span class="mid muted">#${esc(item.id)}</span>
          <span class="slot">${esc(fmtSlot(item.date))}</span>
          <span class="badge ${esc(String(item.status).toLowerCase())}">${esc(item.status)}</span>
          ${conflict}
          ${item.confirm_time ? `<span class="muted">确认于 ${esc(fmtSlot(item.confirm_time))}</span>` : ""}
          ${actions}
        </div>`;
      }).join("")
    : `<div class="ok-box">未排期：这个素材还没有草稿或排期。</div>`;

  const conflicts = Object.keys(data.global_conflicts || {});
  const conflictBox = conflicts.length
    ? `<div class="warn-box">全局冲突：${Object.entries(data.global_conflicts)
        .map(([slot, ids]) => `${esc(fmtSlot(slot))} 被 ${esc(ids.join("、"))} 同时占用`)
        .join("；")}</div>`
    : "";

  return `
    <div class="detail-head">
      <span class="mid">${esc(data.id)}</span>
      <h3>${esc(data.title)}</h3>
      <span class="badge ${esc(auditStatus.toLowerCase())}">${esc(auditStatus)}</span>
      ${data.conflict_count ? `<span class="badge conflict">冲突 ${data.conflict_count}</span>` : ""}
    </div>
    <div class="meta-grid">
      ${metaCells.map(([k, v]) => `<div class="cell"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}
    </div>

    <div class="section-title">审核状态</div>
    <div class="kv">
      <div class="cell"><div class="k">结论</div><div class="v">${esc(auditStatus)}</div></div>
      <div class="cell"><div class="k">风险原因</div><div class="v">${esc(audit.risk_reason || "无")}</div></div>
      <div class="cell"><div class="k">规则版本</div><div class="v">${esc(audit.rules_version || "—")}</div></div>
      <div class="cell"><div class="k">发布时间</div><div class="v">${audit.release_date ? esc(fmtSlot(audit.release_date)) : "未发布（本系统不发布）"}</div></div>
    </div>

    <div class="section-title">当前排期（${data.schedules.length}）</div>
    ${schedules}
    ${conflictBox}
    <div class="row" style="margin-top:14px">
      <button class="btn small" data-fill="${esc(data.id)}">用这个素材排期</button>
      <button class="btn small" data-refresh="${esc(data.id)}">刷新</button>
    </div>`;
}

function wireDetail(data) {
  const box = $("material-detail");
  box.querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.onclick = () => confirmDraft(Number(btn.dataset.confirm));
  });
  box.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.onclick = () => cancelDraft(Number(btn.dataset.cancel));
  });
  const fill = box.querySelector("[data-fill]");
  if (fill) fill.onclick = () => {
    state.materialId = data.id;
    renderMaterials();
    switchTab("result");
    toast(`已选中 ${data.id}，选好时段后点「运行 Agent」`);
  };
  const refresh = box.querySelector("[data-refresh]");
  if (refresh) refresh.onclick = () => loadMaterialDetail(data.id);
}

/* ---------- 动作 ---------- */
async function runAgent() {
  if (state.busy) return;
  const errorBox = $("form-error");
  errorBox.hidden = true;
  if (!state.materialId) return showError("先选一个素材");
  if (!state.date) return showError("先选日期");

  state.busy = true;
  const button = $("run");
  button.disabled = true;
  button.innerHTML = `<span class="spinner"></span>运行中…`;

  try {
    const result = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({
        material_id: state.materialId,
        date: state.date,
        hour: state.hour,
        constraint: document.querySelector('input[name="constraint"]:checked').value,
        user_text: $("user_text").value,
        provider: $("provider").value,
        max_steps: Number($("max_steps").value) || 10,
        simulate: $("simulate").value,
      }),
    });
    renderResult(result);
    switchTab("result");
    await refreshAfterMutation();
  } catch (error) {
    showError(error.message);
  } finally {
    state.busy = false;
    button.disabled = false;
    button.textContent = "运行 Agent";
  }
}

async function confirmDraft(draftId) {
  try {
    const data = await api("/api/confirm", {
      method: "POST",
      body: JSON.stringify({ draft_id: draftId }),
    });
    toast(`草稿 #${draftId} 已确认（${data.draft.status}，仍未发布）`);
    if (state.result && state.result.draft) {
      state.result.draft.status = data.draft.status || "CONFIRMED";
      renderResult(state.result);
    }
    await refreshAfterMutation();
  } catch (error) {
    toast(error.message, true);
  }
}

async function cancelDraft(draftId) {
  if (!confirm(`取消草稿 #${draftId}？该时段会被释放。`)) return;
  try {
    const data = await api("/api/cancel", {
      method: "POST",
      body: JSON.stringify({ draft_id: draftId }),
    });
    toast(`草稿 #${draftId} 已取消（${data.draft.status}）`);
    await refreshAfterMutation();
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshAfterMutation() {
  await loadDrafts();
  await loadSlots();
  if (state.detailId) await loadMaterialDetail(state.detailId);
  await refreshMaterialBadges();
}

async function resetData() {
  if (!confirm("把所有草稿和审核记录重置回种子数据？")) return;
  try {
    await api("/api/reset", { method: "POST", body: "{}" });
    toast("已重置");
    state.result = null;
    state.materialId = null;
    state.detailId = null;
    $("result").hidden = true;
    $("empty").hidden = false;
    $("material-detail").className = "empty";
    $("material-detail").textContent = "点左侧任意素材，查看它的审核状态、排期与冲突。";
    await init(true);
    await loadDrafts();
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshMaterialBadges() {
  try {
    const fresh = await api("/api/bootstrap");
    state.bootstrap.materials = fresh.materials;
    renderMaterials();
  } catch { /* 忽略：徽章刷新失败不影响主流程 */ }
}

function showError(message) {
  const box = $("form-error");
  box.textContent = message;
  box.hidden = false;
}

function updateProviderHint() {
  const provider = $("provider").value;
  const ready = state.bootstrap && state.bootstrap.deepseek_ready;
  const hint = $("provider-hint");
  if (provider === "mock") {
    hint.textContent = "无需 key";
    hint.className = "hint ok";
  } else if (ready) {
    hint.textContent = "key 已配置";
    hint.className = "hint ok";
  } else {
    hint.textContent = "未配置 DEEPSEEK_API_KEY";
    hint.className = "hint warn";
  }
}

/* ---------- 启动 ---------- */
async function init(isReset = false) {
  state.bootstrap = await api("/api/bootstrap");
  const savedProvider = localStorage.getItem("provider");
  if (!isReset && savedProvider) $("provider").value = savedProvider;
  updateProviderHint();

  state.date = $("date").value || state.bootstrap.suggested_date;
  $("date").value = state.date;
  if (!isReset) {
    state.materialId = state.materialId || state.bootstrap.materials[0]?.id || null;
    $("max_steps").value = state.bootstrap.max_steps || 10;
  }
  renderMaterials();
  await loadSlots();
}

function bindEvents() {
  $("run").onclick = runAgent;
  $("reset").onclick = resetData;
  $("date").onchange = async () => {
    state.date = $("date").value;
    state.hour = null;
    await loadSlots();
  };
  $("provider").onchange = () => {
    localStorage.setItem("provider", $("provider").value);
    updateProviderHint();
  };
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => switchTab(tab.dataset.tab);
  });
}

(async function boot() {
  bindEvents();
  try {
    await init();
    await loadDrafts();
    await loadMaterialDetail(state.materialId);
    switchTab("result");

    const params = new URLSearchParams(location.search);
    const materialParam = params.get("material");
    if (materialParam && state.bootstrap.materials.some((item) => item.id === materialParam)) {
      await openMaterial(materialParam);
    }
    const tabParam = params.get("tab");
    if (["result", "drafts", "material"].includes(tabParam)) switchTab(tabParam);

    await autoRunFromQuery();
  } catch (error) {
    showError(`初始化失败：${error.message}`);
  }
})();

/* 可分享的预填链接：/?auto=1&material=M01&date=2026-11-05&hour=18&constraint=soft */
async function autoRunFromQuery() {
  const params = new URLSearchParams(location.search);
  if (params.get("auto") !== "1") return;
  const material = params.get("material");
  if (material && state.bootstrap.materials.some((item) => item.id === material)) {
    state.materialId = material;
    renderMaterials();
  }
  const date = params.get("date");
  if (date) {
    state.date = date;
    $("date").value = date;
    await loadSlots();
  }
  const hour = params.get("hour");
  if (hour !== null) {
    const value = Number(hour);
    if (state.slots.some((slot) => Number(slot.slot.slice(11, 13)) === value && slot.state !== "closed")) {
      state.hour = value;
      renderSlots();
    }
  }
  const constraint = params.get("constraint");
  if (constraint === "hard" || constraint === "soft") {
    document.querySelector(`input[name="constraint"][value="${constraint}"]`).checked = true;
  }
  if (params.get("provider") === "deepseek") {
    $("provider").value = "deepseek";
    updateProviderHint();
  }
  await runAgent();
  history.replaceState(null, "", location.pathname);
}
