import { http } from "../api.js";
import { t } from "../i18n.js";
import { escapeHtml, openDialog, toast } from "../ui.js";

function option(value, label, selected = false) {
  return `<option value="${escapeHtml(value)}" ${selected ? "selected" : ""}>${escapeHtml(
    label
  )}</option>`;
}

function datasetLabel(dataset) {
  if (!dataset) return "";
  if (!dataset.isOwner) {
    return `${dataset.name} · ${t("runs.sharedFrom", {
      name: dataset.ownerName || "?",
    })}`;
  }
  return dataset.shared
    ? `${dataset.name} · ${t("datasets.sharedBadge")}`
    : dataset.name;
}

export async function openRunModal({ navigate, preselectedNotebookId = null }) {
  let notebooks = [];
  let datasets = [];
  let kernels = [];
  let defaultKernel = null;
  try {
    const [notebookPage, datasetPage, kernelInfo] = await Promise.all([
      http.get("/api/notebooks?page=1&size=100"),
      http.get("/api/datasets?page=1&size=100"),
      http.get("/api/kernels"),
    ]);
    notebooks = notebookPage.records || [];
    datasets = datasetPage.records || [];
    kernels = kernelInfo.kernels || [];
    defaultKernel = kernelInfo.default || (kernels[0] && kernels[0].name) || "";
  } catch (error) {
    toast(error.message, "error");
    return;
  }

  if (!notebooks.length) {
    openDialog({
      title: t("runModal.noNotebookTitle"),
      bodyHtml: `<p class="meta-empty" style="font-size:13px;color:var(--ink-2)">${escapeHtml(
        t("runModal.noNotebookText")
      )}</p>`,
      footerHtml: `
        <button type="button" class="btn btn-secondary" data-act="cancel">${escapeHtml(t("common.cancel"))}</button>
        <button type="button" class="btn btn-primary" data-act="go">${escapeHtml(t("runModal.goUpload"))}</button>`,
      onMount: ({ dialog, close }) => {
        dialog.querySelector('[data-act="cancel"]').addEventListener("click", close);
        dialog.querySelector('[data-act="go"]').addEventListener("click", () => {
          close();
          navigate("/notebooks");
        });
      },
    });
    return;
  }

  const selectedNotebook = preselectedNotebookId || notebooks[0].id;
  const kernelOptions = kernels.length
    ? kernels
        .map((kernel) =>
          option(
            kernel.name,
            `${kernel.displayName} · ${kernel.name}`,
            kernel.name === defaultKernel
          )
        )
        .join("")
    : option(defaultKernel || "python3", defaultKernel || "python3", true);

  openDialog({
    title: t("runModal.title"),
    subtitle: t("runModal.subtitle"),
    bodyHtml: `
      <div class="field">
        <label for="run-notebook">${escapeHtml(t("runModal.notebook"))}</label>
        <select class="select" id="run-notebook">
          ${notebooks
            .map((notebook) =>
              option(notebook.id, notebook.name, notebook.id === selectedNotebook)
            )
            .join("")}
        </select>
      </div>
      <div class="field">
        <label for="run-dataset">${escapeHtml(t("runModal.dataset"))}</label>
        <select class="select" id="run-dataset">
          ${option("", t("runModal.datasetNone"), true)}
          ${datasets
            .map((dataset) => option(dataset.id, datasetLabel(dataset)))
            .join("")}
        </select>
      </div>
      <div class="field">
        <label for="run-name">${escapeHtml(t("runModal.name"))}</label>
        <input class="input" id="run-name" maxlength="120"
          placeholder="${escapeHtml(t("runModal.namePlaceholder"))}" />
      </div>
      <div class="field-row">
        <div class="field">
          <label for="run-kernel">${escapeHtml(t("runModal.kernel"))}</label>
          <select class="select" id="run-kernel">${kernelOptions}</select>
        </div>
        <div class="field">
          <label for="run-timeout">${escapeHtml(t("runModal.timeout"))}</label>
          <input class="input" id="run-timeout" type="number" min="0" step="1" inputmode="numeric" />
          <span class="hint">${escapeHtml(t("runModal.timeoutHint"))}</span>
        </div>
      </div>
      <div class="field">
        <label for="run-params">${escapeHtml(t("runModal.parameters"))}</label>
        <textarea class="textarea" id="run-params" spellcheck="false"
          placeholder='{"epochs": 3, "learningRate": 0.001}'></textarea>
        <span class="hint">${escapeHtml(t("runModal.parametersHint"))}</span>
        <span class="hint" id="run-params-prefill"></span>
      </div>`,
    footerHtml: `
      <button type="button" class="btn btn-secondary" data-act="cancel">${escapeHtml(
        t("common.cancel")
      )}</button>
      <button type="button" class="btn btn-primary" data-submit="true">${escapeHtml(
        t("runModal.submit")
      )}</button>`,
    onMount: ({ dialog, close, showError }) => {
      const submit = dialog.querySelector('[data-submit="true"]');
      dialog.querySelector('[data-act="cancel"]').addEventListener("click", close);

      const notebookSelect = dialog.querySelector("#run-notebook");
      const paramsInput = dialog.querySelector("#run-params");
      const paramsHint = dialog.querySelector("#run-params-prefill");
      const drafts = new Map();
      let activeNotebook = notebookSelect.value;
      let prefillSeq = 0;

      const prefillFromLastRun = async (notebookId) => {
        const seq = ++prefillSeq;
        let last = null;
        try {
          last = await http.get(`/api/notebooks/${encodeURIComponent(notebookId)}/last-run`);
        } catch {
          last = null;
        }
        if (seq !== prefillSeq || activeNotebook !== notebookId) return;
        if (paramsInput.value.trim()) return;
        const parameters = last && last.parameters;
        if (
          !parameters ||
          typeof parameters !== "object" ||
          Array.isArray(parameters) ||
          !Object.keys(parameters).length
        ) {
          paramsHint.textContent = "";
          return;
        }
        paramsInput.value = JSON.stringify(parameters, null, 2);
        paramsHint.textContent = t("runModal.parametersFromLastRun", {
          name: last.name || "—",
        });
      };

      notebookSelect.addEventListener("change", () => {
        drafts.set(activeNotebook, paramsInput.value);
        activeNotebook = notebookSelect.value;
        paramsInput.value = drafts.get(activeNotebook) || "";
        paramsHint.textContent = "";
        prefillSeq += 1;
        if (!paramsInput.value.trim()) prefillFromLastRun(activeNotebook);
      });
      paramsInput.addEventListener("input", () => {
        drafts.set(activeNotebook, paramsInput.value);
        paramsHint.textContent = "";
      });
      prefillFromLastRun(activeNotebook);

      const submitRun = async () => {
        showError("");
        const parametersText = dialog.querySelector("#run-params").value.trim();
        let parameters = null;
        if (parametersText) {
          try {
            const parsed = JSON.parse(parametersText);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
              throw new Error("not an object");
            }
            parameters = parsed;
          } catch {
            showError(t("runModal.parametersInvalid"));
            return;
          }
        }
        const timeoutText = dialog.querySelector("#run-timeout").value.trim();
        const timeoutSeconds = timeoutText === "" ? null : Number(timeoutText);
        if (timeoutSeconds != null && (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 0)) {
          showError(t("runModal.timeoutHint"));
          return;
        }

        submit.disabled = true;
        submit.textContent = t("common.saving");
        try {
          const run = await http.post("/api/runs", {
            notebookId: dialog.querySelector("#run-notebook").value,
            datasetId: dialog.querySelector("#run-dataset").value || null,
            name: dialog.querySelector("#run-name").value.trim() || null,
            kernel: dialog.querySelector("#run-kernel").value || null,
            parameters,
            timeoutSeconds,
          });
          close();
          toast(t("toast.runCreated"), "success");
          navigate(`/runs/${run.id}`);
        } catch (error) {
          showError(error.message);
          submit.disabled = false;
          submit.textContent = t("runModal.submit");
        }
      };

      submit.__onSubmit = submitRun;
      submit.addEventListener("click", submitRun);
      setTimeout(() => dialog.querySelector("#run-notebook").focus(), 30);
    },
  });
}
