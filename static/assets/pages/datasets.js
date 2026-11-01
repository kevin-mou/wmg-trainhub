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
        <th style="width:36%">${escapeHtml(t("datasets.col.name"))}</th>
        <th>${escapeHtml(t("datasets.col.owner"))}</th>
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
                <div class="cell-main">
                  <button type="button" class="dataset-name truncate" data-detail="${escapeHtml(
                    item.id
                  )}" title="${escapeHtml(t("datasets.detail.view"))}">${escapeHtml(
                    item.name
                  )}</button>
                  ${
                    item.shared
                      ? `<span class="badge badge-neutral">${escapeHtml(
                          t("datasets.sharedBadge")
                        )}</span>`
                      : ""
                  }
                </div>
                ${
                  item.description
                    ? `<div class="cell-sub">${escapeHtml(item.description)}</div>`
                    : ""
                }
              </td>
              <td class="muted nowrap">${escapeHtml(
                item.isOwner ? t("datasets.mine") : item.ownerName || "—"
              )}</td>
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
                  ${
                    item.isOwner && item.hasZip
                      ? `<button type="button" class="icon-btn" data-extract="${escapeHtml(
                          item.id
                        )}" data-name="${escapeHtml(item.name)}" title="${escapeHtml(
                          t("datasets.extract")
                        )}" aria-label="${escapeHtml(
                          t("datasets.extract")
                        )}">${icons.folder}</button>`
                      : ""
                  }
                  ${
                    item.isOwner
                      ? `<button type="button" class="icon-btn" data-share="${escapeHtml(
                          item.id
                        )}" data-shared="${item.shared ? "1" : "0"}" title="${escapeHtml(
                          item.shared ? t("datasets.unshare") : t("datasets.share")
                        )}" aria-label="${escapeHtml(
                          item.shared ? t("datasets.unshare") : t("datasets.share")
                        )}">${icons.share}</button>`
                      : ""
                  }
                  ${
                    item.isOwner
                      ? `<button type="button" class="icon-btn" data-delete="${escapeHtml(
                          item.id
                        )}" data-name="${escapeHtml(item.name)}" title="${escapeHtml(
                          t("common.delete")
                        )}" aria-label="${escapeHtml(t("common.delete"))}">${icons.trash}</button>`
                      : ""
                  }
                </div>
              </td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table></div>`;

    tableHost.querySelectorAll("[data-detail]").forEach((button) => {
      button.addEventListener("click", () => {
        const record = records.find((item) => item.id === button.dataset.detail);
        if (record) openDatasetDetail(record);
      });
    });

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

    tableHost.querySelectorAll("[data-share]").forEach((button) => {
      button.addEventListener("click", async () => {
        const nextShared = button.dataset.shared !== "1";
        button.disabled = true;
        try {
          await http.patch(`/api/datasets/${button.dataset.share}/sharing`, {
            shared: nextShared,
          });
          toast(
            nextShared ? t("datasets.toast.shared") : t("datasets.toast.unshared"),
            "success"
          );
          load(page);
        } catch (error) {
          toast(error.message, "error");
          button.disabled = false;
        }
      });
    });

    tableHost.querySelectorAll("[data-extract]").forEach((button) => {
      button.addEventListener("click", async () => {
        const okToExtract = await confirmDialog({
          title: t("datasets.extractTitle"),
          message: t("datasets.extractText", { name: button.dataset.name }),
          confirmText: t("datasets.extract"),
        });
        if (!okToExtract) return;
        button.disabled = true;
        try {
          await http.post(`/api/datasets/${button.dataset.extract}/extract`);
          toast(t("datasets.toast.extracted"), "success");
          load(page);
        } catch (error) {
          toast(error.message, "error");
          button.disabled = false;
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

  function renderDatasetDetail(detail) {
    const files = Array.isArray(detail.files) ? detail.files : [];
    const limit = 500;
    const shown = files.slice(0, limit);
    const rows = shown
      .map(
        (item) => `<tr>
          <td class="mono">${escapeHtml(item.filename)}</td>
          <td class="mono num">${formatBytes(item.sizeBytes || 0)}</td>
        </tr>`
      )
      .join("");
    const truncated = files.length > shown.length;
    const owner = detail.isOwner
      ? t("datasets.mine")
      : detail.ownerName || "—";
    return `
      <div class="dataset-detail-meta">
        <span>${escapeHtml(t("datasets.col.owner"))}: <strong>${escapeHtml(owner)}</strong></span>
        <span>${escapeHtml(t("datasets.col.files"))}: <strong>${detail.fileCount ?? files.length}</strong></span>
        <span>${escapeHtml(t("datasets.col.size"))}: <strong>${formatBytes(detail.sizeBytes || 0)}</strong></span>
        <span>${escapeHtml(t("datasets.col.created"))}: <strong>${formatTime(detail.createdAt)}</strong></span>
        ${
          detail.shared
            ? `<span class="badge badge-neutral">${escapeHtml(
                t("datasets.sharedBadge")
              )}</span>`
            : ""
        }
      </div>
      ${
        files.length
          ? `<div class="table-scroll dataset-file-list">
              <table class="table dataset-file-table">
                <thead><tr>
                  <th>${escapeHtml(t("datasets.detail.files"))}</th>
                  <th class="num">${escapeHtml(t("datasets.col.size"))}</th>
                </tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
            ${
              truncated
                ? `<p class="hint">${escapeHtml(
                    t("datasets.detail.truncated", {
                      n: shown.length,
                      total: files.length,
                    })
                  )}</p>`
                : ""
            }`
          : `<div class="meta-empty">${escapeHtml(t("datasets.detail.empty"))}</div>`
      }`;
  }

  function openDatasetDetail(record) {
    openDialog({
      title: t("datasets.detail.title"),
      subtitle: record.name,
      bodyHtml: `<div id="ds-detail"><div class="meta-empty">${escapeHtml(
        t("common.loading")
      )}</div></div>`,
      footerHtml: `<button type="button" class="btn btn-secondary" data-act="close">${escapeHtml(
        t("common.close")
      )}</button>`,
      onMount: ({ dialog, close, showError }) => {
        dialog.querySelector('[data-act="close"]').addEventListener("click", close);
        const body = dialog.querySelector("#ds-detail");
        void (async () => {
          try {
            const detail = await http.get(`/api/datasets/${record.id}`);
            body.innerHTML = renderDatasetDetail(detail);
          } catch (error) {
            showError(error.message);
            body.innerHTML = "";
          }
        })();
      },
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
          <label class="check-row" for="ds-shared">
            <input type="checkbox" id="ds-shared" />
            <span>${escapeHtml(t("datasets.modal.shared"))}</span>
          </label>
          <span class="hint">${escapeHtml(t("datasets.modal.sharedHint"))}</span>
        </div>
        <div class="field">
          <label>${escapeHtml(t("datasets.col.files"))}</label>
          <div class="dropzone" id="ds-drop" role="button" tabindex="0">
            ${icons.upload}
            <div class="dz-title">${escapeHtml(t("datasets.modal.drop"))}</div>
            <div class="dz-hint">${escapeHtml(t("datasets.modal.dropHint"))}</div>
            <input type="file" id="ds-files" multiple hidden />
          </div>
          <div class="dz-actions">
            <button type="button" class="btn btn-secondary btn-sm" id="ds-pick-files">${escapeHtml(
              t("datasets.modal.pickFiles")
            )}</button>
            <button type="button" class="btn btn-secondary btn-sm" id="ds-pick-folder">${escapeHtml(
              t("datasets.modal.pickFolder")
            )}</button>
            <span class="hint">${escapeHtml(t("datasets.modal.folderHint"))}</span>
          </div>
          <input type="file" id="ds-folder" webkitdirectory directory multiple hidden />
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
        const folderInput = dialog.querySelector("#ds-folder");
        const list = dialog.querySelector("#ds-file-list");
        const progress = dialog.querySelector("#ds-progress");
        const progressBar = progress.querySelector("span");
        const progressLabel = dialog.querySelector("#ds-progress-label");
        const submit = dialog.querySelector('[data-submit="true"]');

        const renderFiles = () => {
          list.innerHTML = selected.length
            ? selected
                .map(
                  (entry) => `<div class="file-row">${icons.file}
                    <span class="fname">${escapeHtml(entry.path)}</span>
                    <span class="fsize">${formatBytes(entry.file.size)}</span>
                  </div>`
                )
                .join("")
            : "";
        };

        const addFiles = (files) => {
          for (const file of files) {
            selected.push({ file, path: file.webkitRelativePath || file.name });
          }
          renderFiles();
        };

        const walkEntry = async (entry, prefix, collected) => {
          if (entry.isFile) {
            const file = await new Promise((resolve, reject) =>
              entry.file(resolve, reject)
            );
            collected.push({ file, path: `${prefix}${entry.name}` });
            return;
          }
          if (!entry.isDirectory) return;
          const reader = entry.createReader();
          while (true) {
            const batch = await new Promise((resolve, reject) =>
              reader.readEntries(resolve, reject)
            );
            if (!batch.length) break;
            for (const child of batch) {
              await walkEntry(child, `${prefix}${entry.name}/`, collected);
            }
          }
        };

        const addDropped = async (dataTransfer) => {
          const entries = (dataTransfer?.items || [])
            .map((item) => item.webkitGetAsEntry?.())
            .filter(Boolean);
          const collected = [];
          for (const entry of entries) {
            await walkEntry(entry, "", collected);
          }
          if (collected.length) {
            selected.push(...collected);
            renderFiles();
            return;
          }
          addFiles(dataTransfer?.files || []);
        };

        drop.addEventListener("click", () => input.click());
        drop.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            input.click();
          }
        });
        dialog
          .querySelector("#ds-pick-files")
          .addEventListener("click", () => input.click());
        dialog
          .querySelector("#ds-pick-folder")
          .addEventListener("click", () => folderInput.click());
        input.addEventListener("change", () => {
          addFiles(input.files || []);
          input.value = "";
        });
        folderInput.addEventListener("change", () => {
          addFiles(folderInput.files || []);
          folderInput.value = "";
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
          void addDropped(event.dataTransfer).catch((error) => {
            showError(error.message);
          });
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
          if (dialog.querySelector("#ds-shared").checked) {
            formData.append("shared", "true");
          }
          for (const entry of selected) {
            formData.append("files", entry.file, entry.path);
          }

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
