import { esc } from "./utils.js";

export const GRAPH_LIMIT = 200;
const keyOf = a => `${a.platform}:${a.account_id}`;

// 场景关系仅使用投影中的可证明账号来源；分页样本之外不猜连接。
export function identityGraphModel(entity, projection, applications = [], history = [], expanded = new Set(), mode = "subject") {
  const nodes = [{ id: "entity", kind: "entity", label: (entity.display_name || entity.one_id) + (entity.entity_type === "bot" ? " · 机器人" : entity.entity_type === "human" ? " · 人" : ""), detail: entity.one_id }];
  const edges = [];
  const accounts = projection.accounts || [];
  const addAccount = (a, status) => {
    const id = `${status}:${keyOf(a)}`;
    if (!nodes.some(n => n.id === id)) {
      nodes.push({ id, kind: "account", label: keyOf(a), detail: status, account: a, status });
      edges.push({ source: "entity", target: id, status });
    }
    return id;
  };
  accounts.forEach(a => {
    const id = addAccount(a, "active");
    if (!expanded.has(keyOf(a))) return;
    const records = (projection.records || []).filter(r => (mode === "owner" ? r.owner_account : r.source_account) === keyOf(a));
    const keys = new Set(records.map(r => r.scene_key));
    (projection.scenes || []).filter(s => keys.has(s.scene_key)).forEach(s => {
      const sceneId = "scene:" + s.scene_key;
      if (!nodes.some(n => n.id === sceneId)) nodes.push({ id: sceneId, kind: "scene", label: s.session_id || "来源场景未知", detail: s.adapter_id, scene: s });
      edges.push({ source: id, target: sceneId, status: "scene" });
    });
  });
  applications.filter(a => a.one_id === entity.one_id && a.status === "pending").forEach(a => addAccount(a, "pending"));
  history.filter(a => a.status === "revoked").forEach(a => addAccount(a, "revoked"));
  const kept = nodes.slice(0, GRAPH_LIMIT);
  const ids = new Set(kept.map(n => n.id));
  const keptEdges = edges.filter(e => ids.has(e.source) && ids.has(e.target)).slice(0, 400);
  return { nodes: kept, edges: keptEdges, truncated: nodes.length > kept.length || edges.length > keptEdges.length };
}

// 二维弹簧力、节点斥力及中心引力；有明确的节点/帧预算，收敛后停止。
export function forceStep(nodes, edges, alpha = 1) {
  for (const n of nodes) { n.vx *= .72; n.vy *= .72; n.vx -= n.x * .002 * alpha; n.vy -= n.y * .002 * alpha; }
  for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    const a = nodes[i], b = nodes[j];
    let dx = a.x - b.x, dy = a.y - b.y;
    if (Math.abs(dx) + Math.abs(dy) < .01) { dx = .1; dy = .1; }
    const d2 = Math.max(400, dx * dx + dy * dy), f = 400 * alpha / d2;
    a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
  }
  const map = new Map(nodes.map(n => [n.id, n]));
  for (const e of edges) {
    const a = map.get(e.source), b = map.get(e.target);
    if (!a || !b) continue;
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(1, Math.hypot(dx, dy));
    const f = (d - (e.status === "scene" ? 145 : 190)) * .012 * alpha / d;
    a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
  }
  for (const n of nodes) {
    if (n.fixed) { n.vx = n.vy = 0; continue; }
    n.x += Math.max(-12, Math.min(12, n.vx)); n.y += Math.max(-12, Math.min(12, n.vy));
  }
}

export class IdentityForce {
  constructor(container, model, { onSelect = () => {}, onError = () => {} } = {}) {
    this.container = container;
    this.model = model;
    this.nodes = model.nodes.map((n, i) => ({ ...n, x: i ? Math.cos(i * 2.4) * (160 + i * 2) : 0, y: i ? Math.sin(i * 2.4) * (160 + i * 2) : 0, vx: 0, vy: 0, fixed: false }));
    this.reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    this.paused = !!this.reduced?.matches;
    this.onSelect = onSelect; this.onError = onError;
    this.zoom = 1; this.pan = { x: 0, y: 0 }; this.frame = 0; this.remaining = 0;
    this.selected = null; this.disposed = false;
    try { this.mount(); } catch (error) { this.dispose(); onError(error); }
  }
  mount() {
    this.container.innerHTML = `<div class="oneid-force-controls">
      <button type="button" data-force="pause" aria-pressed="${this.paused}">${this.paused ? "继续布局" : "暂停布局"}</button>
      <button type="button" data-force="center">居中</button>
      <button type="button" data-force="in" aria-label="放大图谱">＋</button>
      <button type="button" data-force="out" aria-label="缩小图谱">−</button>
      <button type="button" data-force="pin" disabled>固定选中节点</button>
    </div><svg class="oneid-force-svg" viewBox="-450 -240 900 480" role="group" aria-label="二维关联图；可拖动节点，使用下方列表也能展开场景">
      <g class="oneid-viewport">
        ${this.model.edges.map(e => `<line class="oneid-edge ${esc(e.status || "active")}" />`).join("")}
        ${this.nodes.map((n, i) => `<g data-node="${i}" class="oneid-node ${esc(n.kind)} ${esc(n.status || "")}" role="button" tabindex="0" aria-label="${esc(n.label)}${n.status ? " · " + ({ active: "已生效", pending: "待审核", revoked: "已解绑" }[n.status] || n.status) : ""}">
          <circle r="${n.kind === "entity" ? 32 : n.kind === "scene" ? 12 : 20}" />
          <text y="${n.kind === "entity" ? 52 : 40}" text-anchor="middle">${esc(n.kind === "scene" ? (
            /GroupMessage/.test(n.label) ? "群聊" : /FriendMessage|PrivateMessage/.test(n.label) ? "私聊" : /Comment/.test(n.label) ? "评论场景" : "来源场景"
          ) : n.label.length > 20 ? n.label.slice(0, 18) + "…" : n.label)}</text>
          ${n.kind === "entity" ? `<text class="oneid-node-id" text-anchor="middle">${esc(n.detail?.length > 18 ? n.detail.slice(0, 16) + "…" : n.detail || "")}</text>` : ""}
          <title>${esc(n.label)} · ${esc(n.detail || "")}</title></g>`).join("")}
      </g></svg><p class="hint oneid-force-hint">拖动节点或空白区域 · 滚轮缩放 · Enter 选择 · P 固定/释放 · 方向键移动节点</p>`;
    this.svg = this.container.querySelector("svg");
    this.size = { width: this.svg.clientWidth || 900, height: this.svg.clientHeight || 350 };
    this.svg.setAttribute("viewBox", `${-this.size.width / 2} ${-this.size.height / 2} ${this.size.width} ${this.size.height}`);
    this.group = this.container.querySelector(".oneid-viewport");
    this.nodeElements = [...this.container.querySelectorAll("[data-node]")];
    this.lineElements = [...this.container.querySelectorAll("line")];
    this.container.querySelectorAll("[data-force]").forEach(b => b.onclick = () => {
      if (b.dataset.force === "pause") this.setPaused(!this.paused);
      if (b.dataset.force === "center") { this.zoom = 1; this.pan = { x: 0, y: 0 }; this.fit(); }
      if (b.dataset.force === "in") this.zoom = Math.min(3, this.zoom * 1.25);
      if (b.dataset.force === "out") this.zoom = Math.max(.25, this.zoom / 1.25);
      if (b.dataset.force === "pin") this.pin();
      this.paint();
    });
    this.svg.onwheel = e => { e.preventDefault(); this.zoom = Math.max(.25, Math.min(3, this.zoom * (e.deltaY < 0 ? 1.12 : .88))); this.paint(); };
    this.svg.onpointerdown = e => this.pointerStart(e);
    this.svg.onpointermove = e => this.pointerMove(e);
    this.svg.onpointerup = e => this.pointerEnd(e);
    this.svg.onpointercancel = () => { this.drag = null; };
    this.nodeElements.forEach((el, i) => {
      el.onclick = () => { if (!this.moved) this.select(this.nodes[i]); };
      el.onkeydown = e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.select(this.nodes[i]); }
        if (e.key.toLowerCase() === "p") { e.preventDefault(); this.select(this.nodes[i], false); this.pin(); }
        if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
          e.preventDefault(); const n = this.nodes[i]; n.fixed = true;
          n.x += e.key === "ArrowLeft" ? -16 : e.key === "ArrowRight" ? 16 : 0;
          n.y += e.key === "ArrowUp" ? -16 : e.key === "ArrowDown" ? 16 : 0; this.paint();
        }
      };
    });
    this.motionListener = e => { if (e.matches) this.setPaused(true); };
    this.reduced?.addEventListener?.("change", this.motionListener);
    for (let i = 0; i < 80; i++) forceStep(this.nodes, this.model.edges, .6);
    this.fit(); this.paint(); this.start();
    if (typeof ResizeObserver !== "undefined") {
      this.observer = new ResizeObserver(() => {
        if (this.disposed) return;
        this.size = { width: this.svg.clientWidth || 900, height: this.svg.clientHeight || 350 };
        this.svg.setAttribute("viewBox", `${-this.size.width / 2} ${-this.size.height / 2} ${this.size.width} ${this.size.height}`);
        this.fit(); this.paint();
      });
      this.observer.observe(this.svg);
    }
  }
  select(node, notify = true) {
    this.selected = node;
    this.nodeElements.forEach((e, i) => e.classList.toggle("selected", this.nodes[i] === node));
    const pin = this.container.querySelector('[data-force="pin"]');
    pin.disabled = false; pin.textContent = node.fixed ? "释放选中节点" : "固定选中节点";
    if (notify) this.onSelect(node);
  }
  pin() {
    if (!this.selected) return;
    this.selected.fixed = !this.selected.fixed; this.select(this.selected, false); this.paint(); this.start();
  }
  point(e) {
    const r = this.svg.getBoundingClientRect(), scale = Math.min(r.width / this.size.width, r.height / this.size.height);
    return { x: (e.clientX - r.left - r.width / 2) / scale, y: (e.clientY - r.top - r.height / 2) / scale };
  }
  pointerStart(e) {
    if (e.button !== 0) return;
    const el = e.target.closest("[data-node]"), n = el ? this.nodes[Number(el.dataset.node)] : null;
    this.moved = false; this.drag = { node: n, point: this.point(e), pan: { ...this.pan } };
    if (n) { n.fixed = true; this.select(n, false); }
    this.svg.setPointerCapture?.(e.pointerId);
  }
  pointerMove(e) {
    if (!this.drag) return;
    const p = this.point(e), d = this.drag;
    if (Math.hypot(p.x - d.point.x, p.y - d.point.y) < 3 && !this.moved) return;
    this.moved = true;
    if (d.node) { d.node.x = (p.x - this.pan.x) / this.zoom; d.node.y = (p.y - this.pan.y) / this.zoom; }
    else { this.pan.x = d.pan.x + p.x - d.point.x; this.pan.y = d.pan.y + p.y - d.point.y; }
    this.paint();
  }
  pointerEnd() { this.drag = null; this.start(); }
  fit() {
    const maxX = Math.max(200, ...this.nodes.map(n => Math.abs(n.x) + 100));
    const maxY = Math.max(160, ...this.nodes.map(n => Math.abs(n.y) + 55));
    this.zoom = Math.min(1, (this.size.width / 2 - 16) / maxX, (this.size.height / 2 - 16) / maxY);
  }
  paint() {
    this.group.setAttribute("transform", `translate(${this.pan.x},${this.pan.y}) scale(${this.zoom})`);
    const map = new Map(this.nodes.map(n => [n.id, n]));
    this.nodeElements.forEach((el, i) => {
      const n = this.nodes[i];
      el.setAttribute("transform", `translate(${n.x},${n.y})`);
      // 标签和可点击圆保持屏幕尺寸，不随关系图缩小到不可读。
      const radius = n.kind === "entity" ? 28 : n.kind === "scene" ? 14 : 22;
      el.querySelector("circle").setAttribute("r", radius / this.zoom);
      const label = el.querySelector("text");
      label.setAttribute("font-size", 14 / this.zoom);
      label.style.fontSize = 14 / this.zoom + "px";
      label.setAttribute("y", (radius + 20) / this.zoom);
      const idLabel = el.querySelector(".oneid-node-id");
      if (idLabel) { idLabel.style.fontSize = 14 / this.zoom + "px"; idLabel.setAttribute("y", (radius + 38) / this.zoom); }
    });
    this.lineElements.forEach((el, i) => {
      const e = this.model.edges[i], a = map.get(e.source), b = map.get(e.target);
      if (!a || !b) return;
      for (const [k, v] of Object.entries({ x1: a.x, y1: a.y, x2: b.x, y2: b.y })) el.setAttribute(k, v);
    });
  }
  setPaused(value) {
    this.paused = value;
    const b = this.container.querySelector('[data-force="pause"]');
    b.textContent = value ? "继续布局" : "暂停布局"; b.setAttribute("aria-pressed", String(value));
    if (value) { cancelAnimationFrame(this.frame); this.frame = 0; } else this.start();
  }
  start() {
    if (this.paused || this.disposed) return;
    this.remaining = 120;
    if (this.frame) return;
    const tick = () => {
      this.frame = 0;
      if (this.disposed || this.paused) return;
      try {
        forceStep(this.nodes, this.model.edges, Math.max(.05, this.remaining / 120)); this.paint();
        if (--this.remaining > 0) this.frame = requestAnimationFrame(tick);
      } catch (error) { this.dispose(); this.onError(error); }
    };
    this.frame = requestAnimationFrame(tick);
  }
  dispose() {
    this.disposed = true; cancelAnimationFrame(this.frame); this.frame = 0; this.drag = null;
    this.reduced?.removeEventListener?.("change", this.motionListener);
    this.observer?.disconnect();
    if (this.svg) for (const name of ["onwheel", "onpointerdown", "onpointermove", "onpointerup", "onpointercancel"]) this.svg[name] = null;
    this.nodeElements?.forEach(e => { e.onclick = e.onkeydown = null; });
    this.container.querySelectorAll?.("[data-force]").forEach(b => { b.onclick = null; });
  }
}
