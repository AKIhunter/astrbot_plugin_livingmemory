import assert from "node:assert/strict";
import test from "node:test";
import { KnowledgePage } from "../../pages/dashboard/modules/knowledge-page.js";

function environment(t) {
  function element() {
    return {
      value: "", checked: false, disabled: false, textContent: "", innerHTML: "", childNodes: [],
      classList: { toggle() {} },
      addEventListener() {},
      append(...nodes) { this.childNodes.push(...nodes); },
      replaceChildren() { this.childNodes = []; this.textContent = ""; this.innerHTML = ""; },
    };
  }
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  globalThis.document = { getElementById: el, createElement: element };
  globalThis.window = { t: key => key };
  t.after(() => { delete globalThis.document; delete globalThis.window; });
  return el;
}

const policy = () => ({
  enabled: true, valid: true, revision: "current",
  bindings: [{ caller: "bilibot", purpose: "public_comment", persona_id: "persona", kb_ids: ["kb"], session_id: "" }],
});

test("a delayed private document response is discarded after leaving the page", async t => {
  const el = environment(t);
  let finish;
  const page = new KnowledgePage({}, { get: () => new Promise(resolve => { finish = resolve; }) });
  const pending = page.preview({ kb_id: "kb" }, { doc_id: "doc" }, 1);
  page.leave();
  finish({ name: "private", items: [{ content: "private" }], total: 1 });
  await pending;
  assert.equal(el("kb-preview").childNodes.length, 0);
  assert.equal(el("kb-preview").textContent, "");
});

test("document markup is displayed as text and never inserted as executable HTML", async t => {
  const el = environment(t);
  const malicious = '<img src="https://tracker.invalid" onerror="alert(1)"><script>alert(1)</script>';
  const page = new KnowledgePage({}, { get: async () => ({
    name: malicious, items: [{ content: malicious }], total: 1,
  }) });
  await page.preview({ kb_id: "kb" }, { doc_id: "doc" }, 1);
  assert.equal(el("kb-preview").childNodes[1].textContent, malicious);
  assert.equal(el("kb-preview").childNodes[1].innerHTML, "");
});

test("editing a grant cannot test an unsaved identity or retain old result cards", async t => {
  const el = environment(t);
  let calls = 0;
  const page = new KnowledgePage({}, { post: async () => { calls++; } });
  page.setPolicy(policy());
  el("kb-query").value = "query";
  el("kb-identity").value = "0";
  el("kb-bindings").value = "[]";
  await page.test();
  assert.equal(calls, 0);
});

test("failed policy save keeps the draft and original revision for conflict recovery", async t => {
  const el = environment(t);
  const page = new KnowledgePage({}, { post: async () => { throw new Error("conflict"); } });
  page.setPolicy(policy());
  el("kb-bindings").value = "[]";
  await page.save();
  assert.equal(el("kb-bindings").value, "[]");
  assert.equal(page.policy.revision, "current");
  assert.equal(el("kb-feedback").textContent, "conflict");
  assert.equal(page.busy, false);
  assert.equal(el("kb-test").disabled, true);
});

test("public test submits no owner session and clears previous results on failure", async t => {
  const el = environment(t);
  let sent;
  const page = new KnowledgePage({}, { post: async (path, body) => {
    sent = body;
    throw new Error("unavailable");
  } });
  page.setPolicy(policy());
  el("kb-identity").value = "0";
  el("kb-query").value = "Claude";
  el("kb-results").append("old private result");
  await page.test();
  assert.equal(sent.identity.session_id, "");
  assert.equal(sent.identity.purpose, "public_comment");
  assert.equal(el("kb-results").childNodes.length, 0);
  assert.equal(page.busy, false);
});
