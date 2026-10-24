import { getToken, http, setToken } from "./api.js";
import { getLang, setLang, t } from "./i18n.js";
import { escapeHtml, icons, initials, logoMark, toast } from "./ui.js";
import { renderAccount } from "./pages/account.js";
import { renderActivity } from "./pages/activity.js";
import { renderDatasets } from "./pages/datasets.js";
import { renderLogin } from "./pages/login.js";
import { renderNotebooks } from "./pages/notebooks.js";
import { renderOverview } from "./pages/overview.js";
import { renderRunDetail } from "./pages/run-detail.js";
import { renderRuns } from "./pages/runs.js";

const NAV_ITEMS = [
  { name: "overview", path: "/overview", icon: "overview", label: "nav.overview" },
  { name: "datasets", path: "/datasets", icon: "datasets", label: "nav.datasets" },
  { name: "notebooks", path: "/notebooks", icon: "notebooks", label: "nav.notebooks" },
  { name: "runs", path: "/runs", icon: "runs", label: "nav.runs" },
  { name: "activity", path: "/activity", icon: "activity", label: "nav.activity" },
  { name: "account", path: "/account", icon: "account", label: "nav.account" },
];

const ROUTES = [
  { name: "overview", re: /^\/overview\/?$/, loader: renderOverview, label: "nav.overview" },
  { name: "datasets", re: /^\/datasets\/?$/, loader: renderDatasets, label: "nav.datasets" },
  { name: "notebooks", re: /^\/notebooks\/?$/, loader: renderNotebooks, label: "nav.notebooks" },
  { name: "runs", re: /^\/runs\/?$/, loader: renderRuns, label: "nav.runs" },
  {
    name: "run-detail",
    re: /^\/runs\/([A-Za-z0-9_-]+)\/?$/,
    loader: renderRunDetail,
    label: "nav.runs",
    param: "id",
  },
  { name: "activity", re: /^\/activity\/?$/, loader: renderActivity, label: "nav.activity" },
  { name: "account", re: /^\/account\/?$/, loader: renderAccount, label: "nav.account" },
];

const THEME_KEY = "trainhub.theme";

const state = {
  customer: null,
  theme: localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light",
  runtime: { available: false, kernels: [], defaultKernel: null, loaded: false },
};

const app = document.getElementById("app");
let activeCleanup = null;
let routeVersion = 0;
let activeNavName = "overview";

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  const themeButton = document.getElementById("theme-toggle");
  if (themeButton) {
    themeButton.innerHTML = state.theme === "dark" ? icons.sun : icons.moon;
    themeButton.setAttribute("aria-label", t("aria.theme"));
    themeButton.setAttribute("title", t("aria.theme"));
  }
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  const [path, queryString] = raw.split("?");
  return { path: path || "", query: new URLSearchParams(queryString || "") };
}

function matchRoute(path) {
  for (const route of ROUTES) {
    const match = route.re.exec(path || "/overview");
    if (match) {
      const params = {};
      if (route.param) params[route.param] = match[1];
      return { ...route, params };
    }
  }
  return null;
}

function navigate(path) {
  const target = `#${path}`;
  if (location.hash === target) scheduleRoute();
  else location.hash = target;
}

let routeQueued = false;
function scheduleRoute() {
  if (routeQueued) return;
  routeQueued = true;
  queueMicrotask(() => {
    routeQueued = false;
    handleRoute();
  });
}

function cleanupPage() {
  if (activeCleanup) {
    try {
      activeCleanup();
    } catch {
      /* page cleanup must never break navigation */
    }
    activeCleanup = null;
  }
}

function updateChrome(route) {
  activeNavName = route.name === "run-detail" ? "runs" : route.name;
  document.querySelectorAll("[data-nav]").forEach((item) => {
    item.classList.toggle("active", item.dataset.nav === activeNavName);
    if (item.dataset.nav === activeNavName) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const crumb = document.getElementById("crumb-current");
  if (crumb) crumb.textContent = t(`nav.${activeNavName}`);
  document.title = `${t(`nav.${activeNavName}`)} · TrainHub`;
}

async function handleRoute() {
  if (!state.customer) {
    renderLoginView();
    return;
  }
  const { path, query } = parseHash();
  const route = matchRoute(path);
  if (!route) {
    navigate("/overview");
    return;
  }
  cleanupPage();
  updateChrome(route);
  const view = document.getElementById("view");
  if (!view) return;
  view.innerHTML = `<div class="loading">${escapeHtml(t("common.loading"))}</div>`;
  window.scrollTo({ top: 0 });
  closeMobileMenu();

  const version = ++routeVersion;
  const ctx = {
    navigate,
    params: route.params,
    query,
    theme: state.theme,
    setLang: switchLang,
    setTheme: switchTheme,
    logout,
  };
  try {
    const cleanup = await route.loader(view, ctx);
    if (version !== routeVersion) {
      if (typeof cleanup === "function") cleanup();
      return;
    }
    activeCleanup = typeof cleanup === "function" ? cleanup : null;
  } catch (error) {
    if (version !== routeVersion) return;
    view.innerHTML = `<div class="panel"><div class="empty">
      <div class="empty-title">${escapeHtml(
        t("errors.loadFailed", { message: error.message })
      )}</div>
      <button type="button" class="btn btn-secondary" id="route-retry">${escapeHtml(
        t("common.retry")
      )}</button>
    </div></div>`;
    view.querySelector("#route-retry")?.addEventListener("click", scheduleRoute);
    toast(error.message, "error");
  }
}

/* ---------------------------------------------------------------- shell */

function renderShell() {
  const switchLabel = getLang() === "zh" ? "EN" : "中";
  const activeRoute = matchRoute(parseHash().path) || { name: "overview" };
  activeNavName = activeRoute.name === "run-detail" ? "runs" : activeRoute.name;
  const navHtml = NAV_ITEMS.map(
    (item) => `<a class="nav-item ${item.name === activeNavName ? "active" : ""}"
        href="#${item.path}" data-nav="${item.name}">
        ${icons[item.icon]}<span>${escapeHtml(t(item.label))}</span>
      </a>`
  ).join("");

  app.innerHTML = `
    <div class="shell">
      <aside class="sidebar" id="sidebar">
        <div class="brand">
          <div class="brand-mark">${logoMark}</div>
          <div class="brand-text">
            <span class="brand-name">TrainHub</span>
            <span class="brand-tagline">${escapeHtml(t("brand.tagline"))}</span>
          </div>
        </div>
        <nav class="nav" aria-label="TrainHub">${navHtml}</nav>
        <div class="sidebar-foot">
          <div class="runtime-card">
            <span class="runtime-line">
              <span class="runtime-dot" id="runtime-dot"></span>
              <span id="runtime-text">${escapeHtml(t("common.loading"))}</span>
            </span>
          </div>
          <div class="sidebar-version">TrainHub v0.1.0</div>
        </div>
      </aside>
      <div class="sidebar-backdrop" id="sidebar-backdrop"></div>
      <div class="main">
        <header class="topbar">
          <button type="button" class="icon-btn menu-toggle" id="menu-toggle"
            aria-label="${escapeHtml(t("aria.menu"))}" title="${escapeHtml(t("aria.menu"))}">
            ${icons.menu}
          </button>
          <div class="crumbs">
            <span class="crumb-home">TrainHub</span>
            <span class="sep">/</span>
            <strong id="crumb-current">${escapeHtml(t(`nav.${activeNavName}`))}</strong>
          </div>
          <div class="topbar-spacer"></div>
          <div class="topbar-tools">
            <span class="kernel-chip ${state.runtime.available ? "" : "off"}" id="kernel-chip">
              <span class="dot"></span>
              <span id="kernel-chip-text">Jupyter</span>
            </span>
            <button type="button" class="icon-btn lang-btn" id="lang-toggle"
              aria-label="${escapeHtml(t("aria.language"))}" title="${escapeHtml(t("aria.language"))}">
              ${icons.globe}<span>${switchLabel}</span>
            </button>
            <button type="button" class="icon-btn" id="theme-toggle"
              aria-label="${escapeHtml(t("aria.theme"))}" title="${escapeHtml(t("aria.theme"))}">
              ${state.theme === "dark" ? icons.sun : icons.moon}
            </button>
            <a class="avatar" href="#/account" id="account-link"
              title="${escapeHtml(state.customer ? state.customer.name : "")}"
              aria-label="${escapeHtml(t("aria.account"))}">
              ${escapeHtml(initials(state.customer ? state.customer.name : ""))}
            </a>
          </div>
        </header>
        <main id="view" class="view" tabindex="-1"></main>
      </div>
    </div>`;

  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  document.getElementById("menu-toggle").addEventListener("click", () => {
    sidebar.classList.add("open");
    backdrop.classList.add("show");
  });
  backdrop.addEventListener("click", closeMobileMenu);
  sidebar.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", closeMobileMenu);
  });
  document.getElementById("lang-toggle").addEventListener("click", () =>
    switchLang(getLang() === "zh" ? "en" : "zh")
  );
  document.getElementById("theme-toggle").addEventListener("click", () =>
    switchTheme(state.theme === "dark" ? "light" : "dark")
  );

  updateRuntimeUI();
  applyTheme();
}

function closeMobileMenu() {
  document.getElementById("sidebar")?.classList.remove("open");
  document.getElementById("sidebar-backdrop")?.classList.remove("show");
}

async function loadRuntimeStatus() {
  try {
    const info = await http.get("/api/kernels");
    state.runtime = {
      available: Boolean(info.jupyterAvailable),
      kernels: info.kernels || [],
      defaultKernel: info.default,
      loaded: true,
    };
  } catch {
    state.runtime = { available: false, kernels: [], defaultKernel: null, loaded: true };
  }
  updateRuntimeUI();
}

function updateRuntimeUI() {
  const dot = document.getElementById("runtime-dot");
  const text = document.getElementById("runtime-text");
  const chip = document.getElementById("kernel-chip");
  const chipText = document.getElementById("kernel-chip-text");
  if (!dot || !text) return;
  const { available, kernels, defaultKernel } = state.runtime;
  const count = kernels.length;
  dot.className = `runtime-dot ${available ? "ok" : "bad"}`;
  text.textContent = available
    ? `${t("runtime.ready")} · ${t("runtime.kernels", { n: count })}`
    : t("runtime.missing");
  if (chip && chipText) {
    chip.classList.toggle("off", !available);
    chipText.textContent = available ? `${defaultKernel || "jupyter"} · ${count}` : t("runtime.missing");
  }
}

/* ---------------------------------------------------------------- login */

function renderLoginView() {
  cleanupPage();
  document.title = "TrainHub";
  renderLogin(app, {
    onLogin: handleLogin,
    onToggleLang: () => switchLang(getLang() === "zh" ? "en" : "zh"),
    onToggleTheme: () => switchTheme(state.theme === "dark" ? "light" : "dark"),
    theme: state.theme,
  });
}

async function handleLogin(token) {
  setToken(token);
  let me;
  try {
    me = await http.get("/api/auth/me", { silent: true });
  } catch (error) {
    setToken("");
    throw error;
  }
  state.customer = me;
  renderShell();
  await loadRuntimeStatus();
  const { path } = parseHash();
  if (!path || path === "/login") navigate("/overview");
  else scheduleRoute();
  toast(`${t("toast.loggedIn")} · ${me.name}`, "success");
}

/* ---------------------------------------------------------------- settings */

function switchTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  localStorage.setItem(THEME_KEY, state.theme);
  applyTheme();
  if (state.customer) {
    renderShell();
    scheduleRoute();
  } else {
    renderLoginView();
  }
}

function switchLang(lang) {
  setLang(lang);
  if (state.customer) {
    renderShell();
    loadRuntimeStatus();
    scheduleRoute();
  } else {
    renderLoginView();
  }
}

function logout() {
  cleanupPage();
  state.customer = null;
  setToken("");
  toast(t("toast.loggedOut"), "info");
  renderLoginView();
}

window.addEventListener("trainhub:unauthorized", () => {
  if (!state.customer) return;
  cleanupPage();
  state.customer = null;
  setToken("");
  renderLoginView();
  toast(t("errors.unauthorized"), "error");
});

window.addEventListener("hashchange", scheduleRoute);

/* ---------------------------------------------------------------- boot */

async function boot() {
  setLang(getLang());
  applyTheme();
  if (!getToken()) {
    renderLoginView();
    return;
  }
  try {
    state.customer = await http.get("/api/auth/me", { silent: true });
  } catch {
    setToken("");
    renderLoginView();
    return;
  }
  renderShell();
  await loadRuntimeStatus();
  const { path } = parseHash();
  if (!path) navigate("/overview");
  else handleRoute();
}

boot();
