import { esc } from "./utils.js";
import { neuronSvg } from "./identity-neuron.js";

export class LegacyBindingPage {
  constructor(state, api, peek, container) {
    const view = container || document.getElementById("bindings-container");
    let generation = 0;
    let busy = false;
    const get = (path, params) => api.get(path, params);
    const post = async (path, body) => {
      if (busy) throw new Error("绑定变更正在保存，请稍候");
      busy = true;
      try { return await api.post(path, body); }
      finally { busy = false; }
    };
    const askToast = (message, kind) => window.lmShowToast(message, kind === "err");
    const askConfirm = async (message, options = {}) => {
      // 此页通常没有打开详情侧栏，确认前必须解除 inert 并展开面板。
      peek.open();
      try {
        const decision = peek.showConfirmDialog("确认绑定变更", message, {
          confirmLabel: options.ok || "确认", destructive: options.ok === "撤销",
        });
        document.getElementById("confirm-ok-btn")?.focus();
        return await decision;
      } finally { peek.close(); }
    };
    const fmtTime = ts => ts ? new Date(Number(ts) * 1000).toLocaleString() : "—";
    const render = async () => {
      try { await renderBindings(); }
      catch (error) {
        view.textContent = "绑定管理加载失败：" + error.message;
        const button = document.createElement("button");
        button.className = "btn btn-secondary"; button.textContent = "重试";
        button.onclick = render; view.appendChild(button);
      }
    };
const bindingFilter = { person_id: "", platform: "", status: "" };
const bindingOps = { operator: "", reviewer: "", reason: "" };
const bindingEdit = { expected: {}, values: {} };
const bindingEditFields = [
  ["person_id", "人物"], ["platform", "平台"], ["account_id", "账号"],
  ["display_name", "显示名"], ["evidence_type", "依据类型"],
  ["evidence_ref", "依据引用"], ["note", "备注"],
];
/* 上传草稿要跨重绘保留：预览拿到令牌后，执行时必须用同一份内容 */
const bindingUpload = { content: "", filename: "", mode: "merge", preview: null,
  revision: 0, fileReadId: 0, fileState: "idle" };

function bindingStatusCN(status) {
  return {
    active: "生效",
    revoked: "已撤销",
    superseded: "已被替换",
    pending: "待审核",
    approved: "已通过",
    rejected: "已驳回",
  }[status] || status || "—";
}

function bindingAuditActionCN(action) {
  return {
    create: "新增",
    update: "更新",
    merge: "更正人物",
    revoke: "撤销",
    rollback: "回滚",
    upload: "上传",
    review: "审核",
    candidate_submit: "提交待审",
    candidate_update: "更新待审",
    candidate_approve: "审核通过",
    candidate_reject: "审核驳回",
    config: "设置",
  }[action] || action || "—";
}

async function renderBindings() {
  const requestGeneration = ++generation;
  const [overview, listed, candidates, audit, settings] = await Promise.all([
    get("page/bindings/overview"),
    get("page/bindings/list", {
      person_id: bindingFilter.person_id,
      platform: bindingFilter.platform,
      status: bindingFilter.status,
    }),
    get("page/bindings/candidates", { status: "pending", limit: 100 }),
    get("page/bindings/audit", { limit: 50 }),
    get("page/bindings/settings"),
  ]);

  const items = listed.items || [];
  const platforms = overview.platforms || [];

  const bindingRows = items.length
    ? `<div class="plain-list">${items.map((row) => `<div class="atom-row" style="align-items:flex-start;gap:12px">
        <div class="grow">
          <div class="atom-text">${esc(row.person_id || "—")}${row.display_name ? ` · ${esc(row.display_name)}` : ""}</div>
          <div class="hint">${esc(row.platform || "")}:${esc(row.account_id || "")} · ${bindingStatusCN(row.status)} · v${esc(row.version || 0)}
            · 来源 ${esc(row.source || "manual")}</div>
          <div class="hint">依据 ${esc(row.evidence_type || "—")}${row.evidence_ref ? ` / ${esc(row.evidence_ref)}` : ""}
            ${row.note ? ` · 备注 ${esc(row.note)}` : ""}</div>
          <div class="hint">生效 ${fmtTime(row.valid_from)}${row.valid_to ? ` · 失效 ${fmtTime(row.valid_to)}` : ""}</div>
          <div class="hint">${esc(row.binding_id || "")}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px">
          <button class="ghost" data-bind-edit="${esc(row.binding_id)}" ${row.status === "active" ? "" : "disabled"}>编辑</button>
          <button class="ghost" data-bind-revoke="${esc(row.binding_id)}" ${row.status === "active" ? "" : "disabled"}>撤销</button>
          <div style="display:flex;gap:6px;align-items:center">
            <input type="number" min="1" step="1" value="${esc(Math.max(1, Number(row.version || 1) - 1))}"
              data-bind-version="${esc(row.binding_id)}" style="width:64px" title="回滚到哪个版本" />
            <button class="ghost" data-bind-rollback="${esc(row.binding_id)}" ${Number(row.version || 0) <= 1 ? "disabled" : ""}>回滚</button>
          </div>
        </div>
      </div>`).join("")}</div>`
    : `<p class="hint">这个筛选条件下没有绑定。${overview.active_count ? `当前共有 ${overview.active_count} 条生效绑定。` : "还没有任何绑定。"}</p>`;

  const candidateRows = (candidates.items || []).length
    ? `<div class="plain-list">${candidates.items.map((row) => `<div class="atom-row" style="align-items:flex-start;gap:12px">
        <div class="grow">
          <div class="atom-text">${esc(row.person_id || "—")}${row.display_name ? ` · ${esc(row.display_name)}` : ""}</div>
          <div class="hint">${esc(row.platform || "")}:${esc(row.account_id || "")} · 来源 ${esc(row.source || "auto")}
            · 提交 ${esc(row.submitted_by || "—")} · ${fmtTime(row.created_at)}</div>
          <div class="hint">依据 ${esc(row.evidence_type || "—")}${row.evidence_ref ? ` / ${esc(row.evidence_ref)}` : ""}</div>
        </div>
        <div style="display:flex;gap:6px">
          <button class="primary" data-bind-approve="${esc(row.candidate_id)}">通过</button>
          <button class="ghost" data-bind-reject="${esc(row.candidate_id)}">驳回</button>
        </div>
      </div>`).join("")}</div>`
    : `<p class="hint">没有待审核的自动识别候选。</p>`;

  const auditRows = (audit.items || []).length
    ? `<div class="plain-list">${audit.items.map((row) => `<div class="atom-row" style="align-items:flex-start">
        <div class="grow">
          <div class="atom-text">${bindingAuditActionCN(row.action)} · ${esc(row.operator_name || row.operator_id || "—")}</div>
          <div class="hint">${fmtTime(row.created_at)} · 渠道 ${esc(row.channel || "—")}
            ${row.reviewer_id ? ` · 审核 ${esc(row.reviewer_id)}` : ""}</div>
          <div class="hint">依据：${esc(row.reason || "—")}</div>
          <div class="hint">${esc(row.binding_id || "配置变更")}${row.binding_id ? ` · v${esc(row.version || 0)}` : ""}
            ${row.file_hash ? ` · 文件哈希 ${esc(row.file_hash)}` : ""}</div>
          ${row.evidence_ref ? `<div class="hint">证据：${esc(row.evidence_type || "")} ${esc(row.evidence_ref)}</div>` : ""}
        </div>
      </div>`).join("")}</div>`
    : `<p class="hint">还没有审计记录。</p>`;

  if (requestGeneration !== generation || state.page !== "bindings") return;
  view.innerHTML = `<section class="cfg-group">
    <div class="cfg-head"><h3>多账号 · 唯一用户标识</h3><p>中心人物连接当前有效账号与衍生记忆。点击「查询图谱」或「查看记忆」沿用此人物范围。</p></div>
    <div class="pad"><div class="row">
      <label class="field">唯一用户标识<input id="identity-person" list="identity-person-options" value="${esc(bindingFilter.person_id)}" placeholder="person_id" /></label>
      <datalist id="identity-person-options">${(overview.persons || []).map(p => `<option value="${esc(p)}"></option>`).join("")}</datalist>
      <button class="ghost" id="identity-load">绘制关联图</button>
      <button class="ghost" id="identity-graph">查询图谱</button>
      <button class="ghost" id="identity-memory">查看记忆</button>
    </div><div id="identity-neuron" aria-live="polite"></div></div>
  </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>绑定概览</h3><p>一个账号只对应一个人物；自动识别先进候选队列，默认逐条人工审核</p></div>
      <div class="pad">
        <div class="grid-stats">
          <div class="stat"><span class="stat-label">生效绑定</span><span class="stat-num">${overview.active_count || 0}</span></div>
          <div class="stat"><span class="stat-label">待审核</span><span class="stat-num">${overview.pending_count || 0}</span></div>
          <div class="stat"><span class="stat-label">人物</span><span class="stat-num">${overview.person_count || 0}</span></div>
          <div class="stat"><span class="stat-label">平台</span><span class="stat-num">${platforms.length}</span></div>
        </div>
        <div class="row" style="margin-top:12px">
          <div class="field"><label>操作人（编辑 / 撤销 / 回滚 / 上传必填）</label>
            <input type="text" id="bind-operator" value="${esc(bindingOps.operator)}" placeholder="谁做的这次变更" /></div>
          <div class="field"><label>审核人（可选）</label>
            <input type="text" id="bind-reviewer" value="${esc(bindingOps.reviewer)}" placeholder="复核的人" /></div>
          <div class="field"><label>依据（编辑 / 撤销 / 回滚 / 上传必填）</label>
            <input type="text" id="bind-reason" value="${esc(bindingOps.reason)}" placeholder="为什么这么改" /></div>
        </div>
        <div class="hint" style="margin-top:8px">平台：${platforms.length ? platforms.map((p) => esc(p)).join(" / ") : "—"}</div>
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>绑定列表</h3><p>按人物 / 平台 / 状态筛选；撤销是软失效，回滚会新建一版并留审计</p></div>
      <div class="pad">
        <div class="row">
          <div class="field"><label>人物</label><input type="text" id="bind-person" value="${esc(bindingFilter.person_id)}" placeholder="person_id" /></div>
          <div class="field"><label>平台</label><input type="text" id="bind-platform" list="bind-platform-list" value="${esc(bindingFilter.platform)}" placeholder="qq / bilibili …" />
            <datalist id="bind-platform-list">${platforms.map((p) => `<option value="${esc(p)}"></option>`).join("")}</datalist></div>
          <div class="field"><label>状态</label><select id="bind-status">
            ${[["", "仅生效"], ["all", "全部"], ["revoked", "已撤销"], ["superseded", "已被替换"]].map(([value, label]) => `<option value="${value}" ${bindingFilter.status === value ? "selected" : ""}>${label}</option>`).join("")}
          </select></div>
        </div>
        <div class="row" style="margin:10px 0 12px">
          <button class="ghost" id="bind-filter">筛选</button>
          <button class="ghost" id="bind-filter-reset">重置</button>
          <span class="hint" style="align-self:center">共 ${items.length} 条</span>
        </div>
        ${bindingRows}
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>手动新增 / 更正</h3><p>已有绑定请先点列表「编辑」。更正人物会保留旧版本；不修改已有记忆，所有变更留审计</p></div>
      <div class="pad">
        <div class="row">
          ${bindingEditFields.map(([key, label]) => `<div class="field"><label>${label}</label>
            <input type="text" id="bind-edit-${key}" value="${esc(bindingEdit.values[key] || "")}"
              ${bindingEdit.expected.binding_id && ["platform", "account_id"].includes(key) ? "readonly" : ""} /></div>`).join("")}
        </div>
        <div class="row" style="margin-top:10px">
          <button class="ghost" id="bind-edit-new">清空 / 新增</button>
          <button class="primary" id="bind-edit-save">保存绑定</button>
        </div>
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>自动识别待审核</h3><p>通过后才写正式绑定；驳回只改候选状态。两种都会写审计</p></div>
      <div class="pad">
        <div class="hint" style="margin-bottom:10px">审核人：${esc(bindingOps.reviewer || "请在上方填写")} · 依据：${esc(bindingOps.reason || "请在上方填写")}</div>
        ${candidateRows}
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>变更预览</h3><p>只读：影响多少条记忆、涉及哪些场景、前 10 条样本；不改任何数据</p></div>
      <div class="pad">
        <div class="row">
          <div class="field"><label>来源账号</label><input type="text" id="bind-preview-account" placeholder="如 qq:123456" /></div>
          <div class="field"><label>或人物</label><input type="text" id="bind-preview-person" placeholder="person_id" /></div>
          <button class="ghost" id="bind-preview-go" style="align-self:flex-end;margin-bottom:18px">预览影响</button>
        </div>
        <div id="bind-preview-result"><p class="hint">填来源账号或人物后点「预览影响」。</p></div>
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>上传绑定文件</h3><p>支持 JSON 数组 / {rows:[]} / {bindings:[]} 与 CSV、TSV；默认 merge，replace 必须先预览确认</p></div>
      <div class="pad">
        <div class="row">
          <div class="field"><label>文件</label><input type="file" id="bind-file" accept=".json,.csv,.tsv,.txt" /></div>
          <div class="field"><label>模式</label><select id="bind-mode">
            <option value="merge" ${bindingUpload.mode === "merge" ? "selected" : ""}>merge（只增改，不撤销）</option>
            <option value="replace" ${bindingUpload.mode === "replace" ? "selected" : ""}>replace（撤销文件里没有的绑定）</option>
          </select></div>
        </div>
        <div class="field"><label>或直接粘贴内容</label>
          <textarea id="bind-content" rows="6" placeholder='[{"person_id":"p1","platform":"qq","account_id":"123456","display_name":"小绫"}]'>${esc(bindingUpload.content)}</textarea></div>
        <div class="row" style="margin:10px 0">
          <button class="ghost" id="bind-upload-preview">预览</button>
          <button class="primary" id="bind-upload-go">执行上传</button>
          <label class="switch" style="align-self:center"><input type="checkbox" id="bind-preview-confirmed" /> replace 已确认预览</label>
        </div>
        <div id="bind-upload-result">${bindingUpload.preview ? bindingUploadResultHtml(bindingUpload.preview) : '<p class="hint">先点「预览」，看清楚新增 / 更新 / 冲突 / 将撤销多少条，再执行。</p>'}</div>
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>自动审核开关</h3><p>${esc(settings.risk_notice || "")}</p></div>
      <div class="pad">
        <div class="row">
          <div class="field"><label>auto_approve</label>
            <label class="switch"><input type="checkbox" id="bind-auto" ${settings.auto_approve ? "checked" : ""} /> 开启</label></div>
          <div class="field"><label>理由（开启必填）</label>
            <input type="text" id="bind-auto-reason" value="${esc(settings.auto_approve_reason || "")}" /></div>
          <div class="field"><label>风险确认</label>
            <label class="switch"><input type="checkbox" id="bind-auto-risk" /> 我已了解风险</label></div>
        </div>
        <div class="hint" style="margin-top:8px">最后修改：${esc(settings.updated_by || "—")} · ${fmtTime(settings.updated_at)}</div>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="bind-auto-save">保存开关</button>
          <span class="hint" id="bind-auto-msg" style="align-self:center"></span>
        </div>
      </div>
    </section>

    <section class="cfg-group">
      <div class="cfg-head"><h3>审计记录</h3><p>最近 50 条：操作人、依据、审核人、文件哈希；只追加，不可改</p></div>
      <div class="pad">${auditRows}</div>
    </section>`;

  const selectedPerson = () => view.querySelector("#identity-person").value.trim();
  const drawIdentity = async () => {
    const person = selectedPerson();
    const box = view.querySelector("#identity-neuron");
    if (!person) { box.textContent = "请选择或输入唯一用户标识。"; return; }
    box.textContent = "正在读取有效账号与记忆…";
    try {
      const data = await get("page/bindings/neuron", {person_id: person});
      if (requestGeneration !== generation || state.page !== "bindings" || selectedPerson() !== person) return;
      box.innerHTML = neuronSvg(data);
    } catch (error) { box.textContent = error.message; }
  };
  view.querySelector("#identity-load").onclick = drawIdentity;
  for (const target of ["graph", "memory"]) {
    view.querySelector("#identity-" + target).onclick = async () => {
      const person = selectedPerson();
      if (!person) { askToast("请先输入唯一用户标识", "err"); return; }
      await window.lmSwitchPage(target);
      const input = document.getElementById(target === "graph" ? "graph-person-filter" : "mem-person");
      input.value = person;
      if (target === "graph") document.getElementById("graph-search-btn").click();
      else document.getElementById("memory-filters").requestSubmit();
    };
  }
  if (selectedPerson()) drawIdentity();
  else view.querySelector("#identity-neuron").textContent = "选择人物后显示二维关联图；没有数据时不会生成示例绑定。";
  const readOps = () => {
    bindingOps.operator = (document.getElementById("bind-operator") || {}).value || "";
    bindingOps.reviewer = (document.getElementById("bind-reviewer") || {}).value || "";
    bindingOps.reason = (document.getElementById("bind-reason") || {}).value || "";
  };
  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };

  const fillBindingEdit = (row = {}) => {
    bindingEdit.expected = { ...row };
    bindingEdit.values = { ...row };
    bindingEditFields.forEach(([key]) => {
      const input = document.getElementById(`bind-edit-${key}`);
      if (input) {
        input.value = row[key] || "";
        input.readOnly = !!row.binding_id && ["platform", "account_id"].includes(key);
      }
    });
  };
  view.querySelectorAll("[data-bind-edit]").forEach((btn) => {
    btn.onclick = () => fillBindingEdit(items.find((row) => row.binding_id === btn.dataset.bindEdit));
  });
  bindingEditFields.forEach(([key]) => {
    const input = document.getElementById(`bind-edit-${key}`);
    if (input) input.oninput = () => { bindingEdit.values[key] = input.value; };
  });
  bind("bind-edit-new", () => fillBindingEdit());
  bind("bind-edit-save", async (event) => {
    readOps();
    const values = {};
    bindingEditFields.forEach(([key]) => {
      values[key] = ((document.getElementById(`bind-edit-${key}`) || {}).value || "").trim();
    });
    if (!values.person_id || !values.platform || !values.account_id || !bindingOps.operator.trim() || !bindingOps.reason.trim()) {
      askToast("请填写人物、平台、账号、操作人和依据", "err");
      return;
    }
    const payload = { ...values, ...bindingOps, expected_binding: { ...bindingEdit.expected } };
    event.target.disabled = true;
    try {
      if (!(await askConfirm("保存绑定？人物变更会生成新版本，不修改已有记忆。", { ok: "保存" }))) return;
      await post("page/bindings/save", payload);
      fillBindingEdit();
      bindingUpload.preview = null;
      askToast("绑定已保存", "ok");
      render();
    } catch (err) { askToast(err.message, "err"); }
    finally { event.target.disabled = false; }
  });

  bind("bind-filter", () => {
    bindingFilter.person_id = (document.getElementById("bind-person") || {}).value || "";
    bindingFilter.platform = (document.getElementById("bind-platform") || {}).value || "";
    bindingFilter.status = (document.getElementById("bind-status") || {}).value || "";
    render();
  });
  bind("bind-filter-reset", () => {
    bindingFilter.person_id = "";
    bindingFilter.platform = "";
    bindingFilter.status = "";
    render();
  });

  view.querySelectorAll("[data-bind-revoke]").forEach((btn) => {
    btn.onclick = async () => {
      readOps();
      if (!bindingOps.operator || !bindingOps.reason) {
        askToast("撤销要填操作人和依据", "err");
        return;
      }
      if (!(await askConfirm(`撤销这条绑定？会软失效并写审计，历史版本仍可回滚。`, { ok: "撤销" }))) return;
      try {
        await post("page/bindings/revoke", {
          binding_id: btn.dataset.bindRevoke,
          operator: bindingOps.operator,
          reviewer: bindingOps.reviewer,
          reason: bindingOps.reason,
        });
        askToast("已撤销", "ok");
        render();
      } catch (err) { askToast(err.message, "err"); }
    };
  });
  view.querySelectorAll("[data-bind-rollback]").forEach((btn) => {
    btn.onclick = async () => {
      readOps();
      if (!bindingOps.operator || !bindingOps.reason) {
        askToast("回滚要填操作人和依据", "err");
        return;
      }
      const bindingId = btn.dataset.bindRollback;
      const versionInput = document.querySelector(`[data-bind-version="${bindingId}"]`);
      const toVersion = Number(versionInput ? versionInput.value : 0);
      if (!toVersion) { askToast("请填写要回到的版本号", "err"); return; }
      if (!(await askConfirm(`回滚到 v${toVersion}？会新建一版生效绑定，原记录保留。`, { ok: "回滚" }))) return;
      try {
        await post("page/bindings/rollback", {
          binding_id: bindingId,
          to_version: toVersion,
          operator: bindingOps.operator,
          reviewer: bindingOps.reviewer,
          reason: bindingOps.reason,
        });
        askToast("已回滚", "ok");
        render();
      } catch (err) { askToast(err.message, "err"); }
    };
  });

  const review = async (candidateId, approve) => {
    readOps();
    if (!bindingOps.reviewer || !bindingOps.reason) {
      askToast("审核要填审核人和依据（可复用上方输入）", "err");
      return;
    }
    // 绑定本次页面展示的完整快照，不在点击时另取可能已经换人的候选。
    const expectedCandidate = (candidates.items || []).find((row) => row.candidate_id === candidateId);
    if (!expectedCandidate) { askToast("候选已变化，请刷新后重新审核", "err"); return; }
    if (!(await askConfirm(`${approve ? "通过" : "驳回"}这条候选？`, { ok: approve ? "通过" : "驳回" }))) return;
    try {
      await post("page/bindings/candidates/review", {
        candidate_id: candidateId,
        expected_candidate: expectedCandidate,
        approve,
        reviewer: bindingOps.reviewer,
        reason: bindingOps.reason,
      });
      askToast(approve ? "已通过，正式绑定已写入" : "已驳回", "ok");
      render();
    } catch (err) { askToast(err.message, "err"); }
  };
  view.querySelectorAll("[data-bind-approve]").forEach((btn) => {
    btn.onclick = () => review(btn.dataset.bindApprove, true);
  });
  view.querySelectorAll("[data-bind-reject]").forEach((btn) => {
    btn.onclick = () => review(btn.dataset.bindReject, false);
  });

  bind("bind-preview-go", async (event) => {
    const box = document.getElementById("bind-preview-result");
    const sourceAccount = (document.getElementById("bind-preview-account") || {}).value || "";
    const personId = (document.getElementById("bind-preview-person") || {}).value || "";
    if (!sourceAccount && !personId) {
      if (box) box.innerHTML = '<p class="hint">填来源账号或人物后再预览。</p>';
      return;
    }
    event.target.disabled = true;
    try {
      const data = await get("page/bindings/preview", { source_account: sourceAccount, person_id: personId, sample_limit: 10 });
      const audience = Object.entries(data.by_audience || {});
      if (box) {
        box.innerHTML = `<p class="hint">影响 ${data.total || 0} 条记忆；按当前有效绑定与可验证的账号来源统计，纯群记忆不猜归属。</p>
          ${audience.length ? `<p class="hint">场景分布：${audience.map(([key, count]) => `${esc(key)} ${count}`).join(" / ")}</p>` : ""}
          ${(data.samples || []).length ? `<div class="plain-list">${data.samples.map((row) => `<div class="atom-row"><div class="grow">
              <div class="atom-text">${esc(row.content || "")}</div>
              <div class="hint">#${esc(row.memory_id)} · ${esc(row.audience || "unknown")} · 敏感 ${esc(row.sensitivity || 0)}</div>
            </div></div>`).join("")}</div>` : '<p class="hint">没有可展示的样本。</p>'}`;
      }
    } catch (err) {
      if (box) box.innerHTML = `<p class="hint">预览失败：${esc(err.message)}</p>`;
    } finally { event.target.disabled = false; }
  });

  const readUploadDraft = () => {
    bindingUpload.content = (document.getElementById("bind-content") || {}).value || "";
    bindingUpload.mode = (document.getElementById("bind-mode") || {}).value || "merge";
    readOps();
  };
  const invalidateUploadPreview = () => {
    bindingUpload.revision += 1;
    bindingUpload.preview = null;
    const confirmed = document.getElementById("bind-preview-confirmed");
    if (confirmed) confirmed.checked = false;
    const result = document.getElementById("bind-upload-result");
    if (result) result.textContent = "内容或模式已变化，请重新预览。";
  };
  const uploadContent = document.getElementById("bind-content");
  const uploadMode = document.getElementById("bind-mode");
  const fileUnavailable = () => ["loading", "error"].includes(bindingUpload.fileState);
  const syncUploadAvailability = () => {
    ["bind-upload-preview", "bind-upload-go", "bind-preview-confirmed"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = fileUnavailable();
    });
  };
  if (uploadContent) uploadContent.oninput = () => {
    // 手动输入成为新草稿，取消尚未结束的文件读取及其文件名。
    bindingUpload.fileReadId += 1;
    bindingUpload.fileState = "idle";
    bindingUpload.filename = "";
    const input = document.getElementById("bind-file");
    if (input) input.value = "";
    readUploadDraft();
    invalidateUploadPreview();
    syncUploadAvailability();
  };
  if (uploadMode) uploadMode.onchange = () => { readUploadDraft(); invalidateUploadPreview(); };
  const fileInput = document.getElementById("bind-file");
  if (fileInput) {
    fileInput.onchange = () => {
      const file = fileInput.files && fileInput.files[0];
      const readId = ++bindingUpload.fileReadId;
      invalidateUploadPreview();
      bindingUpload.fileState = file ? "loading" : "idle";
      bindingUpload.content = "";
      bindingUpload.filename = "";
      const area = document.getElementById("bind-content");
      if (area) area.value = "";
      syncUploadAvailability();
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        // 只接受最新一次选取；旧读取的成功、失败和取消回调均不得覆盖新草稿。
        if (readId !== bindingUpload.fileReadId) return;
        const area = document.getElementById("bind-content");
        if (area) area.value = String(reader.result || "");
        bindingUpload.filename = file.name || "";
        bindingUpload.fileState = "idle";
        readUploadDraft();
        invalidateUploadPreview();
        syncUploadAvailability();
        askToast("文件已读入，记得先预览", "ok");
      };
      reader.onerror = reader.onabort = () => {
        if (readId !== bindingUpload.fileReadId) return;
        bindingUpload.fileState = "error";
        invalidateUploadPreview();
        syncUploadAvailability();
        askToast("读取文件失败，请重新选择文件或粘贴内容", "err");
      };
      try { reader.readAsText(file); } catch (err) { reader.onerror(); }
    };
  }
  syncUploadAvailability();

  bind("bind-upload-preview", async (event) => {
    if (fileUnavailable()) { askToast("文件未读入，请重新选择文件或粘贴内容", "err"); return; }
    readUploadDraft();
    const box = document.getElementById("bind-upload-result");
    if (!bindingUpload.content.trim()) {
      if (box) box.innerHTML = '<p class="hint">请选择文件或粘贴内容。</p>';
      return;
    }
    event.target.disabled = true;
    invalidateUploadPreview();
    const revision = bindingUpload.revision;
    const draft = { mode: bindingUpload.mode, content: bindingUpload.content, filename: bindingUpload.filename };
    try {
      const data = await post("page/bindings/upload/preview", draft);
      if (revision !== bindingUpload.revision || fileUnavailable()) return;
      readUploadDraft();
      if (draft.content !== bindingUpload.content || draft.mode !== bindingUpload.mode || draft.filename !== bindingUpload.filename) return;
      bindingUpload.preview = data;
      bindingUpload.previewContent = draft.content;
      bindingUpload.previewMode = draft.mode;
      bindingUpload.previewFilename = draft.filename;
      if (box) box.innerHTML = bindingUploadResultHtml(data);
    } catch (err) {
      if (revision !== bindingUpload.revision) return;
      bindingUpload.preview = null;
      if (box) box.innerHTML = `<p class="hint">预览失败：${esc(err.message)}</p>`;
    } finally { event.target.disabled = fileUnavailable(); }
  });

  bind("bind-upload-go", async (event) => {
    if (fileUnavailable()) { askToast("文件未读入，请重新选择文件或粘贴内容", "err"); return; }
    readUploadDraft();
    if (!bindingOps.operator || !bindingOps.reason) {
      askToast("上传要填操作人和依据", "err");
      return;
    }
    if (!bindingUpload.content.trim()) {
      askToast("请先选择文件或粘贴内容", "err");
      return;
    }
    if (!bindingUpload.preview) {
      askToast("请先预览，确认新增 / 更新 / 冲突 / 将撤销数量", "err");
      return;
    }
    if (bindingUpload.previewContent !== bindingUpload.content || bindingUpload.previewMode !== bindingUpload.mode || bindingUpload.previewFilename !== bindingUpload.filename) {
      askToast("文件或模式变了，请重新预览", "err");
      return;
    }
    if (bindingUpload.preview.can_apply === false) {
      askToast("请先修正预览中的错误或冲突", "err");
      return;
    }
    const replaceConfirmed = !!(document.getElementById("bind-preview-confirmed") || {}).checked;
    if (bindingUpload.mode === "replace" && !replaceConfirmed) {
      askToast("replace 需要先勾选「已确认预览」", "err");
      return;
    }
    const payload = {
      mode: bindingUpload.mode, content: bindingUpload.content, filename: bindingUpload.filename,
      ...bindingOps, preview_confirmed: replaceConfirmed,
      preview_token: bindingUpload.preview.preview_token || "",
    };
    const confirmText = `按 ${bindingUpload.mode} 上传 ${bindingUpload.preview.rows} 行？` +
      (bindingUpload.mode === "replace" ? `将撤销 ${bindingUpload.preview.revoke_count || 0} 条未出现的绑定。` : "");
    const revision = bindingUpload.revision;
    event.target.disabled = true;
    try {
      if (!(await askConfirm(confirmText, { ok: "上传" }))) return;
      if (revision !== bindingUpload.revision || fileUnavailable()) {
        askToast("草稿已变化，请重新预览", "err");
        return;
      }
      const result = await post("page/bindings/upload", payload);
      askToast(`已上传：新增 ${result.created || 0} / 更新 ${result.updated || 0} / 冲突 ${(result.conflicts || []).length} / 错误 ${(result.errors || []).length}`, "ok");
      if (revision !== bindingUpload.revision) return;
      bindingUpload.preview = null;
      bindingUpload.content = "";
      bindingUpload.filename = "";
      render();
    } catch (err) { askToast(err.message, "err"); }
    finally { event.target.disabled = fileUnavailable(); }
  });

  bind("bind-auto-save", async () => {
    readOps();
    const enabled = !!((document.getElementById("bind-auto") || {}).checked);
    const reason = (document.getElementById("bind-auto-reason") || {}).value || "";
    const riskAck = !!((document.getElementById("bind-auto-risk") || {}).checked);
    const msg = document.getElementById("bind-auto-msg");
    if (enabled && !reason.trim()) {
      if (msg) msg.textContent = "开启必须写理由";
      return;
    }
    if (enabled && !riskAck) {
      if (msg) msg.textContent = "请先勾选「我已了解风险」";
      return;
    }
    if (!bindingOps.operator) {
      if (msg) msg.textContent = "请填写操作人";
      return;
    }
    try {
      await post("page/bindings/settings/save", {
        auto_approve: enabled,
        reason,
        operator: bindingOps.operator,
        reviewer: bindingOps.reviewer,
        risk_ack: riskAck,
      });

      if (msg) msg.textContent = "已保存";
      askToast("设置已保存", "ok");
    } catch (err) {
      if (msg) msg.textContent = err.message;
    }
  });
}

/* 上传预览结果的统一渲染（预览和上传后都走这里） */
function bindingUploadResultHtml(data) {
  const conflictRows = (data.conflicts || []).length
    ? `<div class="plain-list">${data.conflicts.map((row) => `<div class="atom-row"><div class="grow">
        <div class="atom-text">${esc(row.person_id || "—")} · ${esc(row.platform || "")}:${esc(row.account_id || "")}</div>
        <div class="hint">${esc(row.reason || "冲突")}${row.current_person_id ? `（现绑定：${esc(row.current_person_id)}）` : ""}</div>
      </div></div>`).join("")}</div>`
    : '<p class="hint">没有冲突行。</p>';
  const revokeRows = (data.revoke_items || []).length
    ? `<div class="plain-list">${data.revoke_items.map((row) => `<div class="atom-row"><div class="grow">
        <div class="atom-text">${esc(row.person_id || "—")} · ${esc(row.platform || "")}:${esc(row.account_id || "")}</div>
        <div class="hint">${esc(row.display_name || "")} · ${esc(row.binding_id || "")}</div>
      </div></div>`).join("")}</div>`
    : "";
  return `<div class="grid-stats">
      <div class="stat"><span class="stat-label">文件行数</span><span class="stat-num">${data.rows || 0}</span></div>
      <div class="stat"><span class="stat-label">新增</span><span class="stat-num">${data.created || 0}</span></div>
      <div class="stat"><span class="stat-label">更新</span><span class="stat-num">${data.updated || 0}</span></div>
      <div class="stat"><span class="stat-label">无变化</span><span class="stat-num">${data.unchanged || 0}</span></div>
    </div>
    <p class="hint" style="margin-top:10px">模式 ${esc(data.mode || "")} · 冲突 ${(data.conflicts || []).length} 条
      · 将撤销 ${data.revoke_count || 0} 条 · 文件哈希 ${esc((data.file_hash || "").slice(0, 16))}…</p>
    ${data.can_apply === false ? '<p class="hint">此预览不可执行，请先修正错误或冲突后重新预览。</p>' : ""}
    ${(data.errors || []).length ? `<p class="hint">${data.can_apply === false ? "错误" : "跳过"} ${data.errors.length} 项：${esc(data.errors.slice(0, 5).join("；"))}${data.errors.length > 5 ? "…" : ""}</p>` : ""}
    ${(data.warnings || []).map((item) => `<p class="hint">${esc(item)}</p>`).join("")}
    <p class="hint" style="margin-top:8px">冲突行不会被覆盖，也不会自动加入候选队列；请从列表编辑更正后重新预览。</p>
    ${conflictRows}
    ${revokeRows ? `<p class="hint" style="margin-top:8px">replace 将撤销：</p>${revokeRows}` : ""}`;
}



    this.fetch = render;
    this.leave = () => { generation++; };
    Object.defineProperty(this, "busy", {get: () => busy});
  }
}
