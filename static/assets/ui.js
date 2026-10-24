import { locale, t } from "./i18n.js";

export function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const numberFormat = new Intl.NumberFormat(locale());
export function formatNumber(value) {
  return numberFormat.format(Number(value) || 0);
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}

export function formatDuration(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 1) return "<1s";
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  if (minutes < 60) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${hours}h ${String(mins).padStart(2, "0")}m`;
}

function parseDate(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatTime(iso) {
  const date = parseDate(iso);
  if (!date) return "—";
  const now = new Date();
  const sameYear = date.getFullYear() === now.getFullYear();
  return new Intl.DateTimeFormat(locale(), {
    ...(sameYear ? {} : { year: "numeric" }),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDateTime(iso) {
  const date = parseDate(iso);
  if (!date) return "—";
  return new Intl.DateTimeFormat(locale(), {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatRelative(iso) {
  const date = parseDate(iso);
  if (!date) return "—";
  const diff = (date.getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: "auto" });
  if (abs < 45) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day");
  return formatTime(iso);
}

export function statusBadge(status) {
  const label = t(`status.${status}`);
  return `<span class="badge badge-${escapeHtml(status)}"><span class="dot"></span>${escapeHtml(label)}</span>`;
}

/* ---------------------------------------------------------------- icons */

const icon = (paths, extra = "") =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${paths}</svg>`;

export const icons = {
  overview: icon(
    '<rect x="3.5" y="3.5" width="7" height="7" rx="1.4"/><rect x="13.5" y="3.5" width="7" height="4.5" rx="1.4"/><rect x="13.5" y="11" width="7" height="9.5" rx="1.4"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.4"/>'
  ),
  datasets: icon(
    '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v6c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8v-6"/><path d="M4.5 11.5v6c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8v-6"/>'
  ),
  notebooks: icon(
    '<path d="M6.5 3.5h11a1.5 1.5 0 0 1 1.5 1.5v14a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 19V5a1.5 1.5 0 0 1 1.5-1.5Z"/><path d="M9 7.5l2.2 2.2L9 11.9"/><path d="M13 12.5h3"/>'
  ),
  runs: icon(
    '<circle cx="12" cy="12" r="8.5"/><path d="M10 8.8v6.4l5.2-3.2L10 8.8Z" fill="currentColor" stroke="none"/>'
  ),
  activity: icon(
    '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 1.8"/>'
  ),
  account: icon(
    '<circle cx="12" cy="8.2" r="3.6"/><path d="M4.8 20c.7-3.5 3.7-5.4 7.2-5.4s6.5 1.9 7.2 5.4"/>'
  ),
  upload: icon('<path d="M12 16V4.5"/><path d="m7.5 9 4.5-4.5L16.5 9"/><path d="M4.5 16.5v2a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-2"/>'),
  download: icon('<path d="M12 4.5V16"/><path d="m7.5 11.5 4.5 4.5 4.5-4.5"/><path d="M4.5 16.5v2A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5v-2"/>'),
  trash: icon('<path d="M4.5 7h15"/><path d="M9.5 7V5.4A1.4 1.4 0 0 1 10.9 4h2.2a1.4 1.4 0 0 1 1.4 1.4V7"/><path d="M6.5 7l.8 12.1a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4L17.5 7"/><path d="M10.2 11v5.5M13.8 11v5.5"/>'),
  plus: icon('<path d="M12 5v14M5 12h14"/>'),
  x: icon('<path d="m6 6 12 12M18 6 6 18"/>'),
  chevronLeft: icon('<path d="m14.5 6-6 6 6 6"/>'),
  chevronRight: icon('<path d="m9.5 6 6 6-6 6"/>'),
  search: icon('<circle cx="10.8" cy="10.8" r="6.3"/><path d="m15.5 15.5 4 4"/>'),
  sun: icon('<circle cx="12" cy="12" r="3.6"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/>'),
  moon: icon('<path d="M20 13.4A7.8 7.8 0 0 1 10.6 4 8.2 8.2 0 1 0 20 13.4Z"/>'),
  globe: icon('<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.4 2.3 3.6 5.2 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.2-3.6-8.5s1.2-6.2 3.6-8.5Z"/>'),
  menu: icon('<path d="M4 7h16M4 12h16M4 17h16"/>'),
  copy: icon('<rect x="8.5" y="8.5" width="11" height="11" rx="1.8"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>'),
  stop: icon('<rect x="6.5" y="6.5" width="11" height="11" rx="1.6" fill="currentColor" stroke="none"/>'),
  folder: icon('<path d="M3.5 7.2A1.7 1.7 0 0 1 5.2 5.5h3.4l2 2.2h8.2a1.7 1.7 0 0 1 1.7 1.7v8.1a1.7 1.7 0 0 1-1.7 1.7H5.2a1.7 1.7 0 0 1-1.7-1.7V7.2Z"/>'),
  terminal: icon('<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="m7.5 9.5 2.5 2.5-2.5 2.5M12.5 14.8h4"/>'),
  check: icon('<circle cx="12" cy="12" r="8.5"/><path d="m8.4 12.2 2.4 2.4 4.8-5"/>'),
  alert: icon('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.6v5.2M12 16.2v.2"/>'),
  info: icon('<circle cx="12" cy="12" r="8.5"/><path d="M12 11.2v5M12 8v.2"/>'),
  file: icon('<path d="M7 3.5h6.2L18.5 8.7V19A1.5 1.5 0 0 1 17 20.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5Z"/><path d="M13 3.8V9h5.2"/>'),
  refresh: icon('<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.7 4.5v4.2h-4.2"/>'),
  arrowRight: icon('<path d="M4.5 12h15"/><path d="m14 6.5 5.5 5.5L14 17.5"/>'),
  logout: icon('<path d="M14.5 4.5H18a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5h-3.5"/><path d="M4.5 12H15"/><path d="m11 7.5 4.5 4.5L11 16.5"/>'),
  cpu: icon('<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.8 9.8h4.4v4.4H9.8z"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3"/>'),
  eye: icon('<path d="M2.8 12s3.4-6 9.2-6 9.2 6 9.2 6-3.4 6-9.2 6-9.2-6-9.2-6Z"/><circle cx="12" cy="12" r="2.7"/>'),
  eyeOff: icon('<path d="m4 4 16 16"/><path d="M9.9 5.2A9.6 9.6 0 0 1 12 5c5.8 0 9.2 7 9.2 7a15.6 15.6 0 0 1-3.2 4.1"/><path d="M6.2 6.7C4 8.4 2.8 12 2.8 12s3.4 7 9.2 7a9.7 9.7 0 0 0 4-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
  chevronDown: icon('<path d="m6 9.5 6 6 6-6"/>'),
  arrowUpRight: icon('<path d="M7 17 17 7"/><path d="M8.5 7H17v8.5"/>'),
};

export const logoMark = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.6" y="3.6" width="16.8" height="16.8" rx="3.2"/><path d="M7.6 8.6h8.8M7.6 12.2h5.4"/><circle cx="15.7" cy="15.6" r="1.35" fill="currentColor" stroke="none"/></svg>`;

/* ---------------------------------------------------------------- toast */

function toastRoot() {
  let root = document.getElementById("toast-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "toast-root";
    root.className = "toast-root";
    document.body.appendChild(root);
  }
  return root;
}

export function toast(message, type = "info") {
  const root = toastRoot();
  while (root.children.length >= 4) root.firstElementChild.remove();
  const node = document.createElement("div");
  node.className = `toast toast-${type}`;
  const glyph = type === "success" ? icons.check : type === "error" ? icons.alert : icons.info;
  node.innerHTML = `${glyph}<div>${escapeHtml(message)}</div>`;
  root.appendChild(node);
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 220);
  }, 4200);
}

/* ---------------------------------------------------------------- dialogs */

export function openDialog({ title, subtitle = "", bodyHtml = "", footerHtml = "", onMount }) {
  const dialog = document.createElement("dialog");
  dialog.innerHTML = `
    <div class="modal-head">
      <h3 class="modal-title">${escapeHtml(title)}</h3>
      ${subtitle ? `<p class="modal-sub">${escapeHtml(subtitle)}</p>` : ""}
    </div>
    <form class="modal-form" novalidate>
      <div class="modal-body">
        <p class="modal-error" data-error role="alert"></p>
        ${bodyHtml}
      </div>
      <div class="modal-foot">${footerHtml}</div>
    </form>`;
  document.body.appendChild(dialog);
  const close = () => {
    if (dialog.open) dialog.close();
    else dialog.remove();
  };
  const showError = (message) => {
    const error = dialog.querySelector("[data-error]");
    error.textContent = message || "";
    error.classList.toggle("show", Boolean(message));
  };
  const form = dialog.querySelector("form");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const submit = dialog.querySelector('[data-submit="true"]');
    if (submit && typeof submit.__onSubmit === "function") submit.__onSubmit();
  });
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
  if (onMount) {
    onMount({
      dialog,
      close,
      showError,
      form,
      body: dialog.querySelector(".modal-body"),
      footer: dialog.querySelector(".modal-foot"),
    });
  }
  return { dialog, close, showError, form };
}

export function confirmDialog({
  title,
  message,
  confirmText = t("common.confirmDelete"),
  cancelText = t("common.cancel"),
  danger = false,
}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const { dialog, close } = openDialog({
      title,
      bodyHtml: `<p class="meta-empty" style="font-size:13px;color:var(--ink-2)">${escapeHtml(message)}</p>`,
      footerHtml: `
        <button type="button" class="btn btn-secondary" data-act="cancel">${escapeHtml(cancelText)}</button>
        <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" data-act="ok">${escapeHtml(confirmText)}</button>`,
      onMount: ({ dialog: host }) => {
        host.querySelector('[data-act="cancel"]').addEventListener("click", () => {
          finish(false);
          close();
        });
        host.querySelector('[data-act="ok"]').addEventListener("click", () => {
          finish(true);
          close();
        });
        host.addEventListener("cancel", () => finish(false));
        host.querySelector('[data-act="ok"]').focus();
      },
    });
    dialog.addEventListener("close", () => finish(false));
  });
}

/* ---------------------------------------------------------------- misc */

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to legacy path */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

export function pagerHtml(pageInfo) {
  if (!pageInfo || !pageInfo.pages || pageInfo.pages <= 1) return "";
  const { current, pages, total } = pageInfo;
  return `
    <div class="pager">
      <span>${escapeHtml(t("common.pageInfo", { current, pages, total }))}</span>
      <span class="grow"></span>
      <button type="button" class="btn btn-secondary btn-sm" data-page="${current - 1}" ${
        current <= 1 ? "disabled" : ""
      }>${escapeHtml(t("common.prev"))}</button>
      <button type="button" class="btn btn-secondary btn-sm" data-page="${current + 1}" ${
        current >= pages ? "disabled" : ""
      }>${escapeHtml(t("common.next"))}</button>
    </div>`;
}

export function initials(name) {
  const text = String(name || "").trim();
  if (!text) return "?";
  const chars = [...text];
  return chars.slice(0, 2).join("").toUpperCase();
}

export function debounce(fn, delay = 300) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}
