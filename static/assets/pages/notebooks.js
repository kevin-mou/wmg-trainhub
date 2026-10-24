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
import { openRunModal } from "./run-modal.js";

export async function renderNotebooks(root, ctx) {
  let disposed = false;
  let page = 1;
  const size = 20;
  let query = "";
  let pageInfo = { current: 1, pages: 0, total: 0 };

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">${escapeHtml(t("notebooks.title"))}</h1>
        <p class="page-sub">${escapeHtml(t("notebooks.subtitle"))}</p>
      </div>
      <div class="page-head-actions">
        <button type="button" class="btn btn-primary" id="nb-upload">${icons.upload}${escapeHtml(
          t("notebooks.upload")
        )}</button>
      </div>
    </div>
    <section class="panel">
      <div class="panel-head">
        <span class="panel-title">${escapeHtml(t("common.all"))}</span>
        <span class="panel-sub" id="nb-count"></span>
        <div class="search" style="margin-left:auto">
          ${icons.search}
          <input id="nb-search" type="search" placeholder="${escapeHtml(
            t("notebooks.searchPlaceholder")
          )}" />
        </div>
      </div>
      <div id="nb-table"><div class="loading">${escapeHtml(t("common.loading"))}</div></div>
      <div id="nb-pager"></div>
    </section>`;

  const tableHost = root.querySelector("#nb-table");
  const pagerHost = root.querySelector("#nb-pager");
  const countHost = root.querySelector("#nb-count");
  const searchInput = root.querySelector("#nb-search");

  root.querySelector("#nb-upload").addEventListener("click", () => openUpload());
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
        `/api/notebooks?page=${page}&size=${size}&q=${encodeURIComponent(query)}`
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
        <div class="empty-icon">${icons.notebooks}</div>
        <div class="empty-title">${escapeHtml(
          query ? t("common.noResults") : t("notebooks.empty.title")
        )}</div>
        <div class="empty-text">${escapeHtml(
          query ? t("common.noResultsHint") : t("notebooks.empty.text")
        )}</div>
        ${
          query
            ? ""
            : `<button class="btn btn-primary" id="nb-empty-upload">${escapeHtml(
                t("notebooks.empty.action")
              )}</button>`
        }
      </div>`;
      tableHost.querySelector("#nb-empty-upload")?.addEventListener("click", () => openUpload());
      return;
    }

    tableHost.innerHTML = `<div class="table-scroll"><table class="table">
      <thead><tr>
        <th style="width:38%">${escapeHtml(t("notebooks.col.name"))}</th>
        <th>${escapeHtml(t("notebooks.col.cells"))}</th>
        <th>${escapeHtml(t("notebooks.col.size"))}</th>
        <th>${escapeHtml(t("notebooks.col.created"))}</th>
        <th style="text-align:right">${escapeHtml(t("common.actions"))}</th>
      </tr></thead>
      <tbody>
        ${records
          .map(
            (item) => `<tr>
              <td>
                <div class="cell-main"><span class="truncate">${escapeHtml(item.name)}</span></div>
                <div class="cell-sub mono">${escapeHtml(item.filename)}</div>
              </td>
              <td class="mono">${item.cellCount}</td>
              <td class="mono">${formatBytes(item.sizeBytes)}</td>
              <td class="muted nowrap">${formatTime(item.createdAt)}</td>
              <td>
                <div class="cell-actions">
                  <button type="button" class="btn btn-secondary btn-sm" data-run="${escapeHtml(
                    item.id
                  )}">${escapeHtml(t("notebooks.run"))}</button>
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

    tableHost.querySelectorAll("[data-run]").forEach((button) => {
      button.addEventListener("click", () =>
        openRunModal({ navigate: ctx.navigate, preselectedNotebookId: button.dataset.run })
      );
    });

    tableHost.querySelectorAll("[data-download]").forEach((button) => {
      button.addEventListener("click", async () => {
        const record = records.find((item) => item.id === button.dataset.download);
        try {
          await download(
            `/api/notebooks/${button.dataset.download}/download`,
            record ? record.filename : "notebook.ipynb"
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
          title: t("notebooks.delete.title"),
          message: t("notebooks.delete.text", { name: button.dataset.name }),
          confirmText: t("common.delete"),
          danger: true,
        });
        if (!okToDelete) return;
        try {
          await http.del(`/api/notebooks/${button.dataset.delete}`);
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
    let selected = null;
    openDialog({
      title: t("notebooks.modal.title"),
      subtitle: t("notebooks.modal.subtitle"),
      bodyHtml: `
        <div class="field">
          <label for="nb-name">${escapeHtml(t("notebooks.modal.name"))}</label>
          <input class="input" id="nb-name" maxlength="120"
            placeholder="${escapeHtml(t("notebooks.modal.namePlaceholder"))}" />
        </div>
        <div class="field">
          <label>${escapeHtml(t("notebooks.modal.file"))}</label>
          <div class="dropzone" id="nb-drop" role="button" tabindex="0">
            ${icons.notebooks}
            <div class="dz-title">${escapeHtml(t("notebooks.modal.drop"))}</div>
            <div class="dz-hint">${escapeHtml(t("notebooks.modal.dropHint"))}</div>
            <input type="file" id="nb-file" accept=".ipynb,application/json" hidden />
          </div>
          <div class="file-list" id="nb-file-list"></div>
        </div>`,
      footerHtml: `
        <button type="button" class="btn btn-secondary" data-act="cancel">${escapeHtml(
          t("common.cancel")
        )}</button>
        <button type="button" class="btn btn-primary" data-submit="true">${escapeHtml(
          t("notebooks.modal.submit")
        )}</button>`,
      onMount: ({ dialog, close, showError }) => {
        const drop = dialog.querySelector("#nb-drop");
        const input = dialog.querySelector("#nb-file");
        const list = dialog.querySelector("#nb-file-list");
        const submit = dialog.querySelector('[data-submit="true"]');

        const renderFile = () => {
          list.innerHTML = selected
            ? `<div class="file-row">${icons.file}
                <span class="fname">${escapeHtml(selected.name)}</span>
                <span class="fsize">${formatBytes(selected.size)}</span>
              </div>`
            : "";
        };

        const choose = () => input.click();
        drop.addEventListener("click", choose);
        drop.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            choose();
          }
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
          const file = event.dataTransfer?.files?.[0];
          if (file) {
            selected = file;
            renderFile();
          }
        });
        input.addEventListener("change", () => {
          selected = (input.files || [])[0] || null;
          input.value = "";
          renderFile();
        });

        dialog.querySelector('[data-act="cancel"]').addEventListener("click", close);

        const submitUpload = async () => {
          showError("");
          if (!selected) {
            showError(t("notebooks.invalidFile"));
            return;
          }
          if (!selected.name.toLowerCase().endsWith(".ipynb")) {
            showError(t("notebooks.invalidFile"));
            return;
          }
          const formData = new FormData();
          const name = dialog.querySelector("#nb-name").value.trim();
          if (name) formData.append("name", name);
          formData.append("file", selected, selected.name);

          submit.disabled = true;
          submit.textContent = t("common.saving");
          try {
            await upload("/api/notebooks", formData);
            close();
            toast(t("toast.uploaded"), "success");
            load(1);
          } catch (error) {
            showError(error.message);
            submit.disabled = false;
            submit.textContent = t("notebooks.modal.submit");
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
