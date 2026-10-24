#!/usr/bin/env node
/**
 * TrainHub 前端静态自检：
 * 1. 所有静态 import 都能解析到文件
 * 2. 页面里用到的 i18n 键在中英文两套字典里都存在
 * 3. index.html / styles.css 引用的本地资源存在
 *
 * 用法：node scripts/check-frontend.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const assets = path.join(root, "static", "assets");
const pagesDir = path.join(assets, "pages");
const files = [
  "app.js",
  "api.js",
  "ui.js",
  "i18n.js",
  ...fs
    .readdirSync(pagesDir)
    .filter((name) => name.endsWith(".js"))
    .sort()
    .map((name) => `pages/${name}`),
];

globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.document = { documentElement: {} };

const i18n = await import(pathToFileURL(path.join(assets, "i18n.js")).href);

const failureList = [];
const keys = new Set(["activity.title"]);
const dynamicPrefixes = {
  "status.": ["queued", "running", "succeeded", "failed", "cancelled"],
  "runs.filters.": ["all", "queued", "running", "succeeded", "failed", "cancelled"],
  "nav.": ["overview", "datasets", "notebooks", "runs", "activity", "account"],
};

for (const relative of files) {
  const file = path.join(assets, relative);
  const source = fs.readFileSync(file, "utf8");
  for (const match of source.matchAll(/\bt\(\s*"([^"]+)"/g)) keys.add(match[1]);
  for (const match of source.matchAll(/\bt\(\s*'([^']+)'/g)) keys.add(match[1]);
  for (const match of source.matchAll(/\bt\(\s*`([^`$]+)`/g)) keys.add(match[1]);
  for (const match of source.matchAll(/\bt\(\s*`([A-Za-z0-9_.]+)\$\{/g)) {
    const values = dynamicPrefixes[match[1]];
    if (values) for (const value of values) keys.add(match[1] + value);
  }
  for (const match of source.matchAll(/"(activity\.action\.[A-Za-z0-9]+)"/g)) {
    keys.add(match[1]);
  }
  for (const match of source.matchAll(/"(nav\.[a-z]+)"/g)) keys.add(match[1]);
  for (const match of source.matchAll(/from\s+"(\.\.?\/[^"]+)"/g)) {
    const target = path.resolve(path.dirname(file), match[1]);
    if (!fs.existsSync(target)) {
      failureList.push(`${relative}: 无法解析 import "${match[1]}"`);
    }
  }
}

for (const lang of ["zh", "en"]) {
  i18n.setLang(lang);
  for (const key of [...keys].sort()) {
    if (i18n.t(key) === key) {
      failureList.push(`${lang}: 缺少 i18n 键 "${key}"`);
    }
  }
}

const indexHtml = fs.readFileSync(path.join(root, "static", "index.html"), "utf8");
for (const match of indexHtml.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)) {
  if (!fs.existsSync(path.join(root, "static", match[1]))) {
    failureList.push(`index.html: 缺少资源 ${match[1]}`);
  }
}

const css = fs.readFileSync(path.join(assets, "styles.css"), "utf8");
for (const match of css.matchAll(/url\("(\/assets\/[^"]+)"\)/g)) {
  if (!fs.existsSync(path.join(root, "static", match[1]))) {
    failureList.push(`styles.css: 缺少资源 ${match[1]}`);
  }
}

if (failureList.length) {
  console.error("前端自检失败：");
  for (const failure of failureList) console.error(` - ${failure}`);
  process.exit(1);
}

console.log(
  `前端自检通过：${files.length} 个模块，${keys.size} 个 i18n 键 × 2 种语言。`
);
