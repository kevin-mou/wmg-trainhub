import { getToken, http } from "../api.js";

import { t } from "../i18n.js";
import { confirmDialog, copyText, escapeHtml, icons, toast } from "../ui.js";
import { renderMarkdown } from "../vendor/trainhub-markdown.js";

const CELL_TYPES = ["code", "markdown", "raw"];
const TYPE_OPTIONS = ["auto", ...CELL_TYPES];
const MARKDOWN_START = /^(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|~~~|\|.*\|)/;
const CODE_START = /^(import\s|from\s+\S+\s+import\s|def\s|class\s|async\s+def\s|for\s|while\s|if\s|elif\s|else\s*:|try\s*:|except\b|with\s|return\b|print\s*\(|!|%)/;

function sourceOf(cell) {
  const source = cell && cell.source;
  if (Array.isArray(source)) return source.join("");
  return String(source == null ? "" : source);
}

function detectCellType(source) {
  const text = String(source || "").trim();
  if (!text) return "code";
  if (/^(<!doctype|<html|<div|<table|<style|<script|\\begin\{|\\documentclass)/i.test(text)) {
    return "raw";
  }
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const markdownScore = lines.filter((line) => MARKDOWN_START.test(line)).length;
  const codeScore = lines.filter(
    (line) => CODE_START.test(line) || /^\s{2,}\S/.test(line) || /[={}\[\]]/.test(line)
  ).length;
  if (markdownScore > codeScore) return "markdown";
  if (codeScore > 0) return "code";
  if (/[\u4e00-\u9fff]/.test(text) || /\s/.test(text)) return "markdown";
  return "code";
}

function effectiveCellType(cell) {
  const type = cell && cell.cell_type;
  if (type === "auto") return detectCellType(sourceOf(cell));
  return CELL_TYPES.includes(type) ? type : "code";
}

function newCellId() {
  const random = globalThis.crypto?.randomUUID?.();
  if (random) return random.replace(/-/g, "").slice(0, 32);
  return `cell_${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
}

function newCell(type = "auto") {
  const cell = { id: newCellId(), cell_type: type, metadata: {}, source: "" };
  if (type === "code" || type === "auto") {
    cell.execution_count = null;
    cell.outputs = [];
  }
  return cell;
}

function defaultContent() {
  return {
    cells: [newCell("auto")],
    metadata: {
      kernelspec: {
        name: "python3",
        display_name: "Python 3",
        language: "python",
      },
      language_info: { name: "python" },
    },
    nbformat: 4,
    nbformat_minor: 5,
  };
}

function normalizeCell(cell) {
  const safeCell = cell && typeof cell === "object" ? cell : {};
  const type = effectiveCellType(safeCell);
  const result = JSON.parse(JSON.stringify(safeCell));
  result.id = safeCell.id ? String(safeCell.id) : newCellId();
  result.cell_type = type;
  result.metadata =
    safeCell.metadata && typeof safeCell.metadata === "object" ? safeCell.metadata : {};
  result.source = sourceOf(safeCell);
  if (type === "code") {
    result.execution_count = null;
    result.outputs = [];
  } else {
    delete result.outputs;
    delete result.execution_count;
  }
  return result;
}

function buildContent(documentNode, cells) {
  const content = JSON.parse(JSON.stringify(documentNode || {}));
  content.cells = cells.map(normalizeCell);
  content.metadata =
    content.metadata && typeof content.metadata === "object" ? content.metadata : {};
  content.nbformat = 4;
  content.nbformat_minor =
    Number.isInteger(content.nbformat_minor) && content.nbformat_minor >= 0
      ? content.nbformat_minor
      : 5;
  return content;
}

const MAX_OUTPUT_CHARS = 20000;
const MAX_OUTPUT_ITEMS = 50;
const MAX_IMAGE_BASE64 = 8 * 1024 * 1024;
const IMAGE_MIMES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
let codeMirrorModulePromise = null;

function valueText(value) {
  if (Array.isArray(value)) return value.map((part) => String(part == null ? "" : part)).join("");
  if (value == null) return "";
  return String(value);
}

function stripAnsi(text) {
  return String(text).replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
}

function textBlock(value, className = "") {
  const cleaned = stripAnsi(valueText(value));
  const clipped = cleaned.slice(0, MAX_OUTPUT_CHARS);
  return `<pre class="nb-output-text ${className}">${escapeHtml(clipped)}${
    cleaned.length > MAX_OUTPUT_CHARS
      ? `\n${escapeHtml(t("notebooks.editor.outputTruncated"))}`
      : ""
  }</pre>`;
}

function imageBlock(data) {
  for (const mime of IMAGE_MIMES) {
    const raw = valueText(data[mime]).replace(/\s+/g, "");
    if (!raw) continue;
    if (raw.length > MAX_IMAGE_BASE64) {
      return `<div class="nb-output-note">${escapeHtml(
        t("notebooks.editor.outputImageTooLarge")
      )}</div>`;
    }
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(raw)) continue;
    return `<img class="nb-output-image" alt="${escapeHtml(
      t("notebooks.editor.outputImageAlt")
    )}" src="data:${mime};base64,${raw}" />`;
  }
  return "";
}

function renderOneOutput(output) {
  if (!output || typeof output !== "object") return "";
  const type = output.output_type;
  if (type === "stream") {
    return textBlock(output.text, output.name === "stderr" ? "nb-output-stderr" : "");
  }
  if (type === "error") {
    const title = [output.ename, output.evalue].filter(Boolean).join(": ") || "Error";
    const traceback = Array.isArray(output.traceback)
      ? output.traceback.join("\n")
      : valueText(output.traceback);
    return `<div class="nb-output-error"><div class="nb-output-error-title">${escapeHtml(
      stripAnsi(title)
    )}</div>${traceback ? textBlock(traceback, "nb-output-stderr") : ""}</div>`;
  }
  if (type === "execute_result" || type === "display_data" || type === "update_display_data") {
    const data = output.data && typeof output.data === "object" ? output.data : {};
    const blocks = [];
    const image = imageBlock(data);
    if (image) blocks.push(image);
    const text = valueText(data["text/plain"]);
    if (text) blocks.push(textBlock(text));
    if (!blocks.length && data["application/json"] != null) {
      const value = data["application/json"];
      const json =
        typeof value === "string" ? value : JSON.stringify(value, null, 2);
      if (json) blocks.push(textBlock(json));
    }
    if (!blocks.length) {
      if (data["text/html"] || data["text/markdown"] || data["text/latex"]) {
        blocks.push(
          `<div class="nb-output-note">${escapeHtml(
            t("notebooks.editor.outputHtmlOmitted")
          )}</div>`
        );
      } else {
        const types = Object.keys(data).join(", ") || "unknown";
        blocks.push(
          `<div class="nb-output-note">${escapeHtml(
            t("notebooks.editor.outputUnsupported", { types })
          )}</div>`
        );
      }
    }
    return blocks.join("");
  }
  return "";
}

function outputPlainText(cell) {
  const parts = [];
  for (const output of Array.isArray(cell.outputs) ? cell.outputs : []) {
    if (!output || typeof output !== "object") continue;
    const type = output.output_type;
    if (type === "stream") {
      parts.push(stripAnsi(valueText(output.text)));
    } else if (type === "error") {
      const title = [output.ename, output.evalue].filter(Boolean).join(": ");
      const traceback = Array.isArray(output.traceback)
        ? output.traceback.join("\n")
        : valueText(output.traceback);
      parts.push(stripAnsi([title, traceback].filter(Boolean).join("\n")));
    } else if (type === "execute_result" || type === "display_data" || type === "update_display_data") {
      const data = output.data && typeof output.data === "object" ? output.data : {};
      const text = valueText(data["text/plain"]);
      if (text) {
        parts.push(text);
      } else if (data["application/json"] != null) {
        const value = data["application/json"];
        parts.push(typeof value === "string" ? value : JSON.stringify(value, null, 2));
      }
    }
  }
  return parts.filter(Boolean).join("\n").trimEnd();
}

function renderOutputs(cell, index) {
  const outputs = Array.isArray(cell.outputs) ? cell.outputs : [];
  if (!outputs.length) return "";
  const shown = outputs.slice(0, MAX_OUTPUT_ITEMS);
  const blocks = shown.map(renderOneOutput).filter(Boolean);
  if (!blocks.length) return "";
  const remaining = outputs.length - shown.length;
  return `<div class="nb-cell-outputs">
    <div class="nb-outputs-head" title="${escapeHtml(t("notebooks.editor.outputsHint"))}">
      <span>${escapeHtml(t("notebooks.editor.outputs"))}</span>
      <span class="nb-outputs-head-right">
        <span>${escapeHtml(t("notebooks.editor.outputsCount", { n: outputs.length }))}</span>
        <button type="button" class="icon-btn nb-output-copy" data-copy-output="${index}"
          data-tooltip="${escapeHtml(t("notebooks.editor.outputCopy"))}"
          aria-label="${escapeHtml(t("notebooks.editor.outputCopy"))}">${icons.copy}</button>
      </span>
    </div>
    ${blocks.join("")}
    ${
      remaining > 0
        ? `<div class="nb-output-note">${escapeHtml(
            t("notebooks.editor.outputsCount", { n: remaining })
          )}</div>`
        : ""
    }
  </div>`;
}

function renderCellPreview(cell, index, type) {
  const source = sourceOf(cell);
  if (!source.trim()) {
    return `<div class="nb-cell-preview nb-cell-preview-empty" data-preview-cell="${index}">${escapeHtml(
      t("notebooks.editor.emptyPreview")
    )}</div>`;
  }
  if (type === "markdown") {
    let html = "";
    try {
      html = renderMarkdown(source);
    } catch {
      html = `<pre>${escapeHtml(source)}</pre>`;
    }
    return `<div class="nb-cell-preview nb-markdown-preview" data-preview-cell="${index}">${html}</div>`;
  }
  return `<pre class="nb-cell-preview nb-code-preview" data-preview-cell="${index}" ${
    type === "code" ? `data-code-preview="${index}"` : ""
  }>${escapeHtml(source)}</pre>`;
}

function detectedTypeText(cell) {
  const type = effectiveCellType(cell);
  return t("notebooks.editor.autoDetected", {
    type: t(`notebooks.editor.${type}`),
  });
}

function markdownHeadings(source) {
  const headings = [];
  let fence = false;
  for (const rawLine of String(source || "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (/^(```|~~~)/.test(line)) {
      fence = !fence;
      continue;
    }
    if (fence) continue;
    const match = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (!match) continue;
    const text = match[2]
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/[*_`~]/g, "")
      .trim();
    if (text) headings.push({ level: match[1].length, text: text.slice(0, 120) });
  }
  return headings;
}

function typeMenuOptions(selected) {
  return TYPE_OPTIONS.map(
    (type) =>
      `<button type="button" class="nb-cell-type-option${
        type === selected ? " selected" : ""
      }" role="option"
        data-type-option="${type}" aria-selected="${
          type === selected ? "true" : "false"
        }">${escapeHtml(t(`notebooks.editor.${type}`))}</button>`
  ).join("");
}

export async function renderNotebookEditor(root, ctx) {
  let notebookId = ctx.params.id || null;
  let disposed = false;
  let scriptDirty = false;
  let sessionDirty = false;
  let savingScript = false;
  let savingSession = false;
  let documentNode = null;
  let cells = [];
  let notebookName = "";
  let activeIndex = 0;
  let lastDeleteAt = 0;
  let editingIndex = null;
  let editorInstance = null;
  let editorBlurTimer = null;
  let runningIndex = null;
  let pendingRun = false;
  let restarting = false;
  let openTypeMenuIndex = null;
  let datasets = [];
  let notebookDatasetId = null;
  let savingDataset = false;

  root.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;

  try {
    if (notebookId) {
      const [detail, contentData, datasetPage] = await Promise.all([
        http.get(`/api/notebooks/${notebookId}`),
        http.get(`/api/notebooks/${notebookId}/content`),
        http.get("/api/datasets?page=1&size=100").catch(() => null),
      ]);
      datasets = Array.isArray(datasetPage?.records) ? datasetPage.records : [];
      notebookDatasetId = detail.datasetId || null;
      let sessionData = null;
      try {
        sessionData = await http.get(`/api/notebooks/${notebookId}/session`);
      } catch {
        sessionData = null;
        toast(t("notebooks.editor.sessionLoadFailed"), "error");
      }
      if (disposed) return;
      documentNode = contentData.content || defaultContent();
      cells = (Array.isArray(documentNode.cells) ? documentNode.cells : []).map((cell) => ({
        ...cell,
        id: cell.id ? String(cell.id) : newCellId(),
        source: sourceOf(cell),
      }));
      const matched = new Map(
        (Array.isArray(sessionData?.cells) ? sessionData.cells : [])
          .filter((item) => item && item.matchesScript && item.cellId)
          .map((item) => [String(item.cellId), item])
      );
      cells = cells.map((cell) => {
        const entry = matched.get(cell.id);
        if (effectiveCellType(cell) !== "code") return cell;
        return {
          ...cell,
          outputs: entry && Array.isArray(entry.outputs) ? entry.outputs : [],
          execution_count:
            entry && entry.executionCount != null ? entry.executionCount : null,
        };
      });
      notebookName = contentData.name || detail.name || "";
    } else {
      documentNode = defaultContent();
      cells = documentNode.cells.map((cell) => ({ ...cell }));
      notebookName = t("notebooks.editor.untitled");
      datasets = [];
      notebookDatasetId = null;
    }
  } catch (error) {
    if (disposed) return;
    root.innerHTML = `<div class="panel"><div class="empty">
      <div class="empty-title">${escapeHtml(
        t("notebooks.editor.loadFailed", { message: error.message })
      )}</div>
      <a class="btn btn-secondary" href="#/notebooks">${escapeHtml(
        t("notebooks.editor.back")
      )}</a>
    </div></div>`;
    return () => {
      disposed = true;
    };
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

  function datasetOptions() {
    const options = [
      `<option value="">${escapeHtml(t("notebooks.editor.datasetNone"))}</option>`,
    ];
    let found = false;
    for (const dataset of datasets) {
      const selected = dataset.id === notebookDatasetId;
      if (selected) found = true;
      options.push(
        `<option value="${escapeHtml(dataset.id)}"${selected ? " selected" : ""}>${escapeHtml(
          datasetLabel(dataset)
        )}</option>`
      );
    }
    if (notebookDatasetId && !found) {
      options.push(
        `<option value="${escapeHtml(notebookDatasetId)}" selected>${escapeHtml(
          t("notebooks.editor.datasetUnavailable")
        )}</option>`
      );
    }
    return options.join("");
  }

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text nb-editor-head">
        <a class="nb-editor-back" href="#/notebooks" id="nb-editor-back"
          data-tooltip="${escapeHtml(t("notebooks.editor.back"))}"
          aria-label="${escapeHtml(t("notebooks.editor.back"))}">${icons.chevronLeft}</a>
        <div class="nb-editor-head-text">
          <h1 class="page-title nb-editor-title" id="nb-editor-title">
            <span class="nb-editor-title-text" id="nb-editor-title-text">${escapeHtml(
              notebookName || t("notebooks.editor.untitled")
            )}</span>
            <input class="input nb-editor-title-input" id="nb-editor-title-input" maxlength="120"
              placeholder="${escapeHtml(t("notebooks.editor.namePlaceholder"))}" hidden />
            <button type="button" class="icon-btn nb-editor-title-edit" id="nb-editor-title-edit"
              data-tooltip="${escapeHtml(t("notebooks.editor.renameHint"))}"
              aria-label="${escapeHtml(t("notebooks.editor.renameHint"))}">${icons.edit}</button>
          </h1>
          <p class="page-sub">${escapeHtml(t("notebooks.editor.subtitle"))}</p>
        </div>
      </div>
      <div class="page-head-actions">
        <span class="nb-editor-dirty" id="nb-editor-dirty"></span>
        <button type="button" class="btn btn-secondary" id="nb-editor-save-session"
          data-tooltip="${escapeHtml(t("notebooks.editor.saveSessionHint"))}" disabled>
          ${icons.saveSession}${escapeHtml(t("notebooks.editor.saveSession"))}
        </button>
        <button type="button" class="btn btn-primary" id="nb-editor-save">${icons.check}${escapeHtml(
          t("notebooks.editor.save")
        )}</button>
      </div>
    </div>
    <section class="nb-editor">
      <div class="nb-editor-toolbar" id="nb-editor-toolbar" role="toolbar"
        aria-label="${escapeHtml(t("notebooks.editor.toolbar"))}">
        <span class="nb-toolbar-cell" id="nb-toolbar-cell"></span>
        <label class="nb-editor-dataset"
          data-tooltip="${escapeHtml(t("notebooks.editor.datasetHint"))}">
          <span>${escapeHtml(t("notebooks.editor.dataset"))}</span>
          <select id="nb-editor-dataset"
            aria-label="${escapeHtml(t("notebooks.editor.dataset"))}">
            ${datasetOptions()}
          </select>
        </label>
        <div class="nb-toolbar-actions">
          <button type="button" class="nb-toolbar-text-btn" data-toolbar-action="run-all"
            data-tooltip="${escapeHtml(t("notebooks.editor.runAllHint"))}">
            ${icons.runs}<span>${escapeHtml(t("notebooks.editor.runAll"))}</span>
          </button>
          <button type="button" class="nb-toolbar-text-btn" data-toolbar-action="restart-kernel"
            data-tooltip="${escapeHtml(t("notebooks.editor.restartKernelHint"))}">
            ${icons.refresh}<span>${escapeHtml(t("notebooks.editor.restartKernel"))}</span>
          </button>
          <button type="button" class="nb-toolbar-text-btn" data-toolbar-action="download"
            data-tooltip="${escapeHtml(t("notebooks.editor.downloadCell"))}">
            ${icons.download}<span>${escapeHtml(t("notebooks.editor.download"))}</span>
          </button>
        </div>
        <span class="nb-editor-status" id="nb-editor-status"></span>
      </div>
      <div class="nb-editor-layout">
        <div class="nb-editor-main">
          <div class="nb-cells" id="nb-cells"></div>
          <div class="nb-editor-add">
            <button type="button" class="btn btn-secondary btn-sm" data-add-cell="code">${
              icons.plus
            }${escapeHtml(t("notebooks.editor.addCode"))}</button>
            <button type="button" class="btn btn-secondary btn-sm" data-add-cell="markdown">${
              icons.plus
            }${escapeHtml(t("notebooks.editor.addMarkdown"))}</button>
            <button type="button" class="btn btn-secondary btn-sm" data-add-cell="raw">${
              icons.plus
            }${escapeHtml(t("notebooks.editor.addRaw"))}</button>
          </div>
        </div>
        <aside class="nb-toc">
          <section class="nb-toc-section">
            <div class="nb-toc-title">
              <span>${escapeHtml(t("notebooks.editor.tocTitle"))}</span>
              <span class="nb-toc-count" id="nb-toc-count"></span>
            </div>
            <nav class="nb-toc-list" id="nb-toc-list"></nav>
          </section>
          <section class="nb-toc-section">
            <div class="nb-toc-title">
              <span>${escapeHtml(t("notebooks.editor.cellsTitle"))}</span>
              <span class="nb-toc-count" id="nb-cell-count"></span>
            </div>
            <nav class="nb-toc-list nb-cell-list" id="nb-cell-list"></nav>
          </section>
        </aside>
      </div>
    </section>`;

  const cellsHost = root.querySelector("#nb-cells");
  const tocList = root.querySelector("#nb-toc-list");
  const tocCount = root.querySelector("#nb-toc-count");
  const cellList = root.querySelector("#nb-cell-list");
  const cellCount = root.querySelector("#nb-cell-count");
  const titleText = root.querySelector("#nb-editor-title-text");
  const titleInput = root.querySelector("#nb-editor-title-input");
  const titleEditButton = root.querySelector("#nb-editor-title-edit");
  const saveButton = root.querySelector("#nb-editor-save");
  const saveSessionButton = root.querySelector("#nb-editor-save-session");
  const dirtyLabel = root.querySelector("#nb-editor-dirty");
  const statusLabel = root.querySelector("#nb-editor-status");
  const backLink = root.querySelector("#nb-editor-back");
  const editorToolbar = root.querySelector("#nb-editor-toolbar");
  const datasetSelect = root.querySelector("#nb-editor-dataset");
  const toolbarCell = root.querySelector("#nb-toolbar-cell");
  let tooltipTarget = null;
  let tooltipHideTimer = null;
  document.querySelectorAll(".app-tooltip").forEach((element) => element.remove());
  const tooltip = document.createElement("div");
  tooltip.className = "app-tooltip";
  tooltip.setAttribute("role", "tooltip");
  document.body.appendChild(tooltip);

  function hideTooltip() {
    if (tooltipHideTimer) {
      clearTimeout(tooltipHideTimer);
      tooltipHideTimer = null;
    }
    tooltipTarget = null;
    tooltip.classList.remove("show");
  }

  function positionTooltip(target) {
    if (!target || !target.isConnected) {
      hideTooltip();
      return;
    }
    const margin = 8;
    const rect = target.getBoundingClientRect();
    tooltip.style.maxWidth = `${Math.max(
      120,
      Math.min(320, window.innerWidth - margin * 2)
    )}px`;
    tooltip.style.left = "0px";
    tooltip.style.top = "0px";
    const bubble = tooltip.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - bubble.width / 2;
    left = Math.max(
      margin,
      Math.min(left, window.innerWidth - margin - bubble.width)
    );
    let top = rect.bottom + 7;
    if (top + bubble.height > window.innerHeight - margin) {
      top = rect.top - bubble.height - 7;
    }
    if (top < margin) top = rect.bottom + 7;
    tooltip.style.left = `${Math.round(left)}px`;
    tooltip.style.top = `${Math.round(top)}px`;
  }

  function showTooltip(target) {
    const text = target && target.dataset ? target.dataset.tooltip : "";
    if (!text) {
      hideTooltip();
      return;
    }
    tooltipTarget = target;
    tooltip.textContent = text;
    tooltip.classList.add("show");
    positionTooltip(target);
  }

  function tooltipElement(target) {
    return target && typeof target.closest === "function"
      ? target.closest("[data-tooltip]")
      : null;
  }

  function handleTooltipOver(event) {
    const target = tooltipElement(event.target);
    if (!target) return;
    if (event.relatedTarget && target.contains(event.relatedTarget)) return;
    showTooltip(target);
  }

  function handleTooltipOut(event) {
    if (!tooltipTarget) return;
    if (event.relatedTarget && tooltipTarget.contains(event.relatedTarget)) return;
    const target = tooltipElement(event.target);
    if (target && target !== tooltipTarget) return;
    hideTooltip();
  }

  function handleTooltipFocusIn(event) {
    const target = tooltipElement(event.target);
    if (target) showTooltip(target);
  }

  function handleTooltipFocusOut(event) {
    const target = tooltipElement(event.target);
    if (target && target === tooltipTarget) hideTooltip();
  }

  function handleTooltipPointerDown(event) {
    if (event.pointerType !== "touch") return;
    const target = tooltipElement(event.target);
    if (!target) {
      hideTooltip();
      return;
    }
    showTooltip(target);
    if (tooltipHideTimer) clearTimeout(tooltipHideTimer);
    tooltipHideTimer = setTimeout(() => {
      tooltipHideTimer = null;
      hideTooltip();
    }, 1800);
  }

  function handleTooltipKeydown(event) {
    if (event.key === "Escape") hideTooltip();
  }

  root.addEventListener("mouseover", handleTooltipOver);
  root.addEventListener("mouseout", handleTooltipOut);
  root.addEventListener("focusin", handleTooltipFocusIn);
  root.addEventListener("focusout", handleTooltipFocusOut);
  root.addEventListener("pointerdown", handleTooltipPointerDown);
  document.addEventListener("keydown", handleTooltipKeydown);
  window.addEventListener("scroll", hideTooltip, true);
  window.addEventListener("resize", hideTooltip);

  function updateDirtyState() {
    dirtyLabel.textContent =
      scriptDirty && sessionDirty
        ? t("notebooks.editor.unsavedBoth")
        : scriptDirty
          ? t("notebooks.editor.unsavedScript")
          : sessionDirty
            ? t("notebooks.editor.unsavedSession")
            : "";
    saveSessionButton.disabled = !sessionDirty || savingSession || savingScript;
  }

  function markScriptDirty() {
    scriptDirty = true;
    updateDirtyState();
  }

  function markSessionDirty() {
    sessionDirty = true;
    updateDirtyState();
  }

  function hasUnsaved() {
    return scriptDirty || sessionDirty;
  }

  function titleDisplay() {
    return String(notebookName || "").trim() || t("notebooks.editor.untitled");
  }

  function renderTitle() {
    if (!titleText || !titleInput) return;
    titleText.textContent = titleDisplay();
    titleInput.value = notebookName || "";
    document.title = `${titleDisplay()} · TrainHub`;
  }

  function startTitleEdit() {
    if (titleInput.hidden) {
      titleInput.value = notebookName || "";
      titleText.hidden = true;
      titleEditButton.hidden = true;
      titleInput.hidden = false;
      titleInput.focus();
      titleInput.select();
    }
  }

  function commitTitleEdit({ cancel = false } = {}) {
    if (titleInput.hidden) return;
    if (!cancel) {
      const next = titleInput.value.trim();
      if (next !== notebookName) {
        notebookName = next;
        markScriptDirty();
      }
    }
    titleInput.hidden = true;
    titleText.hidden = false;
    titleEditButton.hidden = false;
    renderTitle();
  }

  function cellSummary(cell) {
    const line = sourceOf(cell)
      .split(/\r?\n/)
      .find((item) => item.trim());
    return line ? line.trim() : t("notebooks.editor.emptyCellSummary");
  }

  function renderToc() {
    const items = [];
    cells.forEach((cell, index) => {
      if (effectiveCellType(cell) !== "markdown") return;
      for (const heading of markdownHeadings(sourceOf(cell))) {
        items.push({ ...heading, index });
      }
    });
    if (tocCount) tocCount.textContent = String(items.length);
    if (!items.length) {
      tocList.innerHTML = `<div class="nb-toc-empty">${escapeHtml(
        t("notebooks.editor.tocEmpty")
      )}</div>`;
    } else {
      tocList.innerHTML = items
        .map(
          (item) => `<button type="button" class="nb-toc-item level-${item.level} ${
            item.index === activeIndex ? "active" : ""
          }" data-toc-cell="${item.index}" title="${escapeHtml(item.text)}">${escapeHtml(
            item.text
          )}</button>`
        )
        .join("");
    }
    // 单元格清单：序号 + 类型 + 首个非空行摘要（与 WMG 训练代码编辑器一致）
    if (cellCount) cellCount.textContent = String(cells.length);
    if (cellList) {
      cellList.innerHTML = cells
        .map((cell, index) => {
          const type = effectiveCellType(cell);
          const summary = cellSummary(cell);
          return `<button type="button" class="nb-cell-link ${
            index === activeIndex ? "active" : ""
          }" data-cell-link="${index}" title="${escapeHtml(summary)}">
            <span class="nb-cell-link-index">${index + 1}</span>
            <span class="nb-cell-link-type">${escapeHtml(
              t(`notebooks.editor.${type}`)
            )}</span>
            <span class="nb-cell-link-text">${escapeHtml(summary)}</span>
          </button>`;
        })
        .join("");
    }
  }

  function updateDetectedBadge(index) {
    const cell = cells[index];
    const badge = cellsHost.querySelector(`[data-detected="${index}"]`);
    if (badge) {
      badge.textContent =
        cell && cell.cell_type === "auto" ? detectedTypeText(cell) : "";
    }
    if (index === activeIndex) syncToolbar();
  }

  function setCellTypeMenu(index) {
    const next = Number.isInteger(index) ? index : null;
    openTypeMenuIndex = next;
    cellsHost.querySelectorAll("[data-cell-type-menu]").forEach((menu) => {
      menu.hidden = Number(menu.dataset.cellTypeMenu) !== next;
    });
    cellsHost.querySelectorAll("[data-cell-type-button]").forEach((button) => {
      const isOpen = Number(button.dataset.cellTypeButton) === next;
      button.classList.toggle("open", isOpen);
      button.setAttribute("aria-expanded", String(isOpen));
    });
  }

  function closeCellTypeMenu() {
    setCellTypeMenu(null);
  }

  function syncToolbar() {
    const cell = cells[activeIndex];
    const hasCell = Boolean(cell);
    toolbarCell.textContent = hasCell
      ? t("notebooks.editor.activeCell", {
          index: activeIndex + 1,
          total: cells.length,
        })
      : t("notebooks.editor.noActiveCell");
    const busy = isBusy();
    if (datasetSelect) {
      datasetSelect.disabled = busy || savingDataset || !notebookId;
    }
    const hasRunnableCode = cells.some(
      (item) => effectiveCellType(item) === "code" && sourceOf(item).trim()
    );
    const disabled = {
      "run-all": !hasRunnableCode || busy,
      "restart-kernel": busy,
      download: !hasCell,
    };
    editorToolbar.querySelectorAll("[data-toolbar-action]").forEach((button) => {
      button.disabled = Boolean(disabled[button.dataset.toolbarAction]);
    });
    cellsHost.querySelectorAll("[data-quick-run]").forEach((button) => {
      button.disabled = busy;
    });
    const runButton = editorToolbar.querySelector('[data-toolbar-action="run-all"]');
    if (runButton) {
      const runLabel = hasRunnableCode
        ? t("notebooks.editor.runAllHint")
        : t("notebooks.editor.onlyCodeRunnable");
      runButton.dataset.tooltip = runLabel;
      runButton.setAttribute("aria-label", runLabel);
    }
    updateRunStatus();
  }

  /** 头部状态提示（Kaggle 风格）：空闲时 Editing，执行单元格期间 Running */
  function updateRunStatus() {
    if (!statusLabel) return;
    const running = isRunningNow();
    statusLabel.textContent = running
      ? t("notebooks.editor.statusRunning")
      : t("notebooks.editor.statusEditing");
    statusLabel.classList.toggle("running", running);
    statusLabel.classList.toggle("editing", !running);
  }

  function setActiveIndex(index, rerender = false) {
    if (!Number.isInteger(index) || index < 0 || index >= cells.length) return;
    activeIndex = index;
    syncToolbar();
    if (rerender) {
      renderCells();
      return;
    }
    cellsHost.querySelectorAll("[data-cell]").forEach((element) => {
      element.classList.toggle("active", Number(element.dataset.cell) === index);
    });
    renderToc();
  }

  function destroyEditor() {
    if (editorInstance) {
      editorInstance.destroy();
      editorInstance = null;
    }
    if (editorBlurTimer) {
      clearTimeout(editorBlurTimer);
      editorBlurTimer = null;
    }
  }

  function renderCells() {
    destroyEditor();
    closeCellTypeMenu();
    hideTooltip();
    if (!cells.length) {
      editingIndex = null;
      activeIndex = 0;
      cellsHost.innerHTML = `<div class="empty nb-editor-empty">
        <div class="empty-text">${escapeHtml(t("notebooks.editor.needCell"))}</div>
      </div>`;
      renderToc();
      syncToolbar();
      return;
    }
    activeIndex = Math.max(0, Math.min(activeIndex, cells.length - 1));
    if (editingIndex !== null && editingIndex >= cells.length) editingIndex = null;
    cellsHost.innerHTML = cells
      .map((cell, index) => {
        const type = effectiveCellType(cell);
        const typeValue = TYPE_OPTIONS.includes(cell.cell_type) ? cell.cell_type : "auto";
        const detected =
          typeValue === "auto"
            ? `<span class="nb-cell-detected" data-detected="${index}">${escapeHtml(
                detectedTypeText(cell)
              )}</span>`
            : `<span class="nb-cell-detected" data-detected="${index}"></span>`;
        const editing = index === editingIndex;
        const markdownPreview = type === "markdown" && !editing;
        const quickRun =
          type === "code"
            ? `<button type="button" class="nb-cell-run" data-quick-run="${index}"
              data-tooltip="${escapeHtml(t("notebooks.editor.runCellHint"))}"
              aria-label="${escapeHtml(t("notebooks.editor.runCellHint"))}">${icons.runs}</button>`
            : "";
        const body = editing
          ? `<div class="nb-cell-editor" data-editor-host="${index}"><div class="nb-editor-loading">${escapeHtml(
              t("common.loading")
            )}</div></div>`
          : renderCellPreview(cell, index, type);
        const running = runningIndex === index;
        return `<div class="nb-cell ${index === activeIndex ? "active" : ""} ${
          editing ? "editing" : ""
        } ${running ? "running" : ""} ${
          markdownPreview ? "markdown-preview" : ""
        }" data-cell="${index}">
          ${
            markdownPreview
              ? ""
              : `<div class="nb-cell-prompt">
            <span class="nb-cell-index">[${index + 1}]</span>${quickRun}
          </div>`
          }
          <div class="nb-cell-main">
            ${
              markdownPreview
                ? ""
                : `<div class="nb-cell-head">
              <div class="nb-cell-type-group" data-cell-type-group="${index}">
                <button type="button" class="nb-cell-type-button" data-cell-type-button="${index}"
                  aria-haspopup="listbox" aria-expanded="false"
                  aria-label="${escapeHtml(t("notebooks.editor.cellType"))}"
                  data-tooltip="${escapeHtml(t("notebooks.editor.cellType"))}">
                  <span data-cell-type-value="${index}">${escapeHtml(
                    t(`notebooks.editor.${typeValue}`)
                  )}</span>${icons.chevronDown}
                </button>
                <div class="nb-cell-type-menu" data-cell-type-menu="${index}"
                  role="listbox" aria-label="${escapeHtml(
                    t("notebooks.editor.cellType")
                  )}" hidden>${typeMenuOptions(typeValue)}</div>
              </div>${detected}
              <span class="spacer"></span>
              <div class="nb-cell-head-actions">
                <button type="button" class="icon-btn" data-cell-action="insert-below"
                  data-tooltip="${escapeHtml(t("notebooks.editor.insertBelow"))}"
                  aria-label="${escapeHtml(t("notebooks.editor.insertBelow"))}">${icons.insertBelow}</button>
                <button type="button" class="icon-btn" data-cell-action="insert-above"
                  data-tooltip="${escapeHtml(t("notebooks.editor.insertAbove"))}"
                  aria-label="${escapeHtml(t("notebooks.editor.insertAbove"))}">${icons.insertAbove}</button>
                <button type="button" class="icon-btn" data-cell-action="move-up"
                  data-tooltip="${escapeHtml(t("notebooks.editor.moveUp"))}"
                  aria-label="${escapeHtml(t("notebooks.editor.moveUp"))}"
                  ${index === 0 ? "disabled" : ""}>${icons.chevronUp}</button>
                <button type="button" class="icon-btn" data-cell-action="move-down"
                  data-tooltip="${escapeHtml(t("notebooks.editor.moveDown"))}"
                  aria-label="${escapeHtml(t("notebooks.editor.moveDown"))}"
                  ${index === cells.length - 1 ? "disabled" : ""}>${icons.chevronDown}</button>
                <button type="button" class="icon-btn" data-cell-action="duplicate"
                  data-tooltip="${escapeHtml(t("notebooks.editor.duplicateCell"))}"
                  aria-label="${escapeHtml(t("notebooks.editor.duplicateCell"))}">${icons.copy}</button>
                <button type="button" class="icon-btn nb-toolbar-danger" data-cell-action="delete"
                  data-tooltip="${escapeHtml(t("notebooks.editor.deleteCell"))}"
                  aria-label="${escapeHtml(t("notebooks.editor.deleteCell"))}">${icons.trash}</button>
              </div>
            </div>`
            }
            ${body}
          </div>
          <div class="nb-cell-output-slot" data-output-slot="${index}">${
            type === "code" ? renderOutputs(cell, index) : ""
          }</div>
        </div>`;
      })
      .join("");
    renderToc();
    syncToolbar();
    void enhanceCodePreviews();
    if (editingIndex !== null) void mountEditor(editingIndex);
  }

  function enhanceCodePreviews() {
    const previews = Array.from(cellsHost.querySelectorAll("[data-code-preview]"));
    if (!previews.length) return;
    if (!codeMirrorModulePromise) {
      codeMirrorModulePromise = import("../vendor/trainhub-codemirror.js");
    }
    return codeMirrorModulePromise
      .then((module) => {
        if (disposed || typeof module.renderCodePreview !== "function") return;
        for (const node of previews) {
          if (!node.isConnected) continue;
          const index = Number(node.dataset.codePreview);
          if (!Number.isInteger(index) || editingIndex === index) continue;
          const cell = cells[index];
          if (!cell) continue;
          const html = module.renderCodePreview(sourceOf(cell), "python");
          if (html != null) node.innerHTML = html;
        }
      })
      .catch(() => {});
  }

  async function mountEditor(index) {
    const cell = cells[index];
    if (!cell || editingIndex !== index) return;
    const host = cellsHost.querySelector(`[data-editor-host="${index}"]`);
    if (!host) return;
    if (!codeMirrorModulePromise) {
      codeMirrorModulePromise = import("../vendor/trainhub-codemirror.js");
    }
    let module;
    try {
      module = await codeMirrorModulePromise;
    } catch (error) {
      codeMirrorModulePromise = null;
      toast(`CodeMirror: ${error.message}`, "error");
      editingIndex = null;
      renderCells();
      return;
    }
    if (
      disposed ||
      editingIndex !== index ||
      (typeof host.isConnected === "boolean" && !host.isConnected)
    ) {
      return;
    }
    host.replaceChildren();
    const type = effectiveCellType(cell);
    const language = type === "markdown" ? "markdown" : type === "code" ? "python" : "none";
    try {
      editorInstance = module.createNotebookEditor({
        parent: host,
        value: sourceOf(cell),
        language,
        ariaLabel: t("notebooks.editor.cellSource", { index: index + 1 }),
        placeholderText: t("notebooks.editor.placeholder"),
        onChange: (value) => {
          if (!cells[index]) return;
          cells[index].source = value;
          let clearedOutputs = false;
          if (Array.isArray(cells[index].outputs) && cells[index].outputs.length) {
            cells[index].outputs = [];
            clearedOutputs = true;
          }
          if (cells[index].execution_count != null) {
            cells[index].execution_count = null;
            clearedOutputs = true;
          }
          if (clearedOutputs) refreshCellOutputs(index);
          markScriptDirty();
          updateDetectedBadge(index);
          renderToc();
        },
        onFocus: () => setActiveIndex(index),
        onBlur: () => scheduleStopEditing(index),
      });
    } catch (error) {
      toast(`CodeMirror: ${error.message}`, "error");
      if (!disposed && editingIndex === index) {
        editingIndex = null;
        renderCells();
      }
    }
  }

  function scheduleStopEditing(index) {
    if (editorBlurTimer) clearTimeout(editorBlurTimer);
    editorBlurTimer = setTimeout(() => {
      editorBlurTimer = null;
      if (disposed || editingIndex !== index) return;
      const cell = cellsHost.querySelector(`[data-cell="${index}"]`);
      if (cell && cell.contains(document.activeElement)) return;
      editingIndex = null;
      renderCells();
    }, 120);
  }

  function startEditing(index) {
    if (!cells[index]) return;
    activeIndex = index;
    editingIndex = index;
    if (editorBlurTimer) {
      clearTimeout(editorBlurTimer);
      editorBlurTimer = null;
    }
    renderCells();
  }

  function insertCellAt(index, type) {
    const at = Math.max(0, Math.min(index, cells.length));
    cells.splice(at, 0, newCell(type));
    activeIndex = at;
    editingIndex = at;
    markScriptDirty();
    renderCells();
  }

  function addCell(type) {
    insertCellAt(cells.length, type);
  }

  function duplicateCell(index) {
    const sourceCell = cells[index];
    if (!sourceCell) return;
    const copy = JSON.parse(JSON.stringify(sourceCell));
    copy.id = newCellId();
    copy.source = sourceOf(sourceCell);
    if (copy.cell_type === "code" || !copy.cell_type) {
      copy.cell_type = "code";
      copy.outputs = [];
      copy.execution_count = null;
    } else {
      delete copy.outputs;
      delete copy.execution_count;
    }
    cells.splice(index + 1, 0, copy);
    activeIndex = index + 1;
    editingIndex = activeIndex;
    markScriptDirty();
    renderCells();
  }

  function moveCell(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= cells.length) return;
    [cells[index], cells[target]] = [cells[target], cells[index]];
    if (editingIndex === index) editingIndex = target;
    else if (editingIndex === target) editingIndex = index;
    activeIndex = target;
    markScriptDirty();
    renderCells();
  }

  function deleteCell(index) {
    if (!cells[index]) return;
    cells.splice(index, 1);
    if (editingIndex === index) editingIndex = null;
    else if (editingIndex !== null && editingIndex > index) editingIndex -= 1;
    activeIndex = Math.min(index, Math.max(0, cells.length - 1));
    markScriptDirty();
    renderCells();
  }

  function setCellType(index, value) {
    const cell = cells[index];
    if (!cell) return;
    const type = TYPE_OPTIONS.includes(value) ? value : "auto";
    cell.cell_type = type;
    if (type === "code") {
      cell.outputs = [];
      cell.execution_count = null;
    } else {
      delete cell.outputs;
      delete cell.execution_count;
    }
    activeIndex = index;
    markScriptDirty();
    renderCells();
  }

  titleEditButton.addEventListener("click", (event) => {
    event.stopPropagation();
    startTitleEdit();
  });
  titleInput.addEventListener("blur", () => commitTitleEdit());
  titleInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      titleInput.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      commitTitleEdit({ cancel: true });
      titleInput.blur();
    }
  });

  cellsHost.addEventListener("focusin", (event) => {
    const target = event.target;
    const cell = target && typeof target.closest === "function" ? target.closest("[data-cell]") : null;
    if (cell) setActiveIndex(Number(cell.dataset.cell));
  });

  cellsHost.addEventListener("click", (event) => {
    const target = event.target;
    const closest = (selector) =>
      target && typeof target.closest === "function" ? target.closest(selector) : null;
    const cell = closest("[data-cell]");
    if (cell) setActiveIndex(Number(cell.dataset.cell));
    const quickRunButton = closest("[data-quick-run]");
    if (quickRunButton) {
      runCellToHere(Number(quickRunButton.dataset.quickRun));
      return;
    }
    const copyOutputButton = closest("[data-copy-output]");
    if (copyOutputButton) {
      event.stopPropagation();
      copyCellOutput(Number(copyOutputButton.dataset.copyOutput));
      return;
    }
    const headAction = closest("[data-cell-action]");
    if (headAction && cell) {
      event.stopPropagation();
      if (headAction.disabled) return;
      const index = Number(cell.dataset.cell);
      switch (headAction.dataset.cellAction) {
        case "insert-below":
          insertCellAt(index + 1, "code");
          break;
        case "insert-above":
          insertCellAt(index, "code");
          break;
        case "move-up":
          moveCell(index, -1);
          break;
        case "move-down":
          moveCell(index, 1);
          break;
        case "duplicate":
          duplicateCell(index);
          break;
        case "delete":
          deleteCell(index);
          break;
        default:
          break;
      }
      return;
    }
    const typeButton = closest("[data-cell-type-button]");
    if (typeButton) {
      event.stopPropagation();
      const index = Number(typeButton.dataset.cellTypeButton);
      setCellTypeMenu(openTypeMenuIndex === index ? null : index);
      if (openTypeMenuIndex !== null) hideTooltip();
      return;
    }
    const typeOption = closest("[data-type-option]");
    if (typeOption) {
      event.stopPropagation();
      const menu = typeOption.closest("[data-cell-type-menu]");
      const index = Number(menu && menu.dataset.cellTypeMenu);
      if (!Number.isInteger(index)) return;
      closeCellTypeMenu();
      setCellType(index, typeOption.dataset.typeOption);
      return;
    }
    const preview = closest("[data-preview-cell]");
    if (preview) {
      const link = closest("a");
      if (link && link.getAttribute("href")) return;
      startEditing(Number(preview.dataset.previewCell));
    }
  });

  editorToolbar.addEventListener("click", (event) => {
    const target = event.target;
    const button =
      target && typeof target.closest === "function"
        ? target.closest("[data-toolbar-action]")
        : null;
    if (!button || button.disabled) return;
    switch (button.dataset.toolbarAction) {
      case "run-all":
        runAllCells();
        break;
      case "restart-kernel":
        void restartKernel();
        break;
      case "download":
        downloadCell(activeIndex);
        break;
      default:
        break;
    }
  });

  function handleDocumentClick(event) {
    const target = event.target;
    const element =
      target && typeof target.closest === "function"
        ? target
        : target && target.parentElement;
    if (
      openTypeMenuIndex !== null &&
      (!element ||
        typeof element.closest !== "function" ||
        !element.closest("[data-cell-type-group]"))
    ) {
      closeCellTypeMenu();
    }
    if (disposed || editingIndex === null) return;
    if (!element || typeof element.closest !== "function") return;
    const preview = element.closest("[data-preview-cell]");
    if (preview) {
      if (editingIndex === Number(preview.dataset.previewCell)) return;
      const link = element.closest("a");
      if (link && link.getAttribute("href")) {
        editingIndex = null;
        setTimeout(() => {
          if (!disposed) renderCells();
        }, 0);
        return;
      }
      editingIndex = null;
      renderCells();
      return;
    }
    if (element.closest("[data-editor-host]")) {
      return;
    }
    editingIndex = null;
    renderCells();
  }

  document.addEventListener("click", handleDocumentClick);

  function handleCellTypeKeydown(event) {
    if (event.key !== "Escape" || openTypeMenuIndex === null) return;
    event.preventDefault();
    event.stopPropagation();
    const index = openTypeMenuIndex;
    closeCellTypeMenu();
    hideTooltip();
    cellsHost.querySelector(`[data-cell-type-button="${index}"]`)?.focus();
  }

  root.addEventListener("keydown", handleCellTypeKeydown);

  /** 目录 / 单元格清单共用：选中并滚动到目标单元格；编辑态先退出编辑再滚动（避免滚到重建前的旧节点） */
  function gotoCell(index) {
    setActiveIndex(index);
    if (editingIndex !== null) {
      editingIndex = null;
      renderCells();
    }
    scrollBelowAffix(cellsHost.querySelector(`[data-cell="${index}"]`));
  }

  tocList.addEventListener("click", (event) => {
    const button =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-toc-cell]")
        : null;
    if (!button) return;
    gotoCell(Number(button.dataset.tocCell));
  });

  cellList.addEventListener("click", (event) => {
    const button =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-cell-link]")
        : null;
    if (!button) return;
    gotoCell(Number(button.dataset.cellLink));
  });

  root.querySelectorAll("[data-add-cell]").forEach((button) => {
    button.addEventListener("click", () => addCell(button.dataset.addCell));
  });

  function downloadCell(index) {
    const cell = cells[index];
    if (!cell) return;
    const type = effectiveCellType(cell);
    const suffix = type === "markdown" ? ".md" : type === "raw" ? ".txt" : ".py";
    const safeName = String(notebookName || "notebook")
      .replace(/[^\w.\-\u4e00-\u9fff]+/g, "-")
      .slice(0, 80) || "notebook";
    const blob = new Blob([sourceOf(cell)], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${safeName}-cell-${index + 1}${suffix}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  function refreshCellOutputs(index) {
    const cell = cells[index];
    const slot = cellsHost.querySelector(`[data-output-slot="${index}"]`);
    if (cell && slot) {
      slot.innerHTML = effectiveCellType(cell) === "code" ? renderOutputs(cell, index) : "";
    }
  }

  function stickyTopOffset() {
    // 顶部有 affix 的页面头（.topbar）与编辑器工具栏（.nb-editor-toolbar），
    // 二者会盖住视口顶部；按它们的实际高度做偏移，避免目标被压在 header 下面
    const topbar = document.querySelector(".topbar");
    const toolbar = document.querySelector(".nb-editor-toolbar");
    return (topbar ? topbar.offsetHeight : 0) + (toolbar ? toolbar.offsetHeight : 0) + 8;
  }

  function scrollBelowAffix(el) {
    if (!el || typeof el.getBoundingClientRect !== "function") return;
    // 等一帧让布局落定再滚动（输出可能刚写入 innerHTML）
    requestAnimationFrame(() => {
      const target = window.scrollY + el.getBoundingClientRect().top - stickyTopOffset();
      window.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
    });
  }

  function scrollToCellOutput(index) {
    scrollBelowAffix(cellsHost.querySelector(`[data-output-slot="${index}"]`));
  }

  function scrollToCell(index) {
    scrollBelowAffix(cellsHost.querySelector(`[data-cell="${index}"]`));
  }

  function refreshRunningState(index) {
    const cell = cellsHost.querySelector(`[data-cell="${index}"]`);
    if (cell) cell.classList.toggle("running", runningIndex === index);
    syncToolbar();
  }

  let serverRun = null;

  function isRunningNow() {
    return (
      pendingRun ||
      runningIndex !== null ||
      Boolean(serverRun && serverRun.status === "running")
    );
  }

  function isBusy() {
    return isRunningNow() || restarting;
  }

  function clearRunningProgress() {
    if (runningIndex === null) return;
    const previous = runningIndex;
    runningIndex = null;
    refreshRunningState(previous);
  }

  async function fetchCurrentRun(includeOutputs) {
    if (!notebookId) return null;
    return await http.get(
      `/api/notebooks/${notebookId}/runs/current?outputs=${includeOutputs ? 1 : 0}`
    );
  }

  /** 运行中的当前单元格高亮（多格运行会随进度移动） */
  function syncRunHighlight(run) {
    if (!run) return;
    const index = run.currentCellId
      ? cells.findIndex((cell) => cell.id && cell.id === run.currentCellId)
      : -1;
    if (index < 0 || index === runningIndex) return;
    const previous = runningIndex;
    runningIndex = index;
    if (previous !== null) refreshRunningState(previous);
    else syncToolbar();
  }

  let streamController = null;
  let streamRunId = null;
  let streamLastSeq = 0;

  function closeRunStream() {
    if (streamController) {
      try {
        streamController.abort();
      } catch {
        /* ignore */
      }
      streamController = null;
    }
    streamRunId = null;
  }

  /** 应用一条 SSE 事件（seq 去重，快照之后的增量按序生效） */
  function applyStreamEvent(event) {
    if (!event || !event.e) return false;
    const seq = Number(event.seq || 0);
    if (seq && seq <= streamLastSeq) return false;
    if (seq) streamLastSeq = seq;
    if (event.e === "state") {
      if (event.run) {
        serverRun = event.run;
        streamLastSeq = Number(event.run.lastEventSeq || seq || 0);
        applyExecutionResults(event.run);
        syncRunHighlight(event.run);
        if (event.run.status !== "running") clearRunningProgress();
        updateRunStatus();
        syncToolbar();
        return event.run.status !== "running";
      }
      return false;
    }
    if (event.e === "count" || event.e === "cell") {
      const index = cells.findIndex((cell) => cell.id && cell.id === event.cellId);
      if (index < 0) return false;
      const cell = cells[index];
      if (event.e === "cell" && event.status === "running" && index !== runningIndex) {
        const previous = runningIndex;
        runningIndex = index;
        if (previous !== null) refreshRunningState(previous);
        else syncToolbar();
      }
      if (event.executionCount != null && cell.execution_count !== event.executionCount) {
        cell.execution_count = event.executionCount;
        refreshCellOutputs(index);
      }
      return false;
    }
    if (event.e === "stream" || event.e === "output" || event.e === "outputs") {
      const index = cells.findIndex((cell) => cell.id && cell.id === event.cellId);
      if (index < 0) return false;
      const cell = cells[index];
      const current = Array.isArray(cell.outputs) ? cell.outputs : [];
      if (event.e === "stream") {
        const name = event.name || "stdout";
        const last = current[current.length - 1];
        if (last && last.output_type === "stream" && (last.name || "stdout") === name) {
          last.text = String(last.text || "") + String(event.text || "");
        } else {
          cell.outputs = [
            ...current,
            { output_type: "stream", name, text: String(event.text || "") },
          ];
        }
      } else if (event.e === "output") {
        cell.outputs = [...current, event.output];
      } else {
        cell.outputs = Array.isArray(event.outputs) ? event.outputs : [];
      }
      if (event.executionCount != null) cell.execution_count = event.executionCount;
      refreshCellOutputs(index);
      markSessionDirty();
      return false;
    }
    if (event.e === "resync") {
      void resyncRunState();
      return false;
    }
    if (event.e === "gone") return true;
    if (event.e === "run") {
      serverRun = {
        ...(serverRun || {}),
        runId: event.runId || (serverRun && serverRun.runId),
        status: event.status,
        failedCellId: event.failedCellId,
        message: event.message,
      };
      clearRunningProgress();
      updateRunStatus();
      syncToolbar();
      return true;
    }
    return false;
  }

  async function resyncRunState() {
    try {
      const run = await fetchCurrentRun(true);
      if (!run) return;
      serverRun = run;
      streamLastSeq = Number(run.lastEventSeq || streamLastSeq || 0);
      applyExecutionResults(run);
      syncRunHighlight(run);
      if (run.status !== "running") {
        clearRunningProgress();
        closeRunStream();
      }
      updateRunStatus();
      syncToolbar();
    } catch {
      /* ignore */
    }
  }

  /**
   * 跟随运行事件流（SSE over fetch，带鉴权头）：服务端先发 state 快照（回填已有输出），
   * 之后把输出变化实时推过来（stream 只发新增文本）；断线自动重连，重连同样先收快照重同步。
   */
  async function streamRun(runId) {
    streamRunId = runId;
    streamLastSeq = 0;
    let retries = 0;
    while (!disposed && streamRunId === runId) {
      const controller = new AbortController();
      streamController = controller;
      let terminal = false;
      let failed = false;
      try {
        const token = getToken();
        const response = await fetch(
          `/api/notebooks/${notebookId}/runs/stream?runId=${encodeURIComponent(runId)}`,
          {
            headers: {
              Accept: "text/event-stream",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            signal: controller.signal,
          }
        );
        if (!response.ok || !response.body) {
          const error = new Error(`运行事件流连接失败（HTTP ${response.status}）`);
          error.status = response.status;
          throw error;
        }
        retries = 0;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let separator = buffer.indexOf("\n\n");
          while (separator >= 0) {
            const raw = buffer.slice(0, separator);
            buffer = buffer.slice(separator + 2);
            separator = buffer.indexOf("\n\n");
            const dataLines = raw
              .split("\n")
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trim());
            if (!dataLines.length) continue; // 注释 / 心跳
            let event = null;
            try {
              event = JSON.parse(dataLines.join("\n"));
            } catch {
              continue;
            }
            if (applyStreamEvent(event)) terminal = true;
          }
          if (terminal) break;
        }
      } catch (error) {
        if (disposed || streamRunId !== runId || (controller.signal && controller.signal.aborted)) {
          return null;
        }
        if (error.status === 404) return null;
        failed = true;
      } finally {
        if (streamController === controller) streamController = null;
      }
      if (terminal || disposed || streamRunId !== runId) break;
      retries += 1;
      if (retries > 30) break;
      await new Promise((resolve) => setTimeout(resolve, failed ? 1000 : 300));
      if (disposed || streamRunId !== runId) break;
    }
    return serverRun;
  }

  /** 收集截至 index（含）的代码单元格，供单元格运行补跑前置使用 */
  function collectCodeCellsUpTo(index) {
    return cells
      .slice(0, index + 1)
      .filter((cell) => effectiveCellType(cell) === "code")
      .map((cell) => ({ cellId: cell.id, source: sourceOf(cell) }));
  }

  /** 收集全部代码单元格，供「运行全部」使用 */
  function collectAllCodeCells() {
    return cells
      .filter((cell) => effectiveCellType(cell) === "code")
      .map((cell) => ({ cellId: cell.id, source: sourceOf(cell) }));
  }

  async function startRunRequest(index, mode) {
    const payload = {
      timeoutSeconds: 0,
      kernel: documentNode?.metadata?.kernelspec?.name || null,
    };
    if (mode === "all") {
      const items = collectAllCodeCells();
      if (!items.length) throw new Error(t("notebooks.editor.onlyCodeRunnable"));
      payload.mode = "all";
      payload.cells = items;
    } else {
      const cell = cells[index];
      if (!cell || effectiveCellType(cell) !== "code") {
        throw new Error(t("notebooks.editor.onlyCodeRunnable"));
      }
      if (!sourceOf(cell).trim()) {
        throw new Error(t("notebooks.editor.emptyCode"));
      }
      const items = collectCodeCellsUpTo(index);
      if (!items.length) throw new Error(t("notebooks.editor.onlyCodeRunnable"));
      payload.mode = "upto";
      payload.cells = items;
      payload.targetCellId = cell.id || null;
    }
    if (!notebookId) {
      await persistNotebook({ navigateAfter: false });
    }
    return await http.post(`/api/notebooks/${notebookId}/runs`, payload);
  }

  /** 刷新/重新进入后接管：有运行就轮询，已结束则回填输出（运行态以服务端为准） */
  async function refreshRunState() {
    if (!notebookId) return;
    let run = null;
    try {
      run = await fetchCurrentRun(false);
    } catch {
      return;
    }
    serverRun = run;
    if (run && run.status !== "running") clearRunningProgress();
    updateRunStatus();
    syncToolbar();
    if (!run) return;
    if (run.status === "running") {
      syncRunHighlight(run);
      updateRunStatus();
      await streamRun(run.runId);
      let full = null;
      try {
        full = await fetchCurrentRun(true);
      } catch {
        full = null;
      }
      if (full && full.runId === run.runId) {
        serverRun = full;
        applyExecutionResults(full);
      }
      clearRunningProgress();
      updateRunStatus();
      syncToolbar();
      return;
    }
    let full = null;
    try {
      full = await fetchCurrentRun(true);
    } catch {
      full = null;
    }
    if (full && full.runId === run.runId) {
      serverRun = full;
      applyExecutionResults(full);
    }
    updateRunStatus();
    syncToolbar();
  }

  async function copyCellOutput(index) {
    const cell = cells[index];
    const text = cell ? outputPlainText(cell) : "";
    if (!text) {
      toast(t("notebooks.editor.outputCopyEmpty"), "info");
      return;
    }
    const ok = await copyText(text);
    toast(
      t(ok ? "notebooks.editor.outputCopied" : "notebooks.editor.outputCopyFailed"),
      ok ? "success" : "error"
    );
  }

  /** 把一次运行返回的逐格结果写回单元格输出区（跳过的单元格不动） */
  function applyExecutionResults(result) {
    let applied = false;
    for (const item of Array.isArray(result?.cells) ? result.cells : []) {
      if (!item || !item.cellId || item.status === "skipped") continue;
      const index = cells.findIndex((cell) => cell.id === item.cellId);
      if (index < 0) continue;
      const cell = cells[index];
      let changed = false;
      if (item.executionCount != null && item.executionCount !== cell.execution_count) {
        cell.execution_count = item.executionCount;
        changed = true;
      }
      if (Array.isArray(item.outputs)) {
        cell.outputs = item.outputs;
        changed = true;
      }
      if (!changed) continue;
      refreshCellOutputs(index);
      applied = true;
    }
    if (applied) markSessionDirty();
  }

  /**
   * 运行单元格（后台任务 + 轮询取回）。
   * - mode = "upto"：运行本格——前置代码单元格未执行或源码已改时自动补跑；
   * - mode = "all"：按顺序运行全部代码单元格（不跳过已执行格）。
   * 执行在服务端进行，刷新页面不中断；新页面会接管运行态并回填输出。
   */
  async function runCell(index, mode = "upto") {
    if (isBusy()) {
      toast(t("notebooks.editor.cellRunning"), "info");
      void refreshRunState();
      return;
    }
    const runAll = mode === "all";
    runningIndex = runAll ? null : index;
    if (runAll) {
      pendingRun = true;
      updateRunStatus();
      syncToolbar();
    } else {
      refreshRunningState(index);
    }
    let statusPoll = null;
    try {
      const started = await startRunRequest(index, mode);
      pendingRun = false;
      serverRun = { runId: started?.runId, status: "running", currentCellId: null, cells: [] };
      updateRunStatus();
      syncToolbar();
      if (started?.runId) {
        // SSE 是主通道；低频轮询兜底：SSE 收不到终态时，界面同样能回到可编辑状态并回填输出。
        statusPoll = setInterval(async () => {
          if (disposed || (serverRun && serverRun.runId && serverRun.runId !== started.runId)) {
            clearInterval(statusPoll);
            statusPoll = null;
            return;
          }
          try {
            const run = await fetchCurrentRun(false);
            if (!run || run.runId !== started.runId) {
              if (run && run.runId !== started.runId) {
                clearInterval(statusPoll);
                statusPoll = null;
              }
              return;
            }
            if (run.status === "running") {
              syncRunHighlight(run);
              return;
            }
            let full = null;
            try {
              full = await fetchCurrentRun(true);
            } catch {
              full = null;
            }
            if (full && full.runId === started.runId) {
              serverRun = full;
              applyExecutionResults(full);
            } else {
              serverRun = { ...(serverRun || {}), ...run };
            }
            clearRunningProgress();
            updateRunStatus();
            syncToolbar();
            clearInterval(statusPoll);
            statusPoll = null;
            closeRunStream();
          } catch {
            /* 轮询失败时保持 SSE；下一次再试 */
          }
        }, 4000);
      }
      await streamRun(started?.runId);
      let full = null;
      try {
        full = await fetchCurrentRun(true);
      } catch {
        full = null;
      }
      if (full && (!started?.runId || full.runId === started.runId)) {
        serverRun = full;
        applyExecutionResults(full);
      }
      const finalRun = full || serverRun;
      const failedIndex = finalRun?.failedCellId
        ? cells.findIndex((item) => item.id === finalRun.failedCellId)
        : -1;
      if (finalRun?.message) {
        const prefix =
          failedIndex >= 0
            ? t("notebooks.editor.runStoppedAt", { index: failedIndex + 1 })
            : "";
        toast(`${prefix}${finalRun.message}`, "error");
      }
      if (runAll) {
        if (failedIndex >= 0) {
          scrollToCell(failedIndex);
        } else {
          const ran = Array.isArray(finalRun?.cells) ? finalRun.cells : [];
          const lastWithOutput = [...ran]
            .reverse()
            .find((item) => Array.isArray(item.outputs) && item.outputs.length);
          if (lastWithOutput?.cellId) {
            const outputIndex = cells.findIndex((item) => item.id === lastWithOutput.cellId);
            if (outputIndex >= 0) scrollToCellOutput(outputIndex);
          }
        }
      } else {
        const target = cells[index];
        if (target && Array.isArray(target.outputs) && target.outputs.length) {
          scrollToCellOutput(index);
        }
      }
    } catch (error) {
      if (error.status === 409) {
        toast(error.message, "info");
        void refreshRunState();
      } else {
        toast(error.message, "error");
      }
    } finally {
      // 服务端仍报告运行中时保留兜底轮询（SSE 可能已经断了）；它会在终态或页面销毁时自停。
      if (statusPoll && (!serverRun || serverRun.status !== "running")) {
        clearInterval(statusPoll);
        statusPoll = null;
      }
      pendingRun = false;
      clearRunningProgress();
      closeRunStream();
      updateRunStatus();
      syncToolbar();
    }
  }

  function runCellToHere(index) {
    void runCell(index, "upto");
  }

  function runAllCells() {
    void runCell(activeIndex, "all");
  }

  /** 重启内核：清空变量与导入状态，不运行任何代码 */
  async function restartKernel() {
    if (isBusy()) {
      toast(t("notebooks.editor.cellRunning"), "info");
      return;
    }
    restarting = true;
    syncToolbar();
    try {
      if (!notebookId) {
        await persistNotebook({ navigateAfter: false });
      }
      await http.post(`/api/notebooks/${notebookId}/sessions/restart`, {
        kernel: documentNode?.metadata?.kernelspec?.name || null,
      });
      serverRun = null;
      clearRunningProgress();
      toast(t("notebooks.editor.kernelRestarted"), "success");
    } catch (error) {
      if (error.status === 409) {
        toast(error.message, "info");
        void refreshRunState();
      } else {
        toast(error.message, "error");
      }
    } finally {
      restarting = false;
      updateRunStatus();
      syncToolbar();
    }
  }

  async function persistNotebook({ navigateAfter = true } = {}) {
    if (savingScript) throw new Error(t("common.saving"));
    if (!cells.length) throw new Error(t("notebooks.editor.needCell"));
    commitTitleEdit();
    const content = buildContent(documentNode, cells);
    const payload = {
      name: notebookName.trim() || null,
      content,
    };
    savingScript = true;
    updateDirtyState();
    saveButton.disabled = true;
    saveButton.textContent = t("notebooks.editor.saving");
    try {
      let saved;
      if (notebookId) {
        saved = await http.put(`/api/notebooks/${notebookId}`, payload);
      } else {
        saved = await http.post("/api/notebooks", payload);
        if (saved && saved.id) {
          notebookId = saved.id;
        }
      }
      if (saved && saved.name) notebookName = saved.name;
      renderTitle();
      scriptDirty = false;
      updateDirtyState();
      if (navigateAfter) {
        toast(t("notebooks.editor.saveSuccess"), "success");
        ctx.navigate("/notebooks");
      }
      return saved;
    } finally {
      savingScript = false;
      saveButton.disabled = false;
      saveButton.innerHTML = `${icons.check}${escapeHtml(t("notebooks.editor.save"))}`;
      updateDirtyState();
    }
  }

  async function save() {
    if (savingSession) {
      toast(t("common.saving"), "info");
      return;
    }
    try {
      const navigateAfter = !sessionDirty;
      await persistNotebook({ navigateAfter });
      if (!navigateAfter) {
        toast(t("notebooks.editor.scriptSavedSessionPending"), "info");
      }
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function saveSession() {
    if (!notebookId) {
      toast(t("notebooks.editor.saveNotebookFirst"), "info");
      return;
    }
    if (scriptDirty) {
      toast(t("notebooks.editor.saveScriptFirst"), "info");
      return;
    }
    if (!sessionDirty || savingSession || savingScript) return;
    const payload = cells
      .filter(
        (cell) =>
          effectiveCellType(cell) === "code" &&
          cell.id &&
          ((Array.isArray(cell.outputs) && cell.outputs.length > 0) ||
            cell.execution_count != null)
      )
      .map((cell) => ({
        cellId: cell.id,
        source: sourceOf(cell),
        executionCount: cell.execution_count == null ? null : cell.execution_count,
        outputs: Array.isArray(cell.outputs) ? cell.outputs : [],
      }));
    savingSession = true;
    saveSessionButton.disabled = true;
    saveSessionButton.textContent = t("notebooks.editor.saving");
    try {
      await http.put(`/api/notebooks/${notebookId}/session`, {
        cells: payload,
      });
      sessionDirty = false;
      toast(t("notebooks.editor.sessionSaved"), "success");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      savingSession = false;
      saveSessionButton.innerHTML = `${icons.saveSession}${escapeHtml(
        t("notebooks.editor.saveSession")
      )}`;
      updateDirtyState();
    }
  }

  async function saveDatasetSelection() {
    if (!datasetSelect) return;
    if (!notebookId) {
      toast(t("notebooks.editor.saveNotebookFirst"), "info");
      datasetSelect.value = notebookDatasetId || "";
      return;
    }
    const previous = notebookDatasetId;
    const next = datasetSelect.value || null;
    if (next === previous) return;
    savingDataset = true;
    syncToolbar();
    try {
      const saved = await http.put(`/api/notebooks/${notebookId}/dataset`, {
        datasetId: next,
      });
      notebookDatasetId = saved.datasetId || null;
      toast(t("notebooks.editor.datasetSaved"), "success");
    } catch (error) {
      notebookDatasetId = previous;
      toast(error.message, "error");
    } finally {
      savingDataset = false;
      datasetSelect.innerHTML = datasetOptions();
      datasetSelect.value = notebookDatasetId || "";
      syncToolbar();
    }
  }

  function onKeydown(event) {
    const target = event.target;
    const editing =
      target instanceof HTMLElement &&
      target.matches("input, textarea, select, [contenteditable='true']");
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();

    if (mod && key === "s") {
      event.preventDefault();
      save();
      return;
    }
    if (editing) return;

    if (mod && event.shiftKey && key === "arrowup") {
      event.preventDefault();
      moveCell(activeIndex, -1);
      return;
    }
    if (mod && event.shiftKey && key === "arrowdown") {
      event.preventDefault();
      moveCell(activeIndex, 1);
      return;
    }
    if (event.altKey || mod) return;

    if (key === "a") {
      event.preventDefault();
      insertCellAt(activeIndex, "code");
    } else if (key === "b") {
      event.preventDefault();
      insertCellAt(activeIndex + 1, "code");
    } else if (key === "d") {
      event.preventDefault();
      const now = Date.now();
      if (now - lastDeleteAt < 800) {
        lastDeleteAt = 0;
        deleteCell(activeIndex);
      } else {
        lastDeleteAt = now;
      }
    }
  }

  root.addEventListener("keydown", onKeydown);

  saveButton.addEventListener("click", save);
  saveSessionButton.addEventListener("click", saveSession);
  if (datasetSelect) {
    datasetSelect.addEventListener("change", () => {
      void saveDatasetSelection();
    });
  }
  backLink.addEventListener("click", async (event) => {
    event.preventDefault();
    if (hasUnsaved()) {
      const leave = await confirmDialog({
        title: t("notebooks.editor.discardTitle"),
        message: t("notebooks.editor.discardText"),
        confirmText: t("notebooks.editor.discard"),
        cancelText: t("common.cancel"),
        danger: true,
      });
      if (!leave) return;
    }
    ctx.navigate("/notebooks");
  });

  const beforeUnload = (event) => {
    if (!hasUnsaved()) return;
    event.preventDefault();
    event.returnValue = "";
  };
  window.addEventListener("beforeunload", beforeUnload);

  updateDirtyState();
  renderCells();
  renderTitle();
  void refreshRunState();

  return () => {
    disposed = true;
    destroyEditor();
    hideTooltip();
    tooltip.remove();
    root.removeEventListener("keydown", onKeydown);
    root.removeEventListener("keydown", handleCellTypeKeydown);
    root.removeEventListener("mouseover", handleTooltipOver);
    root.removeEventListener("mouseout", handleTooltipOut);
    root.removeEventListener("focusin", handleTooltipFocusIn);
    root.removeEventListener("focusout", handleTooltipFocusOut);
    root.removeEventListener("pointerdown", handleTooltipPointerDown);
    document.removeEventListener("click", handleDocumentClick);
    document.removeEventListener("keydown", handleTooltipKeydown);
    window.removeEventListener("scroll", hideTooltip, true);
    window.removeEventListener("resize", hideTooltip);
    window.removeEventListener("beforeunload", beforeUnload);
    closeRunStream();
    // 会话不随页面关闭：后台运行需要继续，且刷新后要靠它恢复状态；空闲由服务端回收。
  };
}
