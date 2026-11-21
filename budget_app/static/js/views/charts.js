// Thin wrapper around Chart.js (loaded from CDN). Charts are skipped if it failed to load.
export const palette = ["#4f5bd5", "#22a06b", "#e8833a", "#d6457a", "#2f9fd0", "#9b5de5", "#c9a227", "#6b7288",
  "#e05d5d", "#3fb8af", "#8d6e63", "#7cb342"];

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
