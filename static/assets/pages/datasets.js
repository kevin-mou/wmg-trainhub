import { download, http, upload } from "../api.js";
import { t } from "../i18n.js";
import {
  confirmDialog,
  debounce,
  escapeHtml,
  formatBytes,
  formatTime,
  icons,
  openDialog,
  pagerHtml,
  toast,
} from "../ui.js";

export async function renderDatasets(root, ctx) {
  let disposed = false;
  let page = 1;
  const size = 20;
  let query = "";
  let pageInfo = { current: 1, pages: 0, total: 0 };

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">${escapeHtml(t("datasets.title"))}</h1>
        <p class="page-sub">${escapeHtml(t("datasets.subtitle"))}</p>
      </div>
      <div class="page-head-actions">
        <button type="button" class="btn btn-primary" id="ds-upload">${icons.upload}${escapeHtml(
          t("datasets.upload")
        )}</button>
      </div>
    </div>
    <section class="panel">
      <div class="panel-head">
        <span class="panel-title">${escapeHtml(t("common.all"))}</span>
        <span class="panel-sub" id="ds-count"></span>
        <div class="search" style="margin-left:auto">
          ${icons.search}
          <input id="ds-search" type="search" placeholder="${escapeHtml(
            t("datasets.searchPlaceholder")
          )}" />
        </div>
      </div>
      <div id="ds-table"><div class="loading">${escapeHtml(t("common.loading"))}</div></div>
      <div id="ds-pager"></div>
    </section>`;

  const tableHost = root.querySelector("#ds-table");
  const pagerHost = root.querySelector("#ds-pager");
  const countHost = root.querySelector("#ds-count");
  const searchInput = root.querySelector("#ds-search");

  root.querySelector("#ds-upload").addEventListener("click", () => openUpload());
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

  async function load(targetPage = page) {
    page = targetPage;
    try {
      const data = await http.get(
        `/api/datasets?page=${page}&size=${size}&q=${encodeURIComponent(query)}`
      );
      if (disposed || !root.isConnected) return;
      pageInfo = data;
      renderTable(data.records);
      pagerHost.innerHTML = pagerHtml(data);
      pagerHost.querySelectorAll("[data-page]").forEach((button) => {
        button.addEventListener("click", () => load(Number(button.dataset.page)));
      });
      countHost.textContent = data.total ? `${data.total}` : "";
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
        <div class="empty-icon">${icons.datasets}</div>
        <div class="empty-title">${escapeHtml(
          query ? t("common.noResults") : t("datasets.empty.title")
        )}</div>
        <div class="empty-text">${escapeHtml(
          query ? t("common.noResultsHint") : t("datasets.empty.text")
        )}</div>
        ${
          query
            ? ""
            : `<button class="btn btn-primary" id="ds-empty-upload">${escapeHtml(
                t("datasets.empty.action")
              )}</button>`
        }
      </div>`;
      tableHost.querySelector("#ds-empty-upload")?.addEventListener("click", () => openUpload());
      return;
    }

    tableHost.innerHTML = `<div class="table-scroll"><table class="table">
      <thead><tr>
        <th style="width:42%">${escapeHtml(t("datasets.col.name"))}</th>
        <th>${escapeHtml(t("datasets.col.files"))}</th>
        <th>${escapeHtml(t("datasets.col.size"))}</th>
        <th>${escapeHtml(t("datasets.col.created"))}</th>
        <th style="text-align:right">${escapeHtml(t("common.actions"))}</th>
      </tr></thead>
      <tbody>
        ${records
          .map(
            (item) => `<tr>
              <td>
                <div class="cell-main"><span class="truncate">${escapeHtml(item.name)}</span></div>
                ${
                  item.description
                    ? `<div class="cell-sub">${escapeHtml(item.description)}</div>`
                    : ""
                }
              </td>
              <td class="mono">${escapeHtml(
                t("datasets.filesCount", { n: item.fileCount })
              )}</td>
              <td class="mono">${formatBytes(item.sizeBytes)}</td>
              <td class="muted nowrap">${formatTime(item.createdAt)}</td>
              <td>
                <div class="cell-actions">
                  <button type="button" class="icon-btn" data-download="${escapeHtml(
                    item.id
                  )}" title="${escapeHtml(t("common.download"))}" aria-label="${escapeHtml(
                    t("common.download")
                  )}">${icons.download}</button>
                  <button type="button" class="icon-btn" data-delete="${escapeHtml(
                    item.id
                  )}" data-name="${escapeHtml(item.name)}" title="${escapeHtml(
                    t("common.delete")
                  )}" aria-label="${escapeHtml(t("common.delete"))}">${icons.trash}</button>
                </div>
              </td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table></div>`;

    tableHost.querySelectorAll("[data-download]").forEach((button) => {
      button.addEventListener("click", async () => {
        const record = records.find((item) => item.id === button.dataset.download);
        try {
          await download(
            `/api/datasets/${button.dataset.download}/download`,
            `${record ? record.name : "dataset"}.zip`
          );
          toast(t("toast.downloadStarted"), "info");
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });

    tableHost.querySelectorAll("[data-delete]").forEach((button) => {
      button.addEventListener("click", async () => {
        const okToDelete = await confirmDialog({
          title: t("datasets.delete.title"),
          message: t("datasets.delete.text", { name: button.dataset.name }),
          confirmText: t("common.delete"),
          danger: true,
        });
        if (!okToDelete) return;
        try {
          await http.del(`/api/datasets/${button.dataset.delete}`);
          toast(t("toast.deleted"), "success");
          if (records.length === 1 && page > 1) page -= 1;
          load(page);
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });
  }

  function openUpload() {
    const selected = [];
    openDialog({
      title: t("datasets.modal.title"),
      subtitle: t("datasets.modal.subtitle"),
      bodyHtml: `
        <div class="field">
          <label for="ds-name">${escapeHtml(t("datasets.modal.name"))}</label>
          <input class="input" id="ds-name" maxlength="120"
            placeholder="${escapeHtml(t("datasets.modal.namePlaceholder"))}" />
        </div>
        <div class="field">
          <label for="ds-description">${escapeHtml(t("datasets.modal.description"))}</label>
          <textarea class="textarea" id="ds-description" maxlength="500"
            placeholder="${escapeHtml(t("datasets.modal.descriptionPlaceholder"))}"></textarea>
        </div>
        <div class="field">
          <label>${escapeHtml(t("datasets.col.files"))}</label>
          <div class="dropzone" id="ds-drop" role="button" tabindex="0">
            ${icons.upload}
            <div class="dz-title">${escapeHtml(t("datasets.modal.drop"))}</div>
            <div class="dz-hint">${escapeHtml(t("datasets.modal.dropHint"))}</div>
            <input type="file" id="ds-files" multiple hidden />
          </div>
          <div class="file-list" id="ds-file-list"></div>
          <div class="progress" id="ds-progress" hidden><span></span></div>
          <div class="progress-label" id="ds-progress-label" hidden></div>
        </div>`,
      footerHtml: `
        <button type="button" class="btn btn-secondary" data-act="cancel">${escapeHtml(
          t("common.cancel")
        )}</button>
        <button type="button" class="btn btn-primary" data-submit="true">${escapeHtml(
          t("datasets.modal.submit")
        )}</button>`,
      onMount: ({ dialog, close, showError }) => {
        const drop = dialog.querySelector("#ds-drop");
        const input = dialog.querySelector("#ds-files");
        const list = dialog.querySelector("#ds-file-list");
        const progress = dialog.querySelector("#ds-progress");
        const progressBar = progress.querySelector("span");
        const progressLabel = dialog.querySelector("#ds-progress-label");
        const submit = dialog.querySelector('[data-submit="true"]');

        const renderFiles = () => {
          list.innerHTML = selected.length
            ? selected
                .map(
                  (file) => `<div class="file-row">${icons.file}
                    <span class="fname">${escapeHtml(file.name)}</span>
                    <span class="fsize">${formatBytes(file.size)}</span>
                  </div>`
                )
                .join("")
            : "";
        };

        const addFiles = (files) => {
          for (const file of files) selected.push(file);
          renderFiles();
        };

        drop.addEventListener("click", () => input.click());
        drop.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            input.click();
          }
        });
        input.addEventListener("change", () => {
          addFiles(input.files || []);
          input.value = "";
        });
        ["dragenter", "dragover"].forEach((name) =>
          drop.addEventListener(name, (event) => {
            event.preventDefault();
            drop.classList.add("dragover");
          })
        );
        ["dragleave", "drop"].forEach((name) =>
          drop.addEventListener(name, (event) => {
            event.preventDefault();
            drop.classList.remove("dragover");
          })
        );
        drop.addEventListener("drop", (event) => {
          addFiles(event.dataTransfer?.files || []);
        });

        dialog.querySelector('[data-act="cancel"]').addEventListener("click", close);

        const submitUpload = async () => {
          showError("");
          if (!selected.length) {
            showError(t("datasets.invalidEmpty"));
            return;
          }
          const formData = new FormData();
          const name = dialog.querySelector("#ds-name").value.trim();
          const description = dialog.querySelector("#ds-description").value.trim();
          if (name) formData.append("name", name);
          if (description) formData.append("description", description);
          for (const file of selected) formData.append("files", file, file.name);

          submit.disabled = true;
          submit.textContent = t("common.saving");
          progress.hidden = false;
          progressLabel.hidden = false;
          try {
            await upload("/api/datasets", formData, {
              onProgress: (percent) => {
                progressBar.style.width = `${percent}%`;
                progressLabel.textContent = t("datasets.modal.uploading", {
                  percent,
                });
              },
            });
            close();
            toast(t("toast.uploaded"), "success");
            load(1);
          } catch (error) {
            showError(error.message);
            submit.disabled = false;
            submit.textContent = t("datasets.modal.submit");
            progress.hidden = true;
            progressLabel.hidden = true;
          }
        };

        submit.addEventListener("click", submitUpload);
        submit.__onSubmit = submitUpload;
      },
    });
  }

  await load(1);
  return () => {
    disposed = true;
  };
}
