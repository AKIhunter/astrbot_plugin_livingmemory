import { esc, confirmDiscardChanges } from "./utils.js";

const $ = id => document.getElementById(id);
const t = key => window.t(key);

export class KnowledgePage {
  constructor(state, api) {
    this.state = state;
    this.api = api;
    this.generation = 0;
    this.detailGeneration = 0;
    this.policy = null;
    this.busy = false;
  }

  initEventListeners() {
    $("kb-refresh").addEventListener("click", async () => {
      if (await this.canLeave()) this.fetch();
    });
    $("kb-save").addEventListener("click", () => this.save());
    $("kb-test-form").addEventListener("submit", event => {
      event.preventDefault();
      this.test();
    });
    $("kb-policy").addEventListener("input", () => {
      this.clearResults();
      $("kb-test").disabled = this.hasUnsavedChanges() || !this.policy?.enabled || !this.policy?.bindings.length;
    });
    $("kb-identity").addEventListener("change", () => this.clearResults());
  }

  clearResults() {
    this.testGeneration = (this.testGeneration || 0) + 1;
    $("kb-results").replaceChildren();
  }

  hasUnsavedChanges() {
    return Boolean(this.policy && (
      $("kb-enabled").checked !== this.policy.enabled
      || $("kb-bindings").value !== JSON.stringify(this.policy.bindings, null, 2)
    ));
  }

  async canLeave() {
    return !this.busy && (!this.hasUnsavedChanges() || await confirmDiscardChanges());
  }

  leave() {
    this.generation++;
    this.detailGeneration++;
    this.clearResults();
    $("kb-preview").replaceChildren();
    $("kb-query").value = "";
  }

  feedback(text, error = false) {
    const node = $("kb-feedback");
    node.textContent = text;
    node.classList.toggle("is-error", error);
  }

  async fetch() {
    if (this.busy) return;
    const generation = ++this.generation;
    this.detailGeneration++;
    this.clearResults();
    this.policy = null;
    $("kb-preview").replaceChildren();
    $("kb-documents").replaceChildren();
    $("kb-catalog").replaceChildren();
    $("kb-save").disabled = true;
    $("kb-test").disabled = true;
    this.feedback(t("common.loading"));
    try {
      const data = await this.api.get("knowledge/catalog");
      if (generation !== this.generation) return;
      this.setPolicy(data.policy);
      for (const kb of data.items || []) {
        const card = document.createElement("article");
        card.className = "kb-card";
        card.innerHTML = `<h3>${esc(kb.name)}</h3><p>${esc(kb.description)}</p>
          <p>${esc(kb.documents)} ${esc(t("kb.documents"))} · ${esc(kb.chunks)} ${esc(t("kb.chunks"))}</p>
          <p class="kb-scope">${esc(t(kb.public ? "kb.public" : "kb.unpublished"))}</p>
          <code>${esc(kb.kb_id)}</code>`;
        const button = document.createElement("button");
        button.className = "btn btn-secondary btn-sm";
        button.textContent = t("kb.browse");
        button.onclick = () => this.documents(kb, 1);
        card.append(button);
        $("kb-catalog").append(card);
      }
      if (!data.items?.length) $("kb-catalog").textContent = t("common.noData");
      this.feedback(t(data.policy.valid ? "kb.loaded" : "kb.invalid"));
    } catch (error) {
      if (generation === this.generation) this.feedback(error.message, true);
    }
  }

  setPolicy(policy) {
    this.policy = policy;
    $("kb-enabled").checked = policy.enabled;
    $("kb-bindings").value = JSON.stringify(policy.bindings, null, 2);
    $("kb-save").disabled = false;
    $("kb-test").disabled = !policy.enabled || !policy.bindings.length || !policy.valid;
    $("kb-identity").replaceChildren();
    for (const [index, binding] of policy.bindings.entries()) {
      const option = document.createElement("option");
      option.value = index;
      option.textContent = `${binding.caller} · ${binding.persona_id} · ${t(binding.purpose === "public_comment" ? "kb.publicPurpose" : "kb.sessionPurpose")}${binding.session_id ? " · " + binding.session_id : ""}`;
      $("kb-identity").append(option);
    }
  }

  pagination(container, page, total, size, load) {
    const nav = document.createElement("div");
    nav.className = "kb-pagination";
    for (const [label, next, disabled] of [
      ["pagination.prev", page - 1, page <= 1],
      ["pagination.next", page + 1, page * size >= total],
    ]) {
      const button = document.createElement("button");
      button.className = "btn btn-secondary btn-sm";
      button.textContent = t(label);
      button.disabled = disabled;
      button.onclick = () => load(next);
      nav.append(button);
    }
    container.append(nav);
  }

  async documents(kb, page) {
    const generation = ++this.detailGeneration;
    $("kb-preview").replaceChildren();
    $("kb-documents").textContent = t("common.loading");
    try {
      const data = await this.api.get("knowledge/documents", { kb_id: kb.kb_id, page });
      if (generation !== this.detailGeneration) return;
      const container = $("kb-documents");
      container.replaceChildren();
      const title = document.createElement("h3");
      title.textContent = kb.name;
      container.append(title);
      for (const doc of data.items) {
        const button = document.createElement("button");
        button.className = "btn btn-secondary kb-document";
        button.textContent = `${doc.name} · ${doc.chunks} ${t("kb.chunks")}`;
        button.onclick = () => this.preview(kb, doc, 1);
        container.append(button);
      }
      if (!data.items.length) container.append(t("common.noData"));
      this.pagination(container, page, data.total, 20, next => this.documents(kb, next));
    } catch (error) {
      if (generation === this.detailGeneration) $("kb-documents").textContent = error.message;
    }
  }

  async preview(kb, doc, page) {
    const generation = ++this.detailGeneration;
    $("kb-preview").textContent = t("common.loading");
    try {
      const data = await this.api.get("knowledge/preview", { kb_id: kb.kb_id, doc_id: doc.doc_id, page });
      if (generation !== this.detailGeneration) return;
      const container = $("kb-preview");
      container.replaceChildren();
      const title = document.createElement("h3");
      title.textContent = data.name;
      container.append(title);
      for (const chunk of data.items) {
        const pre = document.createElement("pre");
        // 文档是数据：不执行 HTML，也不请求 Markdown 中的外链图片。
        pre.textContent = chunk.content + (chunk.truncated ? "\n" + t("kb.truncated") : "");
        container.append(pre);
      }
      if (!data.items.length) container.append(t("common.noData"));
      this.pagination(container, page, data.total, 10, next => this.preview(kb, doc, next));
    } catch (error) {
      if (generation === this.detailGeneration) $("kb-preview").textContent = error.message;
    }
  }

  async save() {
    if (this.busy || !this.policy) return;
    let bindings;
    try {
      bindings = JSON.parse($("kb-bindings").value);
      if (!Array.isArray(bindings)) throw new Error();
    } catch {
      this.feedback(t("kb.invalid"), true);
      return;
    }
    this.busy = true;
    this.clearResults();
    $("kb-save").disabled = true;
    $("kb-test").disabled = true;
    $("kb-policy").disabled = true;
    this.feedback(t("flow.working"));
    try {
      await this.api.post("knowledge/policy", {
        enabled: $("kb-enabled").checked, bindings, revision: this.policy.revision,
      });
      this.busy = false;
      await this.fetch();
      this.feedback(t("kb.saved"));
    } catch (error) {
      this.feedback(error.message, true);
    } finally {
      this.busy = false;
      $("kb-save").disabled = false;
      $("kb-policy").disabled = false;
      $("kb-test").disabled = this.hasUnsavedChanges() || !this.policy?.enabled || !this.policy?.bindings.length;
    }
  }

  async test() {
    if (this.busy || $("kb-test").disabled || !this.policy || this.hasUnsavedChanges()) return;
    const binding = this.policy.bindings[Number($("kb-identity").value)];
    const query = $("kb-query").value.trim();
    if (!binding || !query) return;
    this.clearResults();
    const generation = this.testGeneration;
    this.busy = true;
    $("kb-test").disabled = true;
    this.feedback(t("recall.searching"));
    const { caller, purpose, persona_id, session_id = "" } = binding;
    try {
      const data = await this.api.post("knowledge/test", {
        query, identity: { caller, purpose, persona_id, session_id }, revision: this.policy.revision,
      });
      if (generation !== this.testGeneration) return;
      this.feedback([t("kb.status." + data.status), ...Object.entries(data.sources).map(
        ([source, status]) => `${t(source === "knowledge" ? "nav.knowledge" : "nav.memory")}：${t("kb.status." + status)}`,
      )].join(" · "));
      for (const item of [...data.knowledge, ...data.experiences]) {
        const card = document.createElement("article");
        card.className = "kb-card";
        const title = document.createElement("h3");
        title.textContent = item.source === "knowledge"
          ? `${item.kb_name} / ${item.doc_name}` : `${t("nav.memory")} #${item.memory_id}`;
        const body = document.createElement("pre");
        body.textContent = item.content;
        card.append(title, body);
        $("kb-results").append(card);
      }
      if (!$("kb-results").childNodes.length) $("kb-results").textContent = t("kb.noResults");
    } catch (error) {
      if (generation === this.testGeneration) this.feedback(error.message, true);
    } finally {
      this.busy = false;
      $("kb-test").disabled = this.hasUnsavedChanges() || !this.policy?.enabled;
    }
  }
}
