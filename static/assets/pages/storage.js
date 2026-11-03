import { http } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml,
  formatBytes,
  formatTime,
  icons,
  statusBadge,
} from "../ui.js";

function sharePercent(bytes, total) {
  const value = Number(bytes) || 0;
  const whole = Number(total) || 0;
  return whole > 0 ? Math.round((value / whole) * 100) : 0;
}

function shareBar(bytes, total) {
  const percent = sharePercent(bytes, total);
  return `<div class="storage-share" title="${percent}%"><span style="width:${percent}%"></span></div><span class="muted">${percent}%</span>`;
}

export async function renderStorage(root, ctx) {
  let disposed = false;

  root.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;

  async function load() {
    try {
      const data = await http.get("/api/storage");
      if (disposed || !root.isConnected) return;
      render(data);
    } catch (error) {
      if (disposed) return;
      root.innerHTML = `<div class="panel"><div class="empty">
        <div class="empty-title">${escapeHtml(t("errors.loadFailed", { message: error.message }))}</div>
        <button class="btn btn-secondary" id="storage-retry">${escapeHtml(
          t("common.retry")
        )}</button>
      </div></div>`;
      root.querySelector("#storage-retry")?.addEventListener("click", () => {
        root.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;
        load();
      });
    }
  }

  function render(data) {
    const { customerBytes, disk, categories, details } = data;
    const diskPercent = sharePercent(disk.usedBytes, disk.totalBytes);
    const categoryBytes = Object.fromEntries(
      categories.map((item) => [item.key, item.bytes])
    );

    const categoryRows = categories
      .filter((item) => item.key !== "other" || item.bytes > 0)
      .map(
        (item) => `<tr>
          <td>${escapeHtml(t(`storage.category.${item.key}`))}</td>
          <td class="mono">${item.count}</td>
          <td class="mono">${formatBytes(item.bytes)}</td>
          <td>${shareBar(item.bytes, customerBytes)}</td>
        </tr>`
      )
      .join("");

    root.innerHTML = `
      <div class="page-head">
        <div class="page-head-text">
          <h1 class="page-title">${escapeHtml(t("storage.title"))}</h1>
          <p class="page-sub">${escapeHtml(t("storage.subtitle"))}</p>
        </div>
        <div class="page-head-actions">
          <button type="button" class="btn btn-secondary" id="storage-refresh">${icons.refresh}${escapeHtml(
            t("storage.refresh")
          )}</button>
        </div>
      </div>

      <div class="ledger">
        <div class="ledger-item">
          <div class="ledger-value">${formatBytes(customerBytes)}</div>
          <div class="ledger-label">${escapeHtml(t("storage.summary.customer"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${formatBytes(disk.freeBytes)}</div>
          <div class="ledger-label">${escapeHtml(t("storage.summary.free"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${formatBytes(disk.totalBytes)}</div>
          <div class="ledger-label">${escapeHtml(t("storage.summary.total"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${diskPercent}%</div>
          <div class="ledger-label">${escapeHtml(t("storage.summary.diskUsed"))}</div>
        </div>
      </div>

      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">${escapeHtml(t("storage.breakdown.title"))}</span>
        </div>
        <div class="table-scroll"><table class="table">
          <thead><tr>
            <th>${escapeHtml(t("storage.col.category"))}</th>
            <th>${escapeHtml(t("storage.col.count"))}</th>
            <th>${escapeHtml(t("storage.col.size"))}</th>
            <th>${escapeHtml(t("storage.col.share"))}</th>
          </tr></thead>
          <tbody>${categoryRows}</tbody>
        </table></div>
      </section>

      ${detailPanel({
        key: "datasets",
        title: t("storage.detail.datasets"),
        empty: t("storage.empty.datasets"),
        href: "#/datasets",
        detail: details.datasets,
        totalBytes: categoryBytes.datasets,
        columns: [
          t("datasets.col.name"),
          t("datasets.col.files"),
          t("datasets.col.size"),
          t("datasets.col.created"),
        ],
        rows: details.datasets.items
          .map(
            (item) => `<tr class="clickable" data-open="/datasets">
              <td><span class="truncate">${escapeHtml(item.name)}</span></td>
              <td class="mono">${item.fileCount}</td>
              <td class="mono">${formatBytes(item.bytes)}</td>
              <td class="muted">${formatTime(item.createdAt)}</td>
            </tr>`
          )
          .join(""),
      })}

      ${detailPanel({
        key: "notebooks",
        title: t("storage.detail.notebooks"),
        empty: t("storage.empty.notebooks"),
        href: "#/notebooks",
        detail: details.notebooks,
        totalBytes: categoryBytes.notebooks,
        columns: [
          t("notebooks.col.name"),
          t("notebooks.col.cells"),
          t("storage.col.size"),
          t("storage.col.updated"),
        ],
        rows: details.notebooks.items
          .map(
            (item) => `<tr class="clickable" data-open="/notebooks/${encodeURIComponent(item.id)}/edit">
              <td><span class="truncate">${escapeHtml(item.name)}</span></td>
              <td class="mono">${item.cellCount}</td>
              <td class="mono">${formatBytes(item.bytes)}</td>
              <td class="muted">${formatTime(item.updatedAt)}</td>
            </tr>`
          )
          .join(""),
      })}

      ${detailPanel({
        key: "runs",
        title: t("storage.detail.runs"),
        empty: t("storage.empty.runs"),
        href: "#/runs",
        detail: details.runs,
        totalBytes: categoryBytes.runs,
        columns: [
          t("runs.col.status"),
          t("runs.col.name"),
          t("storage.col.size"),
          t("runs.col.created"),
        ],
        rows: details.runs.items
          .map(
            (item) => `<tr class="clickable" data-open="/runs/${encodeURIComponent(item.id)}">
              <td>${statusBadge(item.status)}</td>
              <td>
                <div class="cell-main"><span class="truncate">${escapeHtml(item.name)}</span></div>
                <div class="cell-sub">${escapeHtml(item.notebookName || "—")}</div>
              </td>
              <td class="mono">${formatBytes(item.bytes)}</td>
              <td class="muted">${formatTime(item.createdAt)}</td>
            </tr>`
          )
          .join(""),
      })}`;

    root.querySelector("#storage-refresh")?.addEventListener("click", () => {
      root.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;
      load();
    });
    root.querySelectorAll("[data-open]").forEach((row) => {
      row.addEventListener("click", () => ctx.navigate(row.dataset.open));
    });
  }

  await load();
  return () => {
    disposed = true;
  };
}

function detailPanel({ key, title, empty, href, detail, totalBytes, columns, rows }) {
  const head = `<div class="panel-head">
      <span class="panel-title">${escapeHtml(title)}</span>
      <span class="panel-sub">${escapeHtml(t("storage.items", { count: detail.total }))} · ${formatBytes(
        totalBytes
      )}</span>
      <a class="btn-link" style="margin-left:auto" href="${href}">${escapeHtml(
        t("common.viewAll")
      )}</a>
    </div>`;
  if (!rows) {
    return `<section class="panel" data-detail="${key}">
      ${head}
      <div class="empty"><div class="empty-title">${escapeHtml(empty)}</div></div>
    </section>`;
  }
  return `<section class="panel" data-detail="${key}">
    ${head}
    <div class="table-scroll"><table class="table">
      <thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${
      detail.truncated
        ? `<div class="meta-empty" style="padding:10px 16px">${escapeHtml(
            t("storage.truncated", { limit: detail.items.length })
          )}</div>`
        : ""
    }
  </section>`;
}
