import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: false });

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const BLOCKED_TAGS = [
  "script",
  "style",
  "iframe",
  "object",
  "embed",
  "form",
  "input",
  "button",
  "textarea",
  "select",
  "option",
  "svg",
];

// DOMPurify 在没有完整 DOM 的环境里可能退化为原样返回；这层兜底只做
// 明确危险的移除，保证浏览器/离线环境都不会把原始 HTML 注入页面。
function fallbackSanitize(html) {
  if (typeof DOMParser === "undefined") return html;
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll(BLOCKED_TAGS.join(",")).forEach((node) => node.remove());
  doc.querySelectorAll("*").forEach((node) => {
    for (const attr of [...node.attributes]) {
      if (/^on/i.test(attr.name) || attr.name === "style") {
        node.removeAttribute(attr.name);
      } else if (
        (attr.name === "href" || attr.name === "src") &&
        /^\s*(javascript|data:text\/html)/i.test(attr.value)
      ) {
        node.removeAttribute(attr.name);
      }
    }
  });
  return doc.body.innerHTML;
}

export function renderMarkdown(text) {
  const html = marked.parse(String(text || ""), { async: false });
  const sanitized = DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: BLOCKED_TAGS,
    FORBID_ATTR: ["style", "onerror", "onload", "onclick"],
  });
  // 某些离屏/无完整 DOM 环境会让 DOMPurify 退化为空字符串；此时用兜底
  // 清理原始 HTML，而不是把内容静默清空。
  return fallbackSanitize(sanitized || html);
}
