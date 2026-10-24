import { getToken, http } from "../api.js";
import { getLang, t } from "../i18n.js";
import {
  copyText,
  escapeHtml,
  formatDateTime,
  icons,
  initials,
  statusBadge,
  toast,
} from "../ui.js";

function maskToken(token) {
  if (!token) return "";
  if (token.length <= 14) return token;
  return `${token.slice(0, 8)}…${token.slice(-4)}`;
}

export async function renderAccount(root, ctx) {
  let disposed = false;
  let customer = null;
  try {
    customer = await http.get("/api/auth/me");
  } catch (error) {
    root.innerHTML = `<div class="panel"><div class="empty"><div class="empty-title">${escapeHtml(
      t("errors.loadFailed", { message: error.message })
    )}</div></div></div>`;
    return () => {
      disposed = true;
    };
  }
  if (disposed) return () => {};

  const token = getToken();
  let revealed = false;

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">${escapeHtml(t("account.title"))}</h1>
        <p class="page-sub">${escapeHtml(t("account.subtitle"))}</p>
      </div>
    </div>
    <div class="account-grid">
      <div style="display:flex;flex-direction:column;gap:20px;min-width:0">
        <section class="panel">
          <div class="account-profile">
            <div class="account-avatar">${escapeHtml(initials(customer.name))}</div>
            <div style="min-width:0">
              <div class="account-name">${escapeHtml(customer.name)}</div>
              <div class="account-id">${escapeHtml(customer.id)}</div>
            </div>
            <div style="margin-left:auto">${statusBadge(customer.status)}</div>
          </div>
          <div class="meta-section">
            <dl class="kv">
              <dt>${escapeHtml(t("account.created"))}</dt>
              <dd>${escapeHtml(formatDateTime(customer.createdAt))}</dd>
              <dt>${escapeHtml(t("account.lastSeen"))}</dt>
              <dd>${escapeHtml(customer.lastSeenAt ? formatDateTime(customer.lastSeenAt) : "—")}</dd>
            </dl>
          </div>
        </section>

        <section class="panel">
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("account.token"))}</div>
            <div class="token-box">
              <span class="value" id="token-value">${escapeHtml(maskToken(token))}</span>
              <button type="button" class="icon-btn" id="token-reveal" title="${escapeHtml(
                t("account.reveal")
              )}" aria-label="${escapeHtml(t("account.reveal"))}">${icons.eye}</button>
              <button type="button" class="icon-btn" id="token-copy" title="${escapeHtml(
                t("common.copy")
              )}" aria-label="${escapeHtml(t("common.copy"))}">${icons.copy}</button>
            </div>
            <div class="meta-empty" style="margin-top:8px">${escapeHtml(
              t("account.tokenHint")
            )}</div>
          </div>
        </section>

        <section class="panel">
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("account.contract.title"))}</div>
            <div class="meta-empty">${escapeHtml(t("account.contract.text"))}</div>
            <pre class="contract-code">import os

dataset_dir = os.environ["WMG_DATASET_DIR"]
output_dir  = os.environ["WMG_OUTPUT_DIR"]

# artifacts written here become downloadable
open(os.path.join(output_dir, "model.pt"), "wb")</pre>
            <dl class="kv" style="margin-top:12px">
              <dt class="mono">WMG_DATASET_DIR</dt>
              <dd>${escapeHtml(t("account.contract.envDataset"))}</dd>
              <dt class="mono">WMG_OUTPUT_DIR</dt>
              <dd>${escapeHtml(t("account.contract.envOutput"))}</dd>
              <dt class="mono">WMG_PARAMS</dt>
              <dd>${escapeHtml(t("account.contract.envParams"))}</dd>
              <dt class="mono">WMG_RUN_ID</dt>
              <dd>${escapeHtml(t("account.contract.envRun"))}</dd>
              <dt class="mono">WMG_WORK_DIR</dt>
              <dd>${escapeHtml(t("account.contract.envWork"))}</dd>
            </dl>
          </div>
        </section>
      </div>

      <div style="display:flex;flex-direction:column;gap:20px;min-width:0">
        <section class="panel">
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("account.preferences"))}</div>
            <div class="settings-row">
              <span class="label">${escapeHtml(t("account.language"))}</span>
              <div class="controls segmented" id="lang-seg">
                <button type="button" data-lang="zh" class="${getLang() === "zh" ? "active" : ""}">中文</button>
                <button type="button" data-lang="en" class="${getLang() === "en" ? "active" : ""}">English</button>
              </div>
            </div>
            <div class="settings-row">
              <span class="label">${escapeHtml(t("account.theme"))}</span>
              <div class="controls segmented" id="theme-seg">
                <button type="button" data-theme="light" class="${ctx.theme === "light" ? "active" : ""}">${escapeHtml(
                  t("account.themeLight")
                )}</button>
                <button type="button" data-theme="dark" class="${ctx.theme === "dark" ? "active" : ""}">${escapeHtml(
                  t("account.themeDark")
                )}</button>
              </div>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="meta-section">
            <div class="meta-title">${escapeHtml(t("account.session"))}</div>
            <div class="meta-empty" style="margin-bottom:12px">${escapeHtml(
              t("account.logoutHint")
            )}</div>
            <button type="button" class="btn btn-danger" id="logout-btn">${icons.logout}${escapeHtml(
              t("account.logout")
            )}</button>
          </div>
        </section>
      </div>
    </div>`;

  const tokenValue = root.querySelector("#token-value");
  const tokenReveal = root.querySelector("#token-reveal");

  tokenReveal.addEventListener("click", () => {
    revealed = !revealed;
    tokenValue.textContent = revealed ? token : maskToken(token);
    tokenReveal.innerHTML = revealed ? icons.eyeOff : icons.eye;
    tokenReveal.setAttribute("aria-label", revealed ? t("account.hide") : t("account.reveal"));
  });

  root.querySelector("#token-copy").addEventListener("click", async () => {
    const copied = await copyText(token);
    toast(copied ? t("toast.copied") : t("account.errors.copyFailed"), copied ? "success" : "error");
  });

  root.querySelectorAll("#lang-seg [data-lang]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.lang !== getLang()) ctx.setLang(button.dataset.lang);
    });
  });
  root.querySelectorAll("#theme-seg [data-theme]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.theme !== ctx.theme) ctx.setTheme(button.dataset.theme);
    });
  });

  root.querySelector("#logout-btn").addEventListener("click", () => ctx.logout());

  return () => {
    disposed = true;
  };
}
