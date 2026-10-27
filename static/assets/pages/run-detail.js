import { download, http } from "../api.js";
import { t } from "../i18n.js";
import { renderMetricsChart } from "../metrics-chart.js";
import {
  confirmDialog,
  escapeHtml,
  formatBytes,
  formatDateTime,
  formatDuration,
  icons,
  statusBadge,
  toast,
} from "../ui.js";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export async function renderRunDetail(root, ctx) {
  const runId = ctx.params.id;
  let disposed = false;
  let run = null;
  let logOffset = 0;
  let logComplete = false;
  let metricsKey = "";
  let follow = true;
  let pollTimer = null;
  let ticks = 0;
  let failures = 0;

  root.innerHTML = `
    <div class="run-head" style="margin-bottom:10px">
      <a class="back-link" href="#/runs">${icons.chevronLeft}${escapeHtml(
        t("runDetail.back")
      )}</a>
    </div>
    <div class="run-head">
      <h1 class="run-title" id="run-name">…</h1>
      <div class="run-head-meta" id="run-status"></div>
      <div class="run-head-actions" id="run-actions"></div>
    </div>
    <section class="panel metrics-panel" id="metrics-panel" hidden>
      <div class="metrics-panel-head">
        <div class="meta-title">${escapeHtml(t("runDetail.metrics"))}</div>
        <div class="metrics-meta" id="metrics-meta"></div>
      </div>
      <div class="metrics-chart" id="metrics-chart"></div>
    </section>
    <div class="run-layout">
      <section class="log-panel">
        <div class="log-bar">
          <span class="file">run.log</span>
          <span id="log-size" class="muted">0 B</span>
          <span class="spacer"></span>
          <label class="log-toggle active" id="follow-toggle">
            <input type="checkbox" id="follow-check" checked />
            <span>${escapeHtml(t("runDetail.follow"))}</span>
          </label>
          <button type="button" class="btn btn-ghost" id="log-download">${icons.download}${escapeHtml(
            t("runDetail.downloadLog")
          )}</button>
        </div>
        <div class="log-wrap">
          <div class="log-view" id="log-view" data-empty="${escapeHtml(
            t("runDetail.logEmpty")
          )}"></div>
          <button type="button" class="btn btn-secondary btn-sm log-jump" id="log-jump">${escapeHtml(
            t("runDetail.jumpBottom")
          )}</button>
        </div>
      </section>
      <aside class="run-meta">
        <div class="panel">
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("runDetail.info"))}</div>
            <dl class="kv" id="meta-info"></dl>
            <div id="meta-message"></div>
          </div>
        </div>
        <div class="panel" id="environment-panel" hidden>
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("runDetail.environment"))}</div>
            <dl class="kv" id="meta-environment"></dl>
          </div>
        </div>
        <div class="panel">
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("runDetail.params"))}</div>
            <div id="meta-params" class="params-list"></div>
          </div>
        </div>
        <div class="panel">
          <div class="meta-section">
            <div class="meta-title" style="display:flex;align-items:center;gap:10px">
              <span>${escapeHtml(t("runDetail.outputs"))}</span>
              <button type="button" class="btn-link" id="outputs-zip" style="margin-left:auto;font-size:12px">${escapeHtml(
                t("runDetail.downloadAll")
              )}</button>
            </div>
            <div id="meta-outputs"></div>
          </div>
        </div>
      </aside>
    </div>`;

  const refs = {
    name: root.querySelector("#run-name"),
    status: root.querySelector("#run-status"),
    actions: root.querySelector("#run-actions"),
    logView: root.querySelector("#log-view"),
    logSize: root.querySelector("#log-size"),
    followCheck: root.querySelector("#follow-check"),
    followWrap: root.querySelector("#follow-toggle"),
    jump: root.querySelector("#log-jump"),
    logDownload: root.querySelector("#log-download"),
    metricsPanel: root.querySelector("#metrics-panel"),
    metricsMeta: root.querySelector("#metrics-meta"),
    metricsChart: root.querySelector("#metrics-chart"),
    environmentPanel: root.querySelector("#environment-panel"),
    environment: root.querySelector("#meta-environment"),
    info: root.querySelector("#meta-info"),
    message: root.querySelector("#meta-message"),
    params: root.querySelector("#meta-params"),
    outputs: root.querySelector("#meta-outputs"),
    zip: root.querySelector("#outputs-zip"),
  };

  const nearBottom = () =>
    refs.logView.scrollHeight - refs.logView.scrollTop - refs.logView.clientHeight < 56;

  const updateJump = () => {
    refs.jump.style.display = nearBottom() ? "none" : "inline-flex";
  };

  refs.logView.addEventListener("scroll", updateJump, { passive: true });
  refs.followCheck.addEventListener("change", () => {
    follow = refs.followCheck.checked;
    refs.followWrap.classList.toggle("active", follow);
    if (follow) refs.logView.scrollTop = refs.logView.scrollHeight;
  });
  refs.jump.addEventListener("click", () => {
    refs.logView.scrollTop = refs.logView.scrollHeight;
    updateJump();
  });

  function appendLog(text) {
    if (!text) return;
    const stick = follow && nearBottom();
    refs.logView.appendChild(document.createTextNode(text));
    if (refs.logView.textContent.length > 2_000_000) {
      refs.logView.textContent = refs.logView.textContent.slice(-1_200_000);
    }
    if (stick) refs.logView.scrollTop = refs.logView.scrollHeight;
    updateJump();
  }

  function kvRow(label, valueHtml) {
    return `<dt>${escapeHtml(label)}</dt><dd>${valueHtml}</dd>`;
  }

  function renderActions() {
    if (!run) return;
    const active = run.status === "queued" || run.status === "running";
    refs.actions.innerHTML = active
      ? `<button type="button" class="btn btn-danger btn-sm" data-act="cancel">${icons.stop}${escapeHtml(
          t("runDetail.cancel")
        )}</button>`
      : `<button type="button" class="btn btn-danger btn-sm" data-act="delete">${icons.trash}${escapeHtml(
          t("runDetail.delete")
        )}</button>`;

    refs.actions.querySelector('[data-act="cancel"]')?.addEventListener("click", async () => {
      const okCancel = await confirmDialog({
        title: t("runDetail.cancelTitle"),
        message: t("runDetail.cancelText", { name: run.name }),
        confirmText: t("runDetail.cancelOk"),
        cancelText: t("runDetail.keepRunning"),
        danger: true,
      });
      if (!okCancel) return;
      try {
        await http.post(`/api/runs/${runId}/cancel`);
        toast(t("toast.runCancelled"), "success");
        await loadRun();
      } catch (error) {
        toast(error.message, "error");
      }
    });

    refs.actions.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
      const okDelete = await confirmDialog({
        title: t("runDetail.deleteTitle"),
        message: t("runDetail.deleteText", { name: run.name }),
        confirmText: t("common.delete"),
        danger: true,
      });
      if (!okDelete) return;
      try {
        await http.del(`/api/runs/${runId}`);
        toast(t("toast.deleted"), "success");
        ctx.navigate("/runs");
      } catch (error) {
        toast(error.message, "error");
      }
    });
  }

  function renderMeta() {
    refs.name.textContent = run.name;
    document.title = `${run.name} · TrainHub`;
    refs.status.innerHTML = `${statusBadge(run.status)}<span class="muted mono" style="font-size:11.5px">${escapeHtml(
      run.id
    )}</span>`;

    const datasetOwnerSuffix =
      run.datasetShared && run.datasetOwnerName
        ? ` · ${t("runs.sharedFrom", { name: run.datasetOwnerName })}`
        : "";
    refs.info.innerHTML = [
      kvRow(t("runDetail.notebook"), escapeHtml(run.notebookName || "—")),
      kvRow(
        t("runDetail.dataset"),
        escapeHtml(`${run.datasetName || t("runs.noDataset")}${datasetOwnerSuffix}`)
      ),
      kvRow(t("runDetail.kernel"), `<span class="mono">${escapeHtml(run.kernel || "—")}</span>`),
      kvRow(t("runDetail.created"), escapeHtml(formatDateTime(run.createdAt))),
      kvRow(t("runDetail.started"), escapeHtml(run.startedAt ? formatDateTime(run.startedAt) : "—")),
      kvRow(
        t("runDetail.finished"),
        escapeHtml(run.finishedAt ? formatDateTime(run.finishedAt) : "—")
      ),
      kvRow(
        t("runDetail.duration"),
        `<span class="mono" id="meta-duration">${formatDuration(run.durationSeconds)}</span>`
      ),
      kvRow(
        t("runDetail.exitCode"),
        `<span class="mono">${run.exitCode == null ? "—" : run.exitCode}</span>`
      ),
      kvRow(
        t("runDetail.timeout"),
        `<span class="mono">${
          run.timeoutSeconds ? formatDuration(run.timeoutSeconds) : escapeHtml(t("common.unlimited"))
        }</span>`
      ),
    ].join("");

    if (run.status === "failed" && run.message) {
      refs.message.innerHTML = `<div class="run-message">${escapeHtml(run.message)}</div>`;
    } else if (run.status === "cancelled" && run.message) {
      refs.message.innerHTML = `<div class="meta-empty" style="margin-top:10px">${escapeHtml(
        run.message
      )}</div>`;
    } else {
      refs.message.innerHTML = "";
    }

    const environment = run.environment;
    if (environment) {
      const service = environment.service || {};
      const kernel = environment.kernel || {};
      const gpuNames = (Array.isArray(environment.gpu) ? environment.gpu : [])
        .map((item) => (item && item.name ? String(item.name) : ""))
        .filter(Boolean);
      refs.environment.innerHTML = [
        kvRow(
          t("runDetail.pythonVersion"),
          `<span class="mono">${escapeHtml(service.pythonVersion || "—")}</span>`
        ),
        kvRow(t("runDetail.platform"), escapeHtml(service.platform || "—")),
        kvRow(
          t("runDetail.effectiveKernel"),
          `<span class="mono">${escapeHtml(
            kernel.effective || kernel.displayName || "—"
          )}</span>`
        ),
        kvRow(
          t("runDetail.gpu"),
          escapeHtml(gpuNames.length ? gpuNames.join(", ") : t("common.unavailable"))
        ),
        kvRow(
          t("runDetail.trainhubVersion"),
          `<span class="mono">${escapeHtml(environment.trainhubVersion || "—")}</span>`
        ),
      ].join("");
      refs.environmentPanel.hidden = false;
    } else {
      refs.environment.innerHTML = "";
      refs.environmentPanel.hidden = true;
    }

    const params = run.parameters || {};
    const entries = Object.entries(params);
    refs.params.innerHTML = entries.length
      ? entries
          .map(
            ([key, value]) => `<div class="params-row">
              <span class="k">${escapeHtml(key)}</span>
              <span class="v">${escapeHtml(
                typeof value === "string" ? value : JSON.stringify(value)
              )}</span>
            </div>`
          )
          .join("")
      : `<div class="meta-empty">${escapeHtml(t("runDetail.noParams"))}</div>`;

    const files = (run.outputs && run.outputs.files) || [];
    refs.outputs.innerHTML = files.length
      ? `<div class="output-list">${files
          .map(
            (file) => `<button type="button" class="output-item" data-path="${escapeHtml(
              file.path
            )}">
              ${icons.file}
              <span class="name">${escapeHtml(file.path)}</span>
              <span class="size">${formatBytes(file.sizeBytes)}</span>
            </button>`
          )
          .join("")}</div>`
      : `<div class="meta-empty">${escapeHtml(t("runDetail.outputsEmpty"))}</div>`;
    refs.zip.disabled = files.length === 0;

    refs.outputs.querySelectorAll("[data-path]").forEach((button) => {
      button.addEventListener("click", async () => {
        const relative = button.dataset.path;
        try {
          await download(
            `/api/runs/${runId}/outputs/file?path=${encodeURIComponent(relative)}`,
            relative.split("/").pop() || "artifact"
          );
          toast(t("toast.downloadStarted"), "info");
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });

    renderActions();
    refs.logSize.textContent = formatBytes(run.logBytes || 0);
    if (run.status === "running" && !run.startedAt) {
      /* no-op: startedAt always set while running */
    }
  }

  async function loadRun() {
    const data = await http.get(`/api/runs/${runId}`);
    if (disposed) return;
    run = data;
    renderMeta();
    return data;
  }

  async function pullLogs() {
    const data = await http.get(`/api/runs/${runId}/logs?offset=${logOffset}`);
    if (disposed) return;
    if (data.nextOffset < logOffset) {
      logOffset = 0;
      refs.logView.textContent = "";
    }
    appendLog(data.content);
    logOffset = data.nextOffset;
    refs.logSize.textContent = formatBytes(data.totalBytes);
    logComplete = data.completed && data.nextOffset >= data.totalBytes;
  }

  async function pullMetrics() {
    const data = await http.get(`/api/runs/${runId}/metrics`);
    if (disposed) return;
    const series = data && Array.isArray(data.series) ? data.series : [];
    if (!series.length) {
      refs.metricsPanel.hidden = true;
      refs.metricsChart.innerHTML = "";
      metricsKey = "";
      return;
    }
    const key = `${data.updatedAt || ""}|${data.pointCount || 0}|${series
      .map((item) => (item && item.name ? item.name : ""))
      .join("\u0000")}`;
    if (key === metricsKey) return;
    metricsKey = key;
    refs.metricsPanel.hidden = false;
    const shownPoints = series.reduce(
      (sum, item) => sum + (Array.isArray(item.points) ? item.points.length : 0),
      0
    );
    refs.metricsMeta.textContent = data.truncated
      ? t("runDetail.metricsTruncated", { points: shownPoints })
      : t("runDetail.metricsPoints", { points: data.pointCount || shownPoints });
    renderMetricsChart(refs.metricsChart, series, { label: t("runDetail.metrics") });
  }

  refs.logDownload.addEventListener("click", async () => {
    try {
      await download(`/api/runs/${runId}/logs/download`, `${run ? run.name : runId}.log`);
      toast(t("toast.downloadStarted"), "info");
    } catch (error) {
      toast(error.message, "error");
    }
  });

  refs.zip.addEventListener("click", async () => {
    try {
      await download(
        `/api/runs/${runId}/outputs/download`,
        `${run ? run.name : runId}-outputs.zip`
      );
      toast(t("toast.downloadStarted"), "info");
    } catch (error) {
      toast(error.message, "error");
    }
  });

  const shouldStop = () => run && TERMINAL.has(run.status) && logComplete;

  async function poll() {
    ticks += 1;
    try {
      if (ticks % 3 === 0) await loadRun();
      await pullLogs();
      if (ticks % 2 === 0) await pullMetrics().catch(() => {});
      failures = 0;
    } catch (error) {
      failures += 1;
      if (failures === 1) toast(error.message, "error");
      if (failures >= 6) {
        clearInterval(pollTimer);
        return;
      }
    }
    if (shouldStop()) {
      clearInterval(pollTimer);
      await loadRun().catch(() => {});
      await pullLogs().catch(() => {});
      await pullMetrics().catch(() => {});
    }
  }

  try {
    await loadRun();
    await pullLogs();
    await pullMetrics().catch(() => {});
  } catch (error) {
    root.innerHTML = `<div class="panel"><div class="empty">
      <div class="empty-title">${escapeHtml(
        t("errors.loadFailed", { message: error.message })
      )}</div>
      <a class="btn btn-secondary" href="#/runs">${escapeHtml(t("runDetail.back"))}</a>
    </div></div>`;
    return () => {
      disposed = true;
    };
  }
  updateJump();

  if (!shouldStop()) {
    pollTimer = setInterval(poll, 1500);
  }

  return () => {
    disposed = true;
    clearInterval(pollTimer);
  };
}
