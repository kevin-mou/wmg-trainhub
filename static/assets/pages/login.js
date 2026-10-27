import { getLang, t } from "../i18n.js";
import { escapeHtml, icons, logoMark } from "../ui.js";

const REMEMBER_COOKIE = "trainhub_remembered_token";
const REMEMBER_MAX_AGE = 60 * 60 * 24 * 30;

function readCookie(name) {
  try {
    const prefix = `${name}=`;
    const cookies = String(document.cookie || "");
    for (const part of cookies.split(";")) {
      const item = part.trim();
      if (item.startsWith(prefix)) {
        return decodeURIComponent(item.slice(prefix.length));
      }
    }
  } catch {
    /* Cookie 不可用时按“未记住”处理 */
  }
  return "";
}

function writeCookie(name, value, maxAgeSeconds) {
  try {
    const secure =
      typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${maxAgeSeconds}; SameSite=Strict${secure}`;
  } catch {
    /* Cookie 不可用时静默降级，不影响登录 */
  }
}

export function renderLogin(root, { onLogin, onToggleLang, onToggleTheme, theme }) {
  const switchLabel = getLang() === "zh" ? "EN" : "中";
  const rememberedToken = readCookie(REMEMBER_COOKIE);
  root.innerHTML = `
    <div class="login-page">
      <section class="login-brand" aria-label="TrainHub">
        <div class="login-brand-top">
          <div class="brand-mark">${logoMark}</div>
          <div class="brand-text">
            <span class="brand-name">TrainHub</span>
            <span class="brand-tagline">${escapeHtml(t("brand.tagline"))}</span>
          </div>
        </div>
        <div class="login-hero">
          <h1>${escapeHtml(t("login.heroTitle"))}</h1>
          <p>${escapeHtml(t("login.heroSubtitle"))}</p>
          <div class="login-terminal" aria-hidden="true">
            <div><span class="t-dim">$</span> trainhub run <span class="t-brass">tr_9f2c…</span></div>
            <div class="t-dim">[12:04:18] kernel python3 · dataset mnist</div>
            <div>[12:04:21] ── cell [3] ──</div>
            <div>epoch 1/3&#160;&#160;loss 0.421&#160;&#160;acc 0.882</div>
            <div>epoch 2/3&#160;&#160;loss 0.310&#160;&#160;acc 0.914</div>
            <div>epoch 3/3&#160;&#160;loss 0.244&#160;&#160;acc 0.937</div>
            <div class="t-ok">✓ artifacts → output/model.pt</div>
          </div>
        </div>
        <div class="login-brand-foot">
          <span>${escapeHtml(t("login.featureSingle"))}</span>
          <span>${escapeHtml(t("login.featureJupyter"))}</span>
          <span>${escapeHtml(t("login.featureLogs"))}</span>
        </div>
      </section>

      <section class="login-side">
        <div class="login-tools">
          <button type="button" class="icon-btn lang-btn" id="login-lang" aria-label="${escapeHtml(
            t("login.langAria")
          )}" title="${escapeHtml(t("login.langAria"))}">
            ${icons.globe}<span>${switchLabel}</span>
          </button>
          <button type="button" class="icon-btn" id="login-theme" aria-label="${escapeHtml(
            t("login.themeAria")
          )}" title="${escapeHtml(t("login.themeAria"))}">
            ${theme === "dark" ? icons.sun : icons.moon}
          </button>
        </div>

        <div class="login-card-wrap">
          <div class="login-card">
            <h2>${escapeHtml(t("login.title"))}</h2>
            <p class="login-sub">${escapeHtml(t("login.subtitle"))}</p>
            <p class="login-error" id="login-error" role="alert"></p>
            <form id="login-form" autocomplete="off" novalidate>
              <div class="field">
                <label for="login-token">${escapeHtml(t("login.tokenLabel"))}</label>
                <div class="token-field">
                  <input class="input" id="login-token" type="password" spellcheck="false"
                    autocomplete="off" value="${escapeHtml(rememberedToken)}"
                    placeholder="${escapeHtml(t("login.tokenPlaceholder"))}" />
                  <button type="button" class="icon-btn" id="login-reveal" aria-label="${escapeHtml(
                    t("account.reveal")
                  )}">${icons.eye}</button>
                </div>
                <span class="hint">${escapeHtml(t("login.tokenHint"))}</span>
                <label class="remember-row">
                  <input type="checkbox" id="login-remember" checked
                    aria-describedby="login-remember-hint" />
                  <span>${escapeHtml(t("login.remember"))}</span>
                </label>
                <span class="hint" id="login-remember-hint">${escapeHtml(
                  t("login.rememberHint")
                )}</span>
              </div>
              <button type="submit" class="btn btn-primary login-submit" id="login-submit">
                ${escapeHtml(t("login.submit"))}
              </button>
            </form>
            <div class="login-note">${icons.info}<span>${escapeHtml(t("login.note"))}</span></div>
          </div>
        </div>
      </section>
    </div>`;

  const form = root.querySelector("#login-form");
  const input = root.querySelector("#login-token");
  const error = root.querySelector("#login-error");
  const submit = root.querySelector("#login-submit");
  const reveal = root.querySelector("#login-reveal");
  const rememberInput = root.querySelector("#login-remember");

  const showError = (message) => {
    error.textContent = message || "";
    error.classList.toggle("show", Boolean(message));
  };

  reveal.addEventListener("click", () => {
    const isPassword = input.type === "password";
    input.type = isPassword ? "text" : "password";
    reveal.innerHTML = isPassword ? icons.eyeOff : icons.eye;
    input.focus();
  });

  root.querySelector("#login-lang").addEventListener("click", onToggleLang);
  root.querySelector("#login-theme").addEventListener("click", onToggleTheme);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = input.value.trim();
    if (!token) {
      showError(t("login.errorEmpty"));
      input.focus();
      return;
    }
    showError("");
    submit.disabled = true;
    submit.textContent = t("login.submitting");
    const remember = Boolean(rememberInput?.checked);
    if (!remember) writeCookie(REMEMBER_COOKIE, "", 0);
    try {
      await onLogin(token);
      if (remember) writeCookie(REMEMBER_COOKIE, token, REMEMBER_MAX_AGE);
    } catch (err) {
      showError(err && err.message ? err.message : t("common.error"));
      submit.disabled = false;
      submit.textContent = t("login.submit");
      input.focus();
      input.select();
    }
  });

  setTimeout(() => input.focus(), 40);
}
