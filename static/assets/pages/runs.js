import { http } from "../api.js";
import { t } from "../i18n.js";
import {
  debounce,
  escapeHtml,
  formatDuration,
  formatTime,
  icons,
  pagerHtml,
  statusBadge,
  toast,
} from "../ui.js";
import { openRunModal } from "./run-modal.js";

const FILTERS = ["all", "queued", "running", "succeeded", "failed", "cancelled"];

export async function renderRuns(root, ctx) {
  let disposed = false;
  let page = 1;
  const size = 20;
  let status = "all";
  let query = "";
  let records = [];
  let refreshTimer = null;

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">${escapeHtml(t("runs.title"))}</h1>
        <p class="page-sub">${escapeHtml(t("runs.subtitle"))}</p>
      </div>
      <div class="page-head-actions">
        <button type="button" class="btn btn-primary" id="runs-submit">${icons.plus}${escapeHtml(
          t("runs.submit")
        )}</button>
      </div>
    </div>
    <section class="panel">
      <div class="panel-head">
        <div class="segmented" id="runs-filters">
          ${FILTERS.map(
            (key) =>
              `<button type="button" data-status="${key}" class="${
                key === "all" ? "active" : ""
              }">${escapeHtml(t(`runs.filters.${key}`))}</button>`
          ).join("")}
        </div>
        <div class="search" style="margin-left:auto">
          ${icons.search}
          <input id="runs-search" type="search" placeholder="${escapeHtml(
            t("runs.searchPlaceholder")
          )}" />
        </div>
      </div>
      <div id="runs-table"><div class="loading">${escapeHtml(t("common.loading"))}</div></div>
      <div id="runs-pager"></div>
    </section>`;

  const tableHost = root.querySelector("#runs-table");
  const pagerHost = root.querySelector("#runs-pager");
  const searchInput = root.querySelector("#runs-search");

  root.querySelector("#runs-submit").addEventListener("click", () =>
    openRunModal({ navigate: ctx.navigate })
  );

  root.querySelectorAll("#runs-filters [data-status]").forEach((button) => {
    button.addEventListener("click", () => {
      status = button.dataset.status;
      root.querySelectorAll("#runs-filters [data-status]").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      load(1);
    });
  });

  searchInput.addEventListener(
    "input",
    debounce(() => {
      query = searchInput.value.trim();
      load(1);
    }, 320)
  );
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      query = searchInput.value.trim();
      load(1);
    }
  });

  function scheduleRefresh() {
    clearInterval(refreshTimer);
    const hasActive = records.some(
      (item) => item.status === "running" || item.status === "queued"
    );
    if (hasActive) {
      refreshTimer = setInterval(() => load(page, { silent: true }), 4000);
    }
  }

  async function load(targetPage = page, { silent = false } = {}) {
    page = targetPage;
    if (!silent && !records.length) {
      tableHost.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;
    }
    const params = new URLSearchParams({ page: String(page), size: String(size) });
    if (status !== "all") params.set("status", status);
    if (query) params.set("q", query);
    try {
      const data = await http.get(`/api/runs?${params.toString()}`);
      if (disposed || !root.isConnected) return;
      records = data.records;
      renderTable(records);
      pagerHost.innerHTML = pagerHtml(data);
      pagerHost.querySelectorAll("[data-page]").forEach((button) => {
        button.addEventListener("click", () => load(Number(button.dataset.page)));
      });
      scheduleRefresh();
    } catch (error) {
      if (disposed) return;
      tableHost.innerHTML = `<div class="empty"><div class="empty-title">${escapeHtml(
        t("errors.loadFailed", { message: error.message })
      )}</div></div>`;
    }
  }

  function renderTable(items) {
    if (!items.length) {
      tableHost.innerHTML = `<div class="empty">
        <div class="empty-icon">${icons.runs}</div>
        <div class="empty-title">${escapeHtml(t("runs.empty.title"))}</div>
        <div class="empty-text">${escapeHtml(t("runs.empty.text"))}</div>
        <button class="btn btn-primary" id="runs-empty-submit">${escapeHtml(
          t("runs.empty.action")
        )}</button>
      </div>`;
      tableHost
        .querySelector("#runs-empty-submit")
        ?.addEventListener("click", () => openRunModal({ navigate: ctx.navigate }));
      return;
    }

    tableHost.innerHTML = `<div class="table-scroll"><table class="table">
      <thead><tr>
        <th>${escapeHtml(t("runs.col.status"))}</th>
        <th style="width:30%">${escapeHtml(t("runs.col.name"))}</th>
        <th>${escapeHtml(t("runs.col.dataset"))}</th>
        <th>${escapeHtml(t("runs.col.kernel"))}</th>
        <th>${escapeHtml(t("runs.col.duration"))}</th>
        <th>${escapeHtml(t("runs.col.created"))}</th>
        <th></th>
      </tr></thead>
      <tbody>
        ${items
          .map(
            (run) => `<tr class="clickable" data-run="${escapeHtml(run.id)}">
              <td>${statusBadge(run.status)}</td>
              <td>
                <div class="cell-main"><span class="truncate">${escapeHtml(run.name)}</span></div>
                <div class="cell-sub mono">${escapeHtml(run.notebookName || "—")}</div>
              </td>
              <td class="muted">${escapeHtml(run.datasetName || t("runs.noDataset"))}</td>
              <td class="mono">${escapeHtml(run.kernel || "—")}</td>
              <td class="mono">${formatDuration(run.durationSeconds)}</td>
              <td class="muted nowrap">${formatTime(run.createdAt)}</td>
              <td><span class="row-chevron">${icons.chevronRight}</span></td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table></div>`;

    tableHost.querySelectorAll("[data-run]").forEach((row) => {
      row.addEventListener("click", () => ctx.navigate(`/runs/${row.dataset.run}`));
    });
  }

  await load(1);
  if (ctx.query && ctx.query.get("new") === "1") {
    openRunModal({ navigate: ctx.navigate });
  }

  return () => {
    disposed = true;
    clearInterval(refreshTimer);
  };
}
