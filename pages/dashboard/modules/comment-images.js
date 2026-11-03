/* BiliBot仅提供已发布公开地址；LivingMemory不存储或下载图片文件。 */
import { esc } from "./utils.js";

function publicUrl(value) {
  try {
    const url = new URL(String(value || ""));
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password ||
        (url.port && !["80", "443"].includes(url.port))) return "";
    if (!["hdslb.com", "biliimg.com", "bilibili.com"].some((d) => url.hostname === d || url.hostname.endsWith("." + d))) return "";
    return `https://${url.hostname}${url.pathname}`;
  } catch { return ""; }
}

export function commentImages(metadata) {
  if (metadata?.bilibot_send_status !== "published" || !Array.isArray(metadata?.bilibot_images)) return [];
  return metadata.bilibot_images.filter((x) => x && typeof x === "object").slice(0, 9);
}

export function imageStrip(metadata, compact = false) {
  const images = commentImages(metadata);
  if (!images.length) return "";
  const shown = compact ? images.slice(0, 1) : images;
  return `<div class="comment-image-strip${compact ? " is-compact" : ""}">${shown.map((image) => {
    const url = publicUrl(image.published_url);
    const description = String(image.description || "评论配图");
    const evicted = image.local_status === "evicted";
    const preview = `<span class="comment-image-preview${url ? "" : " is-unavailable"}">
      ${url ? `<img src="${esc(url)}" alt="${esc(description)}" width="${compact ? 40 : 144}" height="${compact ? 40 : 120}" loading="lazy" decoding="async" referrerpolicy="no-referrer" />` : ""}
      <span class="comment-image-fallback">${url ? "图片暂不可用" : "未保存公开地址"}</span></span>`;
    if (compact) return preview + (images.length > 1 ? `<small>+${images.length - 1}</small>` : "");
    return `<figure class="comment-image-figure">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer" aria-label="打开评论配图：${esc(description)}">${preview}</a>` : preview}
      <figcaption>${esc(description)}<small>${esc(image.emotion || "")}${evicted ? " · 本地已淘汰，历史保留" : ""}</small>
      ${image.use_reason ? `<small>配图原因：${esc(image.use_reason)}</small>` : ""}</figcaption></figure>`;
  }).join("")}</div>`;
}

export function bindImageErrors(root) {
  root?.querySelectorAll(".comment-image-preview img").forEach((image) => {
    const fail = () => image.closest(".comment-image-preview")?.classList.add("is-unavailable");
    image.addEventListener("error", fail, { once: true });
    if (image.complete && !image.naturalWidth) fail();
  });
}
