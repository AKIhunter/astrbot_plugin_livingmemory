import { esc } from "./utils.js";
import { IdentityForce, identityGraphModel } from "./identity-force.js";

const types = { human: "人", bot: "机器人", unknown: "类型待确认" };
const statuses = { pending: "待审核", approved: "已批准", rejected: "已驳回", cancelled: "已取消", expired: "已过期", conflict: "归属冲突" };
const accountKey = a => `${a.platform}:${a.account_id}`;
const clone = v => JSON.parse(JSON.stringify(v));
const uid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const button = (id, label, disabled = false) => `<button type="button" id="${id}" ${disabled ? "disabled" : ""}>${label}</button>`;
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString() : "—";

export class BindingPage {
  constructor(state, api, peek) {
    this.state = state; this.api = api; this.peek = peek;
    this.view = document.getElementById("bindings-container");
    this.tab = "graph"; this.selected = ""; this.mode = "subject";
    this.entities = []; this.entityCursor = ""; this.applications = []; this.appCursor = "";
    this.applicationStatus = "pending"; this.pending = []; this.history = []; this.projection = null;
    this.epoch = 0; this.readGeneration = 0; this._busy = false; this.loading = false;
    this.expanded = new Set(); this.forces = []; this.message = ""; this.error = false;
    this.showPending = false; this.showHistory = false;
    this.draft = { platform: "qq", account_id: "", display_name: "", evidence: "", correction: false };
    this.registration = { display_name: "", entity_type: "human" };
    this.review = null; this.reviewReason = ""; this.preview = null; this.unlinkReason = "";
    this.reviewDrafts = new Map();
    this.unlinkAccount = ""; this.acknowledged = false; this.correctionPreview = null;
    this.keys = new Map();
  }
  get busy() { return this._busy || !!this.legacy?.busy; }
  alive(token) { return token === this.epoch && this.state.page === "bindings"; }
  stopGraphs() { this.forces.forEach(f => f.dispose()); this.forces = []; this.semanticForce = null; }
  leave() {
    this.epoch++; this.readGeneration++; this.stopGraphs(); this.legacy?.leave();
    this.preview = null; this.correctionPreview = null; this.review = null; this.acknowledged = false;
    this.loading = false;
  }
  entity() { return this.entities.find(e => e.one_id === this.selected); }
  async fetch() {
    if (this.busy) return;
    const token = ++this.epoch, read = ++this.readGeneration;
    this.preview = null; this.correctionPreview = null; this.review = null;
    this.loading = true; this.render();
    try {
      const entities = await this.api.get("page/identity/entities", { limit: 100 });
      if (!this.alive(token) || read !== this.readGeneration) return;
      this.entities = entities.items || []; this.entityCursor = entities.next_cursor || "";
      if (!this.entity()) this.selected = this.entities[0]?.one_id || "";
      if (this.entity()?.entity_type !== "bot") this.mode = "subject";
      await this.readData(token, read);
    } catch (e) { if (this.alive(token) && read === this.readGeneration) this.feedback(e.message, true); }
    finally { if (this.alive(token) && read === this.readGeneration) { this.loading = false; this.render(); } }
  }
  async readData(token = this.epoch, read = this.readGeneration) {
    const scope = this.mode === "owner" ? { owner_one_id: this.selected } : { one_id: this.selected };
    const selected = this.selected;
    const [projection, pending, applications, history] = await Promise.all([
      selected ? this.api.get("page/identity/projection", { ...scope, limit: 100 }) : null,
      this.api.get("page/identity/applications", { status: "pending", limit: 100 }),
      this.api.get("page/identity/applications", { status: this.applicationStatus, limit: 10 }),
      selected ? this.api.get("page/bindings/list", { person_id: selected, status: "revoked" }) : { items: [] },
    ]);
    if (!this.alive(token) || read !== this.readGeneration || selected !== this.selected) return;
    this.projection = projection; this.pending = pending.items || [];
    const entity = this.entity();
    if (entity && projection) entity.account_count = projection.accounts?.length || 0;
    this.pendingMore = !!pending.next_cursor; this.applications = applications.items || [];
    this.appCursor = applications.next_cursor || ""; this.history = history.items || [];
    if (!(projection?.accounts || []).some(a => a.binding_id === this.unlinkAccount))
      this.unlinkAccount = projection?.accounts?.[0]?.binding_id || "";
    this.preview = null; this.acknowledged = false; this.review = null;
  }
  async refresh() {
    if (this.busy) return;
    const token = this.epoch, read = ++this.readGeneration;
    this.loading = true; this.render();
    try { await this.readData(token, read); }
    catch (e) { if (this.alive(token) && read === this.readGeneration) this.feedback(e.message, true); }
    finally { if (this.alive(token) && read === this.readGeneration) { this.loading = false; this.render(); } }
  }
  feedback(message, error = false) { this.message = message; this.error = error; }
  async mutate(path, payload, success, { confirm = "", onSuccess = () => {}, onConflict = () => {} } = {}) {
    if (this.busy || this.loading) return;
    const token = this.epoch, fingerprint = path + JSON.stringify(payload);
    if (!this.keys.has(fingerprint)) this.keys.set(fingerprint, uid());
    this._busy = true; this.render();
    let saved = false;
    try {
      if (confirm && !await this.confirm(confirm)) return;
      if (!this.alive(token)) return;
      const result = await this.api.post(path, { ...payload, idempotency_key: this.keys.get(fingerprint) });
      if (!this.alive(token)) return;
      saved = true;
      onSuccess(result); this.feedback(success);
      await this.readData(token, ++this.readGeneration);
    } catch (e) {
      if (!this.alive(token)) return;
      const conflict = e.code === "identity_conflict" || e.status === 409 || /冲突|过期|变化|conflict/i.test(e.message);
      if (conflict) { onConflict(); this.feedback(e.message + "。请刷新后核对；草稿已保留。", true); }
      else this.feedback(saved ? success + " 刷新失败：" + e.message + "，请刷新。" : e.message + "。草稿已保留，可重试。", true);
    } finally { this._busy = false; if (this.alive(token)) this.render(); }
  }
  async confirm(message) {
    this.peek.open();
    try {
      const promise = this.peek.showConfirmDialog("核对关联变更", message, { confirmLabel: "确认", destructive: this.tab === "unlink" });
      document.getElementById("confirm-ok-btn")?.focus();
      return await promise;
    } finally { this.peek.close(); }
  }
  render() {
    this.stopGraphs(); this.legacy?.leave();
    const oldAdvanced = this.view.querySelector?.("#oneid-advanced");
    if (oldAdvanced) this.advancedOpen = oldAdvanced.open;
    const oldRegister = this.view.querySelector?.(".oneid-register");
    if (oldRegister) this.registerOpen = oldRegister.open;
    const focused = document.activeElement?.id;
    const e = this.entity(), disabled = this.busy || this.loading;
    this.view.innerHTML = `<nav class="oneid-tabs" role="tablist" aria-label="用户管理模块">
      ${[["graph", "用户关联图谱"], ["bind", "用户绑定"], ["unlink", "用户解绑"]].map(([key, text]) =>
        `<button id="oneid-tab-${key}" type="button" role="tab" data-tab="${key}" aria-selected="${this.tab === key}" aria-controls="oneid-panel" tabindex="${this.tab === key ? 0 : -1}" ${this.busy ? "disabled" : ""}>${text}</button>`).join("")}
      </nav>
      <div class="oneid-toolbar">
        <label class="field">主体（人 / 机器人）<select id="oneid-entity" ${disabled ? "disabled" : ""}>
          ${this.entities.length ? this.entities.map(e => `<option value="${esc(e.one_id)}" ${e.one_id === this.selected ? "selected" : ""}>${esc(e.display_name || e.one_id)} · ${types[e.entity_type] || types.unknown} · ${Number(e.account_count) || 0} 账号 · ${esc(e.one_id.length > 20 ? e.one_id.slice(0, 18) + "…" : e.one_id)}</option>`).join("") : '<option value="">尚未登记主体</option>'}
        </select></label>
        ${button("oneid-refresh", "刷新", disabled)}
        ${this.entityCursor ? button("oneid-entities-more", "加载更多主体", disabled) : ""}
      </div>
      ${e ? `<div class="oneid-subject"><p><strong>${esc(e.display_name || e.one_id)}</strong> · ${types[e.entity_type] || types.unknown} <span>oneid：${esc(e.one_id)}</span></p>${button("oneid-copy", "复制 oneid")}</div>` : ""}
      <p id="oneid-feedback" role="${this.error ? "alert" : "status"}" class="oneid-feedback ${this.error ? "is-error" : ""}" ${this.message ? "" : "hidden"}>${esc(this.message)}</p>
      <section id="oneid-panel" role="tabpanel" aria-labelledby="oneid-tab-${this.tab}" aria-busy="${this.loading}">
        ${this.loading ? '<p class="hint">正在读取身份与关联…</p>' : ""}
        ${this.tab === "graph" ? this.graphHtml() : this.tab === "bind" ? this.bindHtml() : this.unlinkHtml()}
      </section>
      <details class="oneid-advanced" id="oneid-advanced" ${this.advancedOpen ? "open" : ""}>
        <summary>高级兼容管理 · 上传、旧版历史与审计</summary>
        <p class="hint">这里保留旧客户端的直接写入流程。旧自动审核设置只影响兼容入口；上方新申请始终需要人工审核。</p>
        <div id="oneid-legacy"></div>
      </details>`;
    if (this.legacyContainer) this.view.querySelector("#oneid-legacy").replaceWith(this.legacyContainer);
    this.wire();
    if (disabled) this.view.querySelectorAll("form input, form textarea, form select, form button, #oneid-advanced").forEach(node => {
      if (node.tagName === "DETAILS") node.hidden = true;
      else node.disabled = true;
    });
    if (!this.loading && this.tab === "graph" && e && this.projection) this.drawGraph();
    if (focused) document.getElementById(focused)?.focus({ preventScroll: true });
  }
  graphHtml() {
    const p = this.projection, e = this.entity();
    if (!e) return '<div class="oneid-empty"><h3>先登记一个主体</h3><p>人和机器人都可以拥有多个平台账号。到“用户绑定”登记后，再提交账号申请。</p></div>';
    if (!p) return '<p class="hint">尚未读取关联，点击刷新重试。</p>';
    return `<div class="oneid-graph-head">
      <div><h3>平台账号与场景</h3><p class="hint">同一账号在不同群聊或私聊中只绑定一次。</p></div>
      ${e.entity_type === "bot" ? `<label class="field">记忆视角<select id="oneid-mode" ${this.busy || this.loading ? "disabled" : ""}><option value="subject" ${this.mode === "subject" ? "selected" : ""}>机器人自身相关记忆</option><option value="owner" ${this.mode === "owner" ? "selected" : ""}>机器人持有记忆（含用户相关）</option></select></label>` : ""}
    </div>
    <div class="oneid-stats"><span><strong>${p.accounts?.length || 0}</strong> 有效账号</span><span><strong>${Number(p.attributed_memory_count) || 0}</strong> ${this.mode === "owner" ? "持有记录" : "归属记录"}</span><span><strong>${Number(p.uncertain_memory_count) || 0}</strong> 待复核历史</span></div>
    <p class="hint">当前可召回数：${p.recallable_memory_count == null ? "未提供实际对话，未计算" : esc(p.recallable_memory_count)}。归属统计不代表对话读取权限。</p>
    <div class="oneid-legend"><span class="active">实线 · 已生效</span><span class="pending">虚线 · 待审核</span><span class="revoked">点线 · 已解绑</span></div>
    <div class="oneid-history-controls"><label class="oneid-check"><input id="oneid-show-pending" type="checkbox" ${this.showPending ? "checked" : ""}>显示待审申请</label><label class="oneid-check"><input id="oneid-show-history" type="checkbox" ${this.showHistory ? "checked" : ""}>显示已解绑历史</label></div>
    <div id="oneid-force"></div><p id="oneid-layout-status" class="hint" role="status"></p>
    <div class="oneid-graph-grid">
      <section><h3>账号与场景列表</h3><div id="oneid-accounts">${this.accountListHtml()}</div></section>
      <section><h3>${this.mode === "owner" ? "持有记忆的来源" : "归属记忆的来源"}</h3>
        <p class="hint">待复核：${esc(p.uncertain_count_scope || "来源不明确，不自动归给此主体")}。</p>
        <div id="oneid-records">${this.recordsHtml()}</div>
        ${p.has_more ? button("oneid-records-more", "加载更多来源", this.busy || this.loading) : ""}
        <div id="oneid-semantic" aria-live="polite"></div>
      </section>
    </div>`;
  }
  accountListHtml() {
    const p = this.projection;
    const accounts = p.accounts || [];
    const html = accounts.map(a => {
      const key = accountKey(a), open = this.expanded.has(key);
      const scenes = new Set((p.records || []).filter(r => (this.mode === "owner" ? r.owner_account : r.source_account) === key).map(r => r.scene_key));
      return `<div class="oneid-account"><button type="button" data-expand="${esc(key)}" aria-expanded="${open}">${open ? "−" : "＋"} ${esc(key)} <span>已生效</span></button>
        ${open ? `<p class="hint">关联来源：${a.source === "auto" ? "自动识别后审核" : "手动或兼容登记"} · 当前关系 v${esc(a.version || 1)}<br>依据：${esc(a.evidence_ref || a.note || "未记录")}<br>以下场景计数为该场景内的归属总数；连接来自已读取的账号来源。</p><ul class="oneid-scenes">${(p.scenes || []).filter(s => scenes.has(s.scene_key)).map(s => `<li>${esc(s.session_id || "场景未知")}<small>适配器 ${esc(s.adapter_id || "未知")} · 人格 ${esc(s.persona_id || "未知")} · ${s.memory_count} 条</small></li>`).join("") || "<li>当前来源页未包含可证明的场景</li>"}</ul>` : ""}</div>`;
    }).join("");
    const pending = this.pending.filter(a => a.one_id === this.selected).map(a => `<p class="oneid-relation pending">${esc(accountKey(a))} · 待审核（未生效）</p>`).join("");
    const revoked = this.history.map(a => `<p class="oneid-relation revoked">${esc(accountKey(a))} · 已解绑</p>`).join("");
    return html + (this.showPending ? pending : "") + (this.showHistory ? revoked : "") || '<p class="hint">暂无账号关联。到“用户绑定”提交申请。</p>';
  }
  recordsHtml() {
    return (this.projection?.records || []).map(r => `<div class="oneid-record">
      <span>#${esc(r.memory_id)} · ${esc(r.source_account || "来源待确认")}<small>${esc(r.scene || "场景未知")} · ${this.mode === "owner" && !(r.subject_one_ids || []).includes(this.selected) ? "用户或其他主体相关" : "主体自身相关"}</small></span>
      <button type="button" data-semantic="${esc(r.memory_id)}" ${this.busy || this.loading ? "disabled" : ""}>查看知识图谱</button>
    </div>`).join("") || '<p class="hint">当前范围暂无可证明的归属记录。</p>';
  }
  drawGraph() {
    const model = identityGraphModel(this.entity(), this.projection, this.showPending ? this.pending : [], this.showHistory ? this.history : [], this.expanded, this.mode);
    const status = this.view.querySelector("#oneid-layout-status");
    const f = new IdentityForce(this.view.querySelector("#oneid-force"), model, {
      onSelect: node => { if (node.status === "active" && node.account) this.expand(accountKey(node.account)); },
      onError: () => { status.textContent = "图形布局不可用，请使用下方等价账号列表。"; },
    });
    this.forces.push(f);
    if (model.truncated || this.pendingMore || this.projection.has_more)
      status.textContent = "图形上限 200 节点 / 400 关系；场景连接来自已读取的来源页，更多来源可分页加载。待审列表最多 100 条。";
  }
  expand(key) {
    this.semanticGeneration = (this.semanticGeneration || 0) + 1;
    this.view.querySelector("#oneid-semantic")?.replaceChildren();
    if (this.expanded.has(key)) this.expanded.delete(key); else this.expanded.add(key);
    this.view.querySelector("#oneid-accounts").innerHTML = this.accountListHtml();
    this.wireExpand();
    // 展开更新模型；保留仍存在节点的位置、固定状态和缩放。
    const old = this.forces[0], positions = new Map(old?.nodes.map(n => [n.id, n]) || []);
    const camera = old ? { zoom: old.zoom, pan: { ...old.pan }, paused: old.paused } : null;
    const selectedId = old?.selected?.id;
    this.stopGraphs(); this.drawGraph();
    const graph = this.forces[0];
    if (graph && !graph.disposed) {
      graph.nodes.forEach(n => { const prev = positions.get(n.id); if (prev) Object.assign(n, { x: prev.x, y: prev.y, fixed: prev.fixed }); });
      if (camera) { graph.zoom = camera.zoom; graph.pan = camera.pan; graph.setPaused(camera.paused); }
      const selected = graph.nodes.find(n => n.id === selectedId);
      if (selected) graph.select(selected, false);
      graph.paint();
    }
    this.view.querySelector(`[data-expand="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
  }
  bindHtml() {
    const disabled = this.busy || this.loading;
    return `<p class="hint">手动录入和自动识别都会先进入待审核队列，批准后关联才生效。操作者取自当前后台登录。</p>
      <details class="oneid-register" ${!this.entities.length || this.registerOpen ? "open" : ""}><summary>登记新主体（人 / 机器人）</summary>
        <form id="oneid-register-form" class="oneid-form">
          <label class="field">主体名称<input id="oneid-register-name" value="${esc(this.registration.display_name)}" maxlength="256" required ${disabled ? "disabled" : ""}></label>
          <label class="field">主体类型<select id="oneid-register-type" ${disabled ? "disabled" : ""}>${["human", "bot"].map(t => `<option value="${t}" ${this.registration.entity_type === t ? "selected" : ""}>${types[t]}</option>`).join("")}</select></label>
          <button type="submit" ${disabled ? "disabled" : ""}>登记主体</button>
        </form><p class="hint">系统自动生成 oneid；名称以后可变化，身份编号保持稳定。</p>
      </details>
      <section class="oneid-bind-section"><h3>提交账号绑定申请</h3>
        <form id="oneid-bind-form" class="oneid-form">
          <label class="field">平台<input id="oneid-platform" list="oneid-platforms" value="${esc(this.draft.platform)}" maxlength="64" required ${disabled ? "disabled" : ""}></label>
          <datalist id="oneid-platforms"><option value="qq">QQ</option><option value="bili">Bilibili</option><option value="discord">Discord</option><option value="telegram">Telegram</option></datalist>
          <label class="field">平台账号 ID<input id="oneid-account" value="${esc(this.draft.account_id)}" maxlength="256" required ${disabled ? "disabled" : ""} placeholder="账号 ID，不是群号"></label>
          <label class="field">账号显示名（可选）<input id="oneid-display" value="${esc(this.draft.display_name)}" maxlength="256" ${disabled ? "disabled" : ""}></label>
          <label class="field oneid-wide">申请依据<textarea id="oneid-evidence" maxlength="4000" rows="3" required ${disabled ? "disabled" : ""} placeholder="本人确认记录、验证链接或可追溯事件编号">${esc(this.draft.evidence)}</textarea></label>
          <label class="oneid-check oneid-wide"><input id="oneid-correction" type="checkbox" ${this.draft.correction ? "checked" : ""} ${disabled ? "disabled" : ""}>申请更正账号已有归属</label>
          <div class="oneid-wide">${button("oneid-correction-preview", "核对当前账号", disabled)}<p class="hint">${this.correctionPreview ? "当前归属：" + esc(this.correctionPreview.binding ? this.correctionPreview.one_id : "尚未关联") + "；涉及 " + (this.correctionPreview.scenes?.length || 0) + " 个来源场景。审核仍需核对完整申请快照。" : "提交前先核对账号现有归属；群号不能当作待绑定账号。"}</p></div>
          <button type="submit" class="oneid-primary" ${disabled || !this.entity() || !this.correctionPreview ? "disabled" : ""}>提交绑定申请</button>
        </form>
      </section>
      <section><div class="oneid-graph-head"><h3>申请队列</h3><label class="field">状态<select id="oneid-application-status" ${disabled ? "disabled" : ""}>
        ${[["pending", "待审核"], ["", "全部历史"], ...Object.entries(statuses).filter(([k]) => k !== "pending")].map(([k, text]) => `<option value="${k}" ${k === this.applicationStatus ? "selected" : ""}>${text}</option>`).join("")}</select></label></div>
        <p class="hint">每次读取 10 条；自动识别与手动申请在这里一起审核。</p>
        ${this.applications.map(a => `<article class="oneid-application"><div><strong>${esc(accountKey(a))}</strong><span class="oneid-status">${statuses[a.status] || esc(a.status)}</span>
          <p>${esc(this.entities.find(e => e.one_id === a.one_id)?.display_name || a.one_id)} · ${a.source === "auto" ? "自动识别" : "手动申请"}${a.correction ? " · 更正归属" : ""}</p>
          <p class="hint">依据：${esc(a.evidence?.ref || "未提供")}<br>提交 ${fmtTime(a.created_at)} · 到期 ${fmtTime(a.expires_at)}</p></div>
          ${a.status === "pending" ? `<button type="button" data-review="${esc(a.application_id)}" ${disabled ? "disabled" : ""}>核对并审核</button>` : ""}</article>`).join("") || '<p class="hint">当前状态没有申请。</p>'}
        ${this.appCursor ? button("oneid-apps-more", "加载更多申请", disabled) : ""}
        ${this.review ? this.reviewHtml() : ""}
      </section>`;
  }
  reviewHtml() {
    const a = this.review;
    return `<form id="oneid-review-form" class="oneid-review">
      <h3>审核 ${esc(accountKey(a))}</h3><p>目标主体：${esc(a.one_id)} · 当前状态：${statuses[a.status]}</p>
      <p>申请依据：${esc(a.evidence?.ref || "")}</p>
      <p>当前账号归属快照：${esc(a.binding_snapshot?.person_id || a.binding_snapshot?.one_id || "无有效绑定")}</p>
      <label class="field">审核依据<textarea id="oneid-review-reason" maxlength="4000" rows="2" required>${esc(this.reviewReason)}</textarea></label>
      <div class="oneid-actions">${[["approve", "批准关联"], ["reject", "驳回申请"], ["cancel", "取消申请"]].map(([k, t]) => `<button type="submit" name="decision" value="${k}" ${this.busy ? "disabled" : ""}>${t}</button>`).join("")}</div>
    </form>`;
  }
  unlinkHtml() {
    const accounts = this.projection?.accounts || [], p = this.preview, disabled = this.busy || this.loading;
    return `<h3>撤销一个账号的关联</h3><p class="hint">其他账号关系保留。被解绑账号回到自身隔离身份，原始记忆和向量继续保留。</p>
      <div class="oneid-toolbar"><label class="field">已生效平台账号<select id="oneid-unlink-account" ${disabled ? "disabled" : ""}>
        ${accounts.map(a => `<option value="${esc(a.binding_id)}" ${a.binding_id === this.unlinkAccount ? "selected" : ""}>${esc(accountKey(a))}</option>`).join("") || '<option value="">没有可解绑的账号</option>'}</select></label>
        ${button("oneid-preview", "预览解绑影响", disabled || !accounts.length)}</div>
      ${p ? `<form id="oneid-unlink-form" class="oneid-unlink-preview">
        <h3>核对解绑影响</h3><p><strong>${esc(accountKey(p.binding))}</strong> → ${esc(p.target_one_id)}</p>
        <p class="hint">当前关系 v${esc(p.binding.version)} · 身份修订 ${esc(p.identity_revision)} · 读取于 ${esc(this.previewTime)}</p>
        <div class="oneid-stats"><span><strong>${Number(p.affected_memory_count) || 0}</strong> 可证明的关联记录</span><span><strong>${Number(p.uncertain_memory_count) || 0}</strong> 待复核历史</span></div>
        <p class="hint">${esc(p.uncertain_count_scope || "待复核历史不自动归给目标主体")}。</p>
        <details><summary>涉及场景（${p.scenes?.length || 0}）</summary><ul>${(p.scenes || []).map(s => `<li>${esc(s || "场景未知")}</li>`).join("")}</ul></details>
        <p>只撤销账号关联，不删除记忆、知识图谱或向量。历史混合总结保留；已经发出的消息不会被撤回。</p>
        <label class="field">解绑依据<textarea id="oneid-unlink-reason" rows="2" maxlength="4000" required>${esc(this.unlinkReason)}</textarea></label>
        <label class="oneid-check"><input id="oneid-unlink-ack" type="checkbox" required ${this.acknowledged ? "checked" : ""}>我已核对目标账号与影响范围</label>
        <button type="submit" class="oneid-danger" ${disabled ? "disabled" : ""}>确认解绑</button>
      </form>` : '<p class="hint">先预览，再确认。关联有变化时需重新预览。</p>'}`;
  }
  wireExpand() { this.view.querySelectorAll("[data-expand]").forEach(b => b.onclick = () => this.expand(b.dataset.expand)); }
  wire() {
    const el = id => this.view.querySelector("#" + id);
    const bind = (id, event, fn) => { const node = el(id); if (node) node.addEventListener(event, fn); };
    this.view.querySelectorAll("[data-tab]").forEach(b => {
      b.onclick = () => {
        if (this.busy) return;
        this.semanticGeneration = (this.semanticGeneration || 0) + 1;
        this.tab = b.dataset.tab; this.preview = null; this.acknowledged = false; this.review = null;
        this.render(); el("oneid-tab-" + this.tab)?.focus();
      };
      b.onkeydown = event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const tabs = [...this.view.querySelectorAll("[data-tab]")], i = tabs.indexOf(b);
        const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (i + (event.key === "ArrowRight" ? 1 : 2)) % 3;
        tabs[next].click();
      };
    });
    bind("oneid-refresh", "click", () => this.fetch());
    bind("oneid-copy", "click", async () => {
      const token = this.epoch;
      try {
        await navigator.clipboard.writeText(this.selected);
        if (this.alive(token)) { this.feedback("oneid 已复制。"); this.render(); }
      } catch {
        if (this.alive(token)) { this.feedback("复制不可用，可选中上方完整 oneid 手动复制。", true); this.render(); }
      }
    });
    for (const [id, field] of [["oneid-show-pending", "showPending"], ["oneid-show-history", "showHistory"]])
      bind(id, "change", () => { this[field] = el(id).checked; this.render(); });
    bind("oneid-entity", "change", () => {
      this.selected = el("oneid-entity").value; this.mode = "subject"; this.expanded.clear();
      this.preview = null; this.correctionPreview = null; this.review = null; this.projection = null;
      this.feedback(""); this.refresh();
    });
    bind("oneid-mode", "change", () => { this.mode = el("oneid-mode").value; this.expanded.clear(); this.refresh(); });
    bind("oneid-entities-more", "click", () => this.loadMore("entities"));
    bind("oneid-apps-more", "click", () => this.loadMore("applications"));
    bind("oneid-records-more", "click", () => this.loadMore("records"));
    bind("oneid-application-status", "change", () => { this.applicationStatus = el("oneid-application-status").value; this.refresh(); });
    this.wireExpand();
    this.view.querySelectorAll("[data-semantic]").forEach(b => b.onclick = () => this.semantic(Number(b.dataset.semantic)));
    this.view.querySelectorAll("[data-review]").forEach(b => b.onclick = () => this.openReview(b.dataset.review));
    for (const [id, key] of [["oneid-platform", "platform"], ["oneid-account", "account_id"], ["oneid-display", "display_name"], ["oneid-evidence", "evidence"]])
      bind(id, "input", () => {
        this.draft[key] = el(id).value;
        if (["platform", "account_id"].includes(key)) {
          this.correctionPreview = null;
          const submit = el("oneid-bind-form")?.querySelector('[type="submit"]'); if (submit) submit.disabled = true;
        }
      });
    bind("oneid-correction", "change", () => { this.draft.correction = el("oneid-correction").checked; this.correctionPreview = null; this.render(); el("oneid-correction")?.focus(); });
    bind("oneid-correction-preview", "click", () => this.loadPreview(true));
    bind("oneid-register-name", "input", () => { this.registration.display_name = el("oneid-register-name").value; });
    bind("oneid-register-type", "change", () => { this.registration.entity_type = el("oneid-register-type").value; });
    bind("oneid-register-form", "submit", event => { event.preventDefault(); this.register(); });
    bind("oneid-bind-form", "submit", event => { event.preventDefault(); this.submit(); });
    bind("oneid-review-reason", "input", () => {
      this.reviewReason = el("oneid-review-reason").value;
      if (this.review) this.reviewDrafts.set(this.review.application_id, this.reviewReason);
    });
    bind("oneid-review-form", "submit", event => { event.preventDefault(); this.reviewApplication(event.submitter?.value); });
    bind("oneid-unlink-account", "change", () => { this.unlinkAccount = el("oneid-unlink-account").value; this.preview = null; this.acknowledged = false; this.render(); });
    bind("oneid-preview", "click", () => this.loadPreview());
    bind("oneid-unlink-reason", "input", () => { this.unlinkReason = el("oneid-unlink-reason").value; });
    bind("oneid-unlink-ack", "change", () => { this.acknowledged = el("oneid-unlink-ack").checked; });
    bind("oneid-unlink-form", "submit", event => { event.preventDefault(); this.unlink(); });
    bind("oneid-advanced", "toggle", async () => {
      this.advancedOpen = el("oneid-advanced").open;
      if (!this.advancedOpen) { this.legacy?.leave(); return; }
      if (this.legacy) { this.legacy.fetch(); return; }
      const token = this.epoch, container = el("oneid-legacy");
      const { LegacyBindingPage } = await import("./binding-legacy.js");
      if (!this.alive(token) || !container.isConnected || !el("oneid-advanced").open) return;
      this.legacy = new LegacyBindingPage(this.state, this.api, this.peek, container);
      this.legacyContainer = container;
      this.legacy.fetch();
    });
  }
  async register() {
    if (!this.registration.display_name.trim()) return;
    const fingerprint = JSON.stringify(this.registration);
    if (this.registrationKey !== fingerprint) {
      this.registrationKey = fingerprint; this.registrationId = "oneid:" + uid();
    }
    const one_id = this.registrationId;
    await this.mutate("page/identity/entities/register", { ...this.registration, one_id }, "主体已登记，可以提交账号申请。", {
      onSuccess: e => { this.entities.push(e); this.selected = e.one_id; this.registration.display_name = ""; this.registrationKey = null; },
    });
  }
  async submit() {
    if (!this.entity() || !this.draft.account_id.trim() || !this.draft.evidence.trim() || !this.correctionPreview) return;
    const payload = { one_id: this.selected, platform: this.draft.platform.trim(), account_id: this.draft.account_id.trim(),
      display_name: this.draft.display_name.trim(), evidence: { type: "manual", ref: this.draft.evidence.trim() }, correction: this.draft.correction };
    await this.mutate("page/identity/applications/submit", payload, "申请已提交，待审核；账号关联尚未生效。", {
      onSuccess: () => { this.draft.account_id = this.draft.display_name = this.draft.evidence = ""; this.draft.correction = false; this.correctionPreview = null; this.applicationStatus = "pending"; },
    });
  }
  openReview(applicationId) {
    const application = this.applications.find(a => a.application_id === applicationId);
    if (!application || application.status !== "pending") return;
    this.review = clone(application);
    this.reviewReason = this.reviewDrafts.get(applicationId) || "";
    this.render(); document.getElementById("oneid-review-reason")?.focus?.();
  }
  async reviewApplication(decision) {
    if (!this.review || this.review.status !== "pending" || !this.reviewReason.trim() || !["approve", "reject", "cancel"].includes(decision)) return;
    const a = clone(this.review);
    this.reviewDrafts.set(a.application_id, this.reviewReason);
    await this.mutate("page/identity/applications/review", { application_id: a.application_id, expected_application: a, decision, reason: this.reviewReason.trim() },
      decision === "approve" ? "申请已批准，账号关联已生效。" : decision === "reject" ? "申请已驳回。" : "申请已取消。", {
        confirm: `${decision === "approve" ? "批准" : decision === "reject" ? "驳回" : "取消"} ${accountKey(a)} 关联到 ${a.one_id} 的申请？`,
        onSuccess: () => { this.reviewDrafts.delete(a.application_id); this.review = null; this.reviewReason = ""; },
        onConflict: () => { this.review = null; },
      });
  }
  async loadPreview(correction = false) {
    if (this.busy || this.loading) return;
    const account = this.projection?.accounts?.find(a => a.binding_id === this.unlinkAccount);
    if (!correction && !account) return;
    if (correction && (!this.draft.platform.trim() || !this.draft.account_id.trim())) { this.feedback("请先填写平台和账号 ID。", true); this.render(); return; }
    const token = this.epoch, read = ++this.readGeneration;
    const input = correction ? JSON.stringify(this.draft) : this.unlinkAccount;
    this.preview = null; this.acknowledged = false; this.loading = true; this.render();
    try {
      const data = await this.api.get("page/identity/preview", correction
        ? { one_id: this.selected, platform: this.draft.platform.trim(), account_id: this.draft.account_id.trim() }
        : { one_id: this.selected, platform: account.platform, account_id: account.account_id, binding_id: account.binding_id });
      if (!this.alive(token) || read !== this.readGeneration || input !== (correction ? JSON.stringify(this.draft) : this.unlinkAccount)) return;
      if (correction) this.correctionPreview = data;
      else if (data.binding?.status === "active" && data.binding.binding_id === this.unlinkAccount && data.target_one_id === this.selected) {
        this.preview = clone(data); this.previewTime = new Date().toLocaleTimeString();
      } else throw new Error("账号关联已变化，请刷新");
      this.feedback("");
    } catch (e) { if (this.alive(token) && read === this.readGeneration) this.feedback(e.message, true); }
    finally { if (this.alive(token) && read === this.readGeneration) { this.loading = false; this.render(); } }
  }
  async unlink() {
    const p = this.preview;
    if (!p || !this.acknowledged || !this.unlinkReason.trim() || p.binding.binding_id !== this.unlinkAccount) return;
    await this.mutate("page/identity/unlink", { one_id: this.selected, binding_id: p.binding.binding_id,
      expected_version: p.binding.version, expected_revision: p.identity_revision, reason: this.unlinkReason.trim() },
      "账号已解绑，其他关联与原始记忆均保留。", {
        confirm: `确认解绑 ${accountKey(p.binding)}？只撤销它与 ${this.selected} 的关联，原始记忆保留。`,
        onSuccess: () => { this.preview = null; this.acknowledged = false; this.unlinkReason = ""; },
        onConflict: () => { this.preview = null; this.acknowledged = false; },
      });
  }
  async loadMore(kind) {
    if (this.busy || this.loading) return;
    const token = this.epoch, read = ++this.readGeneration;
    this.loading = true;
    const scope = this.mode === "owner" ? { owner_one_id: this.selected } : { one_id: this.selected };
    try {
      const data = await this.api.get(kind === "records" ? "page/identity/projection" : "page/identity/" + kind,
        kind === "records" ? { ...scope, limit: 100, offset: this.projection.records.length }
          : { limit: kind === "entities" ? 100 : 10, after: kind === "entities" ? this.entityCursor : this.appCursor, ...(kind === "applications" ? { status: this.applicationStatus } : {}) });
      if (!this.alive(token) || read !== this.readGeneration) return;
      if (kind === "records") {
        if (data.identity_revision !== this.projection.identity_revision) throw new Error("身份范围已变化，请刷新");
        const ids = new Set(this.projection.records.map(r => r.memory_id));
        this.projection.records.push(...data.records.filter(r => !ids.has(r.memory_id))); this.projection.has_more = data.has_more;
      } else {
        const items = kind === "entities" ? this.entities : this.applications;
        const field = kind === "entities" ? "one_id" : "application_id", ids = new Set(items.map(a => a[field]));
        items.push(...data.items.filter(a => !ids.has(a[field])));
        if (kind === "entities") this.entityCursor = data.next_cursor; else this.appCursor = data.next_cursor;
      }
    } catch (e) { if (this.alive(token)) this.feedback(e.message, true); }
    finally { if (this.alive(token) && read === this.readGeneration) { this.loading = false; this.render(); } }
  }
  async semantic(memory_id) {
    if (this.busy || this.loading) return;
    const token = this.epoch, read = this.semanticGeneration = (this.semanticGeneration || 0) + 1, box = this.view.querySelector("#oneid-semantic");
    const record = this.projection?.records.find(r => r.memory_id === memory_id);
    if (!record || !box) return;
    const scope = this.mode === "owner" ? { owner_one_id: this.selected } : { one_id: this.selected };
    box.textContent = `正在读取记忆 #${memory_id} 的原生知识图谱…`;
    try {
      // 原生 Bridge 禁止 endpoint 查询串；后端将正文适配器接入同一范围校验。
      const data = await this.api.post("page/graph/query", { ...scope, memory_id, session_id: record.storage_session_id ?? record.scene,
        adapter_id: record.adapter_id, persona_id: record.persona_id, limit_nodes: 48, limit_edges: 72 });
      if (!this.alive(token) || read !== this.semanticGeneration || !box.isConnected) return;
      // 真实宿主将图谱放在 snapshot；同时兼容旧客户端的顶层节点格式。
      const graph = data.snapshot || data;
      const nodes = graph.nodes || [], edges = graph.edges || [];
      this.semanticForce?.dispose();
      box.innerHTML = `<h4>记忆 #${memory_id} 的原生知识图谱</h4><p class="hint">沿用主体、来源场景、人格和适配器范围。账号绑定不会写入这些语义关系。</p><div class="oneid-semantic-force"></div>
        ${nodes.length ? `<ul>${nodes.map(n => `<li>${esc(n.label || n.name || n.id)}</li>`).join("")}</ul><ul>${edges.map(e => `<li>${esc(nodes.find(n => n.id === e.source)?.label || e.source)} → ${esc(e.relation_type || e.relation || e.label || "相关")} → ${esc(nodes.find(n => n.id === e.target)?.label || e.target)}</li>`).join("")}</ul>` : '<p class="hint">此记忆暂无可见的知识图谱节点。</p>'}`;
      if (nodes.length) {
        this.semanticForce = new IdentityForce(box.querySelector(".oneid-semantic-force"), {
          nodes: nodes.map(n => ({ id: String(n.id), kind: "semantic", label: n.label || n.name || String(n.id) })),
          edges: edges.map(e => ({ source: String(e.source), target: String(e.target), status: "active" })),
        }, { onError: () => { box.querySelector(".oneid-semantic-force").textContent = "布局不可用，请阅读下方原生节点与关系列表。"; } });
        this.forces.push(this.semanticForce);
      }
    } catch (e) { if (this.alive(token) && read === this.semanticGeneration && box.isConnected) box.textContent = "知识图谱读取失败：" + e.message; }
  }
}
