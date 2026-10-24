import { t } from "./i18n.js";

const TOKEN_KEY = "trainhub.token";

export class ApiError extends Error {
  constructor(message, { status = 0, code = 0 } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function notifyUnauthorized() {
  window.dispatchEvent(new CustomEvent("trainhub:unauthorized"));
}

async function parseEnvelope(response, { silent = false } = {}) {
  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    if (response.status === 401 && !silent) notifyUnauthorized();
    const message =
      payload && payload.message
        ? payload.message
        : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, {
      status: response.status,
      code: payload && payload.code != null ? payload.code : response.status,
    });
  }
  return payload;
}

export async function api(path, { method = "GET", body, silent = false } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: {
        Accept: "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...authHeaders(),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(t("errors.network"), { status: 0 });
  }
  const payload = await parseEnvelope(response, { silent });
  return payload ? payload.data : null;
}

export const http = {
  get: (path, options = {}) => api(path, { ...options, method: "GET" }),
  post: (path, body, options = {}) => api(path, { ...options, method: "POST", body }),
  del: (path, options = {}) => api(path, { ...options, method: "DELETE" }),
};

function filenameFromDisposition(header, fallback) {
  if (!header) return fallback;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      return fallback;
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : fallback;
}

export async function download(path, fallbackName) {
  let response;
  try {
    response = await fetch(path, { headers: { ...authHeaders() } });
  } catch {
    throw new ApiError(t("errors.network"), { status: 0 });
  }
  if (!response.ok) {
    await parseEnvelope(response);
    throw new ApiError(`请求失败（HTTP ${response.status}）`, { status: response.status });
  }
  const blob = await response.blob();
  const filename = filenameFromDisposition(
    response.headers.get("content-disposition"),
    fallbackName
  );
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "download";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  return filename;
}

export function upload(path, formData, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.responseType = "text";
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onerror = () => reject(new ApiError(t("errors.network"), { status: 0 }));
    xhr.onload = () => {
      let payload = null;
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        payload = null;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        if (xhr.status === 401) notifyUnauthorized();
        reject(
          new ApiError(
            payload && payload.message ? payload.message : `请求失败（HTTP ${xhr.status}）`,
            { status: xhr.status, code: payload && payload.code }
          )
        );
        return;
      }
      resolve(payload ? payload.data : null);
    };
    xhr.send(formData);
  });
}
