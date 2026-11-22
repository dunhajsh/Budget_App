// Thin wrapper around Chart.js (loaded from CDN). Charts are skipped if it failed to load.
export const palette = ["#4f5bd5", "#22a06b", "#e8833a", "#d6457a", "#2f9fd0", "#9b5de5", "#c9a227", "#6b7288",
  "#e05d5d", "#3fb8af", "#8d6e63", "#7cb342"];

// Category colors for series (fixed order, colorblind-checked), stepped separately for light and dark.
const SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"];
export const dark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches;
export const series = () => (dark() ? SERIES_DARK : SERIES_LIGHT);
export const otherGray = () => (dark() ? "#6f6e68" : "#a9a79f");

/** Diagonal hatch: used for money that isn't broken down further (we don't know what it was). */
export function hatch() {
  const c = document.createElement("canvas");
  c.width = c.height = 8;
  const g = c.getContext("2d");
  g.fillStyle = dark() ? "#2c2c2a" : "#ecebe6";
  g.fillRect(0, 0, 8, 8);
  g.strokeStyle = dark() ? "#6f6e68" : "#a9a79f";
  g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(-2, 10); g.lineTo(10, -2); g.moveTo(-2, 2); g.lineTo(2, -2); g.moveTo(6, 10); g.lineTo(10, 6); g.stroke();
  return g.createPattern(c, "repeat");
}

const live = {};

export function chart(id, config) {
  const canvas = document.getElementById(id);
  if (!canvas || !window.Chart) return null;
  live[id]?.destroy();
  const css = getComputedStyle(document.documentElement);
  const text = css.getPropertyValue("--muted").trim();
  const grid = css.getPropertyValue("--border").trim();
  window.Chart.defaults.color = text;
  window.Chart.defaults.font.family = "Segoe UI, system-ui, sans-serif";
  config.options = config.options || {};
  config.options.maintainAspectRatio = false;
  config.options.responsive = true;
  if (config.type !== "doughnut" && config.type !== "pie") {
    config.options.scales = config.options.scales || {};
    for (const ax of ["x", "y"]) {
      config.options.scales[ax] = { grid: { color: grid }, border: { display: false }, ...(config.options.scales[ax] || {}) };
    }
  }
  live[id] = new window.Chart(canvas, config);
  return live[id];
}
