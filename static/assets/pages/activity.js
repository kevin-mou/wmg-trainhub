import { http } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml,
  formatDateTime,
  formatRelative,
  icons,
  pagerHtml,
} from "../ui.js";

const ACTION_KEYS = {
  "dataset.upload": "activity.action.datasetUpload",
  "dataset.delete": "activity.action.datasetDelete",
  "dataset.download": "activity.action.datasetDownload",
  "notebook.upload": "activity.action.notebookUpload",
  "notebook.delete": "activity.action.notebookDelete",
  "notebook.download": "activity.action.notebookDownload",
  "run.create": "activity.action.runCreate",
  "run.cancel": "activity.action.runCancel",
  "run.delete": "activity.action.runDelete",
  "log.download": "activity.action.logDownload",
  "outputs.download": "activity.action.outputsDownload",
  "output.download": "activity.action.outputDownload",
};

const ACTION_ICONS = {
  "dataset.upload": "datasets",
  "dataset.delete": "trash",
  "dataset.download": "download",
  "notebook.upload": "notebooks",
  "notebook.delete": "trash",
  "notebook.download": "download",
  "run.create": "runs",
  "run.cancel": "stop",
  "run.delete": "trash",
  "log.download": "download",
  "outputs.download": "download",
  "output.download": "download",
};

export async function renderActivity(root, ctx) {
  let disposed = false;
  let page = 1;
  const size = 20;

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">${escapeHtml(t("activity.title"))}</h1>
        <p class="page-sub">${escapeHtml(t("activity.subtitle"))}</p>
      </div>
    </div>
    <section class="panel">
      <div id="act-table"><div class="loading">${escapeHtml(t("common.loading"))}</div></div>
      <div id="act-pager"></div>
    </section>`;

  const tableHost = root.querySelector("#act-table");
  const pagerHost = root.querySelector("#act-pager");

  async function load(targetPage = page) {
    page = targetPage;
    try {
      const data = await http.get(`/api/activity?page=${page}&size=${size}`);
      if (disposed || !root.isConnected) return;
      renderTable(data.records);
      pagerHost.innerHTML = pagerHtml(data);
      pagerHost.querySelectorAll("[data-page]").forEach((button) => {
        button.addEventListener("click", () => load(Number(button.dataset.page)));
      });
    } catch (error) {
      if (disposed) return;
      tableHost.innerHTML = `<div class="empty"><div class="empty-title">${escapeHtml(
        t("errors.loadFailed", { message: error.message })
      )}</div></div>`;
    }
  }

  function renderTable(records) {
    if (!records.length) {
      tableHost.innerHTML = `<div class="empty">
        <div class="empty-icon">${icons.activity}</div>
        <div class="empty-title">${escapeHtml(t("activity.empty.title"))}</div>
        <div class="empty-text">${escapeHtml(t("activity.empty.text"))}</div>
      </div>`;
      return;
    }
    tableHost.innerHTML = `<div class="table-scroll"><table class="table">
      <thead><tr>
        <th>${escapeHtml(t("activity.col.time"))}</th>
        <th>${escapeHtml(t("activity.col.action"))}</th>
        <th>${escapeHtml(t("activity.col.target"))}</th>
        <th>${escapeHtml(t("activity.col.detail"))}</th>
        <th>${escapeHtml(t("activity.col.ip"))}</th>
      </tr></thead>
      <tbody>
        ${records
          .map(
            (item) => `<tr>
              <td class="muted nowrap" title="${escapeHtml(formatDateTime(item.createdAt))}">${escapeHtml(
                formatRelative(item.createdAt)
              )}</td>
              <td>
                <div class="cell-main" style="font-weight:500">
                  <span class="row-chevron" style="opacity:1">${icons[ACTION_ICONS[item.action] || "activity"]}</span>
                  ${escapeHtml(t(ACTION_KEYS[item.action] || "activity.title"))}
                </div>
              </td>
              <td class="mono muted nowrap">${escapeHtml(item.targetId || "—")}</td>
              <td>${escapeHtml(item.detail || "—")}</td>
              <td class="mono muted nowrap">${escapeHtml(item.ip || "—")}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table></div>`;
  }

  await load(1);
  return () => {
    disposed = true;
  };
}
