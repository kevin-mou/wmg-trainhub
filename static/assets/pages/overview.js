import { http } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml,
  formatBytes,
  formatDuration,
  formatRelative,
  formatTime,
  icons,
  statusBadge,
  toast,
} from "../ui.js";
import { openRunModal } from "./run-modal.js";

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

function elapsedText(run) {
  const started = new Date(run.startedAt || run.createdAt).getTime();
  const seconds = Math.max(0, (Date.now() - started) / 1000);
  return run.status === "queued"
    ? t("overview.live.queuedElapsed", { time: formatDuration(seconds) })
    : t("overview.live.elapsed", { time: formatDuration(seconds) });
}

export async function renderOverview(root, ctx) {
  let disposed = false;
  let refreshTimer = null;
  let tickTimer = null;
  let tailTimer = null;

  root.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;

  const clearTimers = () => {
    clearInterval(refreshTimer);
    clearInterval(tickTimer);
    clearInterval(tailTimer);
  };

  const lastLogLine = (text) => {
    const lines = String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    return lines.length ? lines[lines.length - 1] : "";
  };

  async function load() {
    try {
      const [data, activity] = await Promise.all([
        http.get("/api/overview"),
        http.get("/api/activity?page=1&size=6"),
      ]);
      if (disposed || !root.isConnected) return;
      render(data, activity);
    } catch (error) {
      if (disposed) return;
      root.innerHTML = `<div class="panel"><div class="empty">
        <div class="empty-title">${escapeHtml(t("errors.loadFailed", { message: error.message }))}</div>
        <button class="btn btn-secondary" id="overview-retry">${escapeHtml(
          t("common.retry")
        )}</button>
      </div></div>`;
      root.querySelector("#overview-retry")?.addEventListener("click", () => {
        root.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;
        load();
      });
    }
  }

  function render(data, activity) {
    clearTimers();
    const { stats, storage, recentRuns, activeRuns, customer } = data;
    const active = activeRuns && activeRuns.length ? activeRuns[0] : null;
    const successRate =
      stats.successRate == null ? "—" : `${Math.round(stats.successRate * 100)}%`;

    const liveBlock = active
      ? `<div class="live-band">
          <div style="min-width:0">
            <div class="live-eyebrow">
              ${statusBadge(active.status)}
              <span>${escapeHtml(
                active.status === "queued" ? t("status.queued") : t("overview.live.running")
              )}</span>
            </div>
            <div class="live-title">${escapeHtml(active.name)}</div>
            <div class="live-meta">
              <span>${escapeHtml(active.notebookName || "—")}</span>
              <span>${escapeHtml(active.datasetName || t("runs.noDataset"))}</span>
              <span>${escapeHtml(active.kernel || "—")}</span>
              <span id="live-elapsed">${escapeHtml(elapsedText(active))}</span>
            </div>
            <div class="live-tail" id="live-tail">${escapeHtml(t("overview.live.tailEmpty"))}</div>
          </div>
          <div class="live-actions">
            <button type="button" class="btn btn-primary" id="live-open">${escapeHtml(
              t("overview.live.open")
            )}</button>
            <button type="button" class="btn btn-ghost" id="live-cancel">${escapeHtml(
              t("overview.live.cancel")
            )}</button>
          </div>
        </div>`
      : `<div class="idle-band">
          <div class="idle-icon">${icons.cpu}</div>
          <div>
            <div class="idle-title">${escapeHtml(t("overview.live.idleTitle"))}</div>
            <div class="idle-text">${escapeHtml(t("overview.live.idleText"))}</div>
          </div>
          <button type="button" class="btn btn-secondary" id="idle-submit">${escapeHtml(
            t("overview.actions.submitRun")
          )}</button>
        </div>`;

    root.innerHTML = `
      <div class="page-head">
        <div class="page-head-text">
          <h1 class="page-title">${escapeHtml(t("overview.title"))}</h1>
          <p class="page-sub">${escapeHtml(t("overview.subtitle"))}</p>
        </div>
        <div class="page-head-actions">
          <button type="button" class="btn btn-secondary" id="ov-upload">${icons.upload}${escapeHtml(
            t("overview.actions.uploadDataset")
          )}</button>
          <button type="button" class="btn btn-primary" id="ov-submit">${icons.runs}${escapeHtml(
            t("overview.actions.submitRun")
          )}</button>
        </div>
      </div>

      ${liveBlock}

      <div class="ledger">
        <div class="ledger-item">
          <div class="ledger-value">${stats.datasets}</div>
          <div class="ledger-label">${escapeHtml(t("overview.stats.datasets"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${stats.notebooks}</div>
          <div class="ledger-label">${escapeHtml(t("overview.stats.notebooks"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${stats.runs}${
            stats.activeRuns ? `<small>+${stats.activeRuns}</small>` : ""
          }</div>
          <div class="ledger-label">${escapeHtml(t("overview.stats.runs"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${successRate}</div>
          <div class="ledger-label">${escapeHtml(t("overview.stats.successRate"))}</div>
        </div>
        <div class="ledger-item">
          <div class="ledger-value">${formatBytes(storage.customerBytes)}</div>
          <div class="ledger-label" title="${escapeHtml(
            `free ${formatBytes(storage.disk.freeBytes)}`
          )}">${escapeHtml(t("overview.stats.storage"))}</div>
        </div>
      </div>

      <div class="overview-grid">
        <section class="panel">
          <div class="panel-head">
            <span class="panel-title">${escapeHtml(t("overview.recent.title"))}</span>
            <a class="btn-link" href="#/runs">${escapeHtml(t("common.viewAll"))}</a>
          </div>
          ${
            recentRuns.length
              ? `<div class="table-scroll"><table class="table">
                  <thead><tr>
                    <th>${escapeHtml(t("runs.col.status"))}</th>
                    <th>${escapeHtml(t("runs.col.name"))}</th>
                    <th>${escapeHtml(t("runs.col.duration"))}</th>
                    <th>${escapeHtml(t("runs.col.created"))}</th>
                  </tr></thead>
                  <tbody>
                    ${recentRuns
                      .map(
                        (run) => `<tr class="clickable" data-run="${escapeHtml(run.id)}">
                          <td>${statusBadge(run.status)}</td>
                          <td>
                            <div class="cell-main"><span class="truncate">${escapeHtml(
                              run.name
                            )}</span></div>
                            <div class="cell-sub">${escapeHtml(run.notebookName || "—")}</div>
                          </td>
                          <td class="mono">${formatDuration(run.durationSeconds)}</td>
                          <td class="muted">${formatTime(run.createdAt)}</td>
                        </tr>`
                      )
                      .join("")}
                  </tbody>
                </table></div>`
              : `<div class="empty">
                  <div class="empty-icon">${icons.runs}</div>
                  <div class="empty-title">${escapeHtml(t("overview.recent.empty"))}</div>
                  <button class="btn btn-primary" id="ov-empty-submit">${escapeHtml(
                    t("overview.recent.emptyCta")
                  )}</button>
                </div>`
          }
        </section>

        <section class="panel">
          <div class="panel-head">
            <span class="panel-title">${escapeHtml(t("overview.activity.title"))}</span>
            <a class="btn-link" href="#/activity">${escapeHtml(t("common.viewAll"))}</a>
          </div>
          ${
            activity.records.length
              ? `<div class="feed">
                  ${activity.records
                    .map(
                      (item) => `<div class="feed-item">
                        <div class="feed-icon">${icons[ACTION_ICONS[item.action] || "activity"]}</div>
                        <div class="feed-text">
                          <strong>${escapeHtml(t(ACTION_KEYS[item.action] || "activity.title"))}</strong>
                          ${item.detail ? `<div class="muted">${escapeHtml(item.detail)}</div>` : ""}
                        </div>
                        <div class="feed-time">${formatRelative(item.createdAt)}</div>
                      </div>`
                    )
                    .join("")}
                </div>`
              : `<div class="empty" style="padding:40px 20px">
                  <div class="empty-title">${escapeHtml(t("overview.activity.empty"))}</div>
                </div>`
          }
        </section>
      </div>`;

    root.querySelector("#ov-upload")?.addEventListener("click", () => ctx.navigate("/datasets"));
    root.querySelector("#ov-submit")?.addEventListener("click", () => submitRun());
    root.querySelector("#idle-submit")?.addEventListener("click", () => submitRun());
    root.querySelector("#ov-empty-submit")?.addEventListener("click", () => submitRun());
    root.querySelectorAll("[data-run]").forEach((row) => {
      row.addEventListener("click", () => ctx.navigate(`/runs/${row.dataset.run}`));
    });

    if (active) {
      root.querySelector("#live-open")?.addEventListener("click", () =>
        ctx.navigate(`/runs/${active.id}`)
      );
      root.querySelector("#live-cancel")?.addEventListener("click", async () => {
        try {
          await http.post(`/api/runs/${active.id}/cancel`);
          toast(t("toast.runCancelled"), "success");
          load();
        } catch (error) {
          toast(error.message, "error");
        }
      });

      const elapsedNode = root.querySelector("#live-elapsed");
      tickTimer = setInterval(() => {
        if (elapsedNode && elapsedNode.isConnected) elapsedNode.textContent = elapsedText(active);
      }, 1000);

      const tailNode = root.querySelector("#live-tail");
      const loadTail = async () => {
        try {
          const logData = await http.get(
            `/api/runs/${active.id}/logs?tail=true&limit=600&offset=0`
          );
          if (!disposed && tailNode && tailNode.isConnected) {
            const line = lastLogLine(logData.content);
            tailNode.textContent = line || t("overview.live.tailEmpty");
          }
        } catch {
          /* 日志尾部是锦上添花，失败不打扰 */
        }
      };
      loadTail();
      tailTimer = setInterval(loadTail, 4000);
    }

    if (stats.activeRuns > 0) {
      refreshTimer = setInterval(load, 10000);
    }
  }

  function submitRun() {
    openRunModal({ navigate: ctx.navigate });
  }

  await load();
  return () => {
    disposed = true;
    clearTimers();
  };
}
