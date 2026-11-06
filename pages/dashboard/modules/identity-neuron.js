import { esc } from "./utils.js";

export function neuronSvg(data) {
  const accounts = Array.isArray(data.accounts) ? data.accounts : [];
  if (!accounts.length && !data.total_memories) return '<p class="hint">该标识没有有效绑定或可验证的记忆。</p>';
  const height = Math.max(320, accounts.length * 92 + 60);
  const center = {x: 180, y: height / 2};
  const branches = accounts.map((account, index) => {
    const y = 52 + index * 92;
    const label = `${account.platform}:${account.account_id}`;
    return `<path d="M ${center.x + 48} ${center.y} C 290 ${center.y},300 ${y},350 ${y}" />
      <path d="M 440 ${y} C 500 ${y},520 ${y},568 ${y}" />
      <circle class="identity-account" cx="390" cy="${y}" r="28" />
      <text x="390" y="${y + 44}">${esc(label)}</text>
      <circle class="identity-memory" cx="610" cy="${y}" r="22" />
      <text x="610" y="${y + 4}">${Number(account.memory_count) || 0}</text>
      <text x="610" y="${y + 44}">衍生记忆</text>`;
  }).join("");
  return `<p class="hint">有效账号 ${accounts.length} · 该人物记忆 ${Number(data.total_memories) || 0} 条 · 各账号计数之和不包含只有人物标签的记录</p>
    <div class="identity-svg-scroll"><svg class="identity-neuron" role="img" aria-label="${esc(data.person_id)}的账号关联图" viewBox="0 0 760 ${height}">
      <title>${esc(data.person_id)} · 多账号关联唯一用户标识</title>${branches}
      <circle class="identity-person" cx="${center.x}" cy="${center.y}" r="48" />
      <text x="${center.x}" y="${center.y - 5}">唯一用户</text>
      <text x="${center.x}" y="${center.y + 17}">${esc(data.person_id)}</text>
    </svg></div>`;
}
