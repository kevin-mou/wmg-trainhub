import { escapeHtml } from "./ui.js";

const WIDTH = 960;
const HEIGHT = 260;
const PAD = { top: 16, right: 16, bottom: 30, left: 54 };
const COLOR_COUNT = 8;

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function formatValue(value) {
  if (!finite(value)) return "—";
  const abs = Math.abs(value);
  if (abs === 0) return "0";
  if (abs >= 1_000_000 || abs < 0.001) return value.toExponential(1);
  if (abs >= 1000) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function axisBounds(min, max) {
  if (min === max) {
    const pad = Math.max(0.5, Math.abs(min) * 0.05);
    return [min - pad, max + pad];
  }
  const pad = (max - min) * 0.08;
  return [min - pad, max + pad];
}

export function renderMetricsChart(container, series, { label = "metrics" } = {}) {
  if (!container) return;
  const usable = (Array.isArray(series) ? series : []).filter(
    (item) => item && Array.isArray(item.points) && item.points.length
  );
  if (!usable.length) {
    container.innerHTML = "";
    return;
  }

  let xMin = Infinity;
  let xMax = -Infinity;
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const item of usable) {
    for (const point of item.points) {
      if (!Array.isArray(point) || point.length < 2) continue;
      const [x, y] = point;
      if (!finite(x) || !finite(y)) continue;
      if (x < xMin) xMin = x;
      if (x > xMax) xMax = x;
      if (y < yMin) yMin = y;
      if (y > yMax) yMax = y;
    }
  }
  if (!finite(xMin) || !finite(yMin)) {
    container.innerHTML = "";
    return;
  }
  [xMin, xMax] = axisBounds(xMin, xMax);
  [yMin, yMax] = axisBounds(yMin, yMax);

  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;
  const sx = (value) => PAD.left + ((value - xMin) / (xMax - xMin)) * plotWidth;
  const sy = (value) => PAD.top + (1 - (value - yMin) / (yMax - yMin)) * plotHeight;

  const grid = [];
  for (let index = 0; index <= 4; index += 1) {
    const value = yMin + ((yMax - yMin) * index) / 4;
    const y = sy(value);
    grid.push(
      `<line class="metrics-grid-line" x1="${PAD.left}" y1="${y.toFixed(1)}" x2="${(
        WIDTH - PAD.right
      ).toFixed(1)}" y2="${y.toFixed(1)}" />`
    );
    grid.push(
      `<text class="metrics-axis-text" x="${PAD.left - 8}" y="${(y + 3.5).toFixed(
        1
      )}" text-anchor="end">${escapeHtml(formatValue(value))}</text>`
    );
  }
  for (let index = 0; index <= 4; index += 1) {
    const value = xMin + ((xMax - xMin) * index) / 4;
    const x = sx(value);
    grid.push(
      `<line class="metrics-grid-line metrics-grid-line-v" x1="${x.toFixed(1)}" y1="${
        PAD.top
      }" x2="${x.toFixed(1)}" y2="${(HEIGHT - PAD.bottom).toFixed(1)}" />`
    );
    grid.push(
      `<text class="metrics-axis-text" x="${x.toFixed(1)}" y="${(
        HEIGHT - PAD.bottom + 18
      ).toFixed(1)}" text-anchor="middle">${escapeHtml(formatValue(value))}</text>`
    );
  }

  const lines = [];
  const legend = [];
  usable.forEach((item, index) => {
    const color = `var(--chart-${(index % COLOR_COUNT) + 1})`;
    const points = item.points.filter(
      (point) => Array.isArray(point) && point.length >= 2 && finite(point[0]) && finite(point[1])
    );
    if (!points.length) return;
    const polyline = points
      .map(([x, y]) => `${sx(x).toFixed(2)},${sy(y).toFixed(2)}`)
      .join(" ");
    lines.push(
      `<polyline class="metrics-line" points="${polyline}" style="stroke:${color}" ` +
        `fill="none" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`
    );
    if (points.length === 1) {
      const [x, y] = points[0];
      lines.push(
        `<circle class="metrics-point" cx="${sx(x).toFixed(2)}" cy="${sy(y).toFixed(2)}" ` +
          `r="3.2" style="fill:${color}" />`
      );
    }
    const last = points[points.length - 1][1];
    legend.push(
      `<span class="metrics-legend-item">` +
        `<span class="metrics-swatch" style="background:${color}"></span>` +
        `<span class="metrics-legend-name" title="${escapeHtml(item.name)}">${escapeHtml(
          item.name
        )}</span>` +
        `<span class="metrics-legend-value">${escapeHtml(formatValue(last))}</span>` +
        `</span>`
    );
  });

  container.innerHTML =
    `<div class="metrics-legend">${legend.join("")}</div>` +
    `<div class="metrics-plot" role="img" aria-label="${escapeHtml(label)}">` +
    `<svg viewBox="0 0 ${WIDTH} ${HEIGHT}" preserveAspectRatio="xMidYMid meet">` +
    `${grid.join("")}${lines.join("")}</svg></div>`;
}
