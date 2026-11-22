import { api, esc, money, money0, monthAdd, monthLabel, state } from "../app.js";
import { chart, palette } from "./charts.js";

let year = null;
let catId = null;
let span = 24;

export default {
  async render(el, alive) {
    year = year || +state.month.slice(0, 4);
    const end = state.month;
    const [yh, trend] = await Promise.all([
      api(`/history?start=${year}-01&end=${year}-12`),
      api(`/history?start=${monthAdd(end, -(span - 1))}&end=${end}`),
    ]);
    if (!alive()) return;
    const cats = yh.categories.filter((c) => c.spent.some((v) => v) || c.allocated.some((v) => v));
    catId = catId || cats.sort((a, b) => b.spent.reduce((x, y) => x + y, 0) - a.spent.reduce((x, y) => x + y, 0))[0]?.id;
    const max = Math.max(1, ...cats.flatMap((c) => c.spent));
    const heat = (v) => (v > 0 ? `background: color-mix(in srgb, var(--accent) ${Math.round((v / max) * 55)}%, transparent)` : "");
    const ordered = [...cats].sort((a, b) => a.name.localeCompare(b.name));

    el.innerHTML = `<div class="stack">
      <div class="panel">
        <div class="panel-head"><h2>Trend</h2><span class="spacer"></span>
          <div class="chips">${[12, 24, 60].map((n) => `<span class="chip ${span === n ? "active" : ""}" data-span="${n}">${n / 12} yr</span>`).join("")}</div></div>
        <div class="chart-box" style="height:280px"><canvas id="ch-long"></canvas></div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Envelope detail</h2>
          <select id="cat-pick">${ordered.map((c) => `<option value="${c.id}" ${c.id === catId ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
          <span class="muted small">last ${span} months</span><span class="spacer"></span>
          <a class="btn small" id="full-breakdown" href="#/spending?env=${catId}">Full breakdown →</a></div>
        <div class="grid grid-2">
          <div class="chart-box"><canvas id="ch-cat"></canvas></div>
          <div><div class="chart-box" style="height:200px"><canvas id="ch-sub"></canvas></div>
            <div id="top-items" class="small" style="margin-top:10px"></div></div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>${year} by month</h2>
          <button class="small" data-year="-1">‹ ${year - 1}</button><button class="small" data-year="1">${year + 1} ›</button>
          <span class="spacer"></span><a class="btn" href="/api/export/year/${year}.xlsx">⬇ Export ${year} to Excel</a></div>
        <div class="table-wrap"><table class="matrix">
          <thead><tr><th>Spent</th>${yh.months.map((m) => `<th class="num">${monthLabel(m, true).split(" ")[0]}</th>`).join("")}<th class="num">Year</th><th class="num">Budgeted</th></tr></thead>
          <tbody>${ordered.map((c) => {
            const tot = c.spent.reduce((a, b) => a + b, 0);
            const al = c.allocated.reduce((a, b) => a + b, 0);
            return `<tr><td class="cat">${esc(c.name)}</td>${c.spent.map((v) => `<td class="num" style="${heat(v)}">${v ? money0(v) : ""}</td>`).join("")}
              <td class="num"><b>${money0(tot)}</b></td><td class="num ${tot > al + 0.5 ? "bad" : "muted"}">${money0(al)}</td></tr>`;
          }).join("")}</tbody>
          <tfoot><tr><td>Total spent</td>${yh.spent.map((v) => `<td class="num">${v ? money0(v) : ""}</td>`).join("")}
            <td class="num">${money0(yh.spent.reduce((a, b) => a + b, 0))}</td><td class="num">${money0(yh.allocated.reduce((a, b) => a + b, 0))}</td></tr>
            <tr><td>Income</td>${yh.income.map((v) => `<td class="num muted">${v ? money0(v) : ""}</td>`).join("")}
            <td class="num">${money0(yh.income.reduce((a, b) => a + b, 0))}</td><td></td></tr></tfoot>
        </table></div>
      </div>
    </div>`;

    chart("ch-long", {
      type: "line",
      data: {
        labels: trend.months.map((m) => monthLabel(m, true)),
        datasets: [
          { label: "Income", data: trend.income, borderColor: palette[1], backgroundColor: palette[1], tension: 0.3, pointRadius: 0 },
          { label: "Spent", data: trend.spent, borderColor: palette[0], backgroundColor: palette[0], tension: 0.3, pointRadius: 0 },
          { label: "Budgeted", data: trend.allocated, borderColor: palette[7], borderDash: [4, 4], tension: 0.3, pointRadius: 0 },
        ],
      },
      options: { interaction: { mode: "index", intersect: false }, scales: { y: { ticks: { callback: (v) => money0(v) } } } },
    });

    const drawCat = async () => {
      const c = trend.categories.find((x) => x.id === catId) || { spent: [], allocated: [] };
      chart("ch-cat", {
        type: "bar",
        data: {
          labels: trend.months.map((m) => monthLabel(m, true)),
          datasets: [
            { type: "line", label: "Budgeted", data: c.allocated, borderColor: palette[7], borderDash: [4, 4], pointRadius: 0, tension: 0.2 },
            { label: "Spent", data: c.spent, backgroundColor: palette[0], borderRadius: 3 },
          ],
        },
        options: { scales: { y: { ticks: { callback: (v) => money0(v) } } } },
      });
      const d = await api(`/reports/category/${catId}?start=${monthAdd(end, -(span - 1))}&end=${end}`);
      const subs = d.subcategories.filter((x) => x.total > 0);
      chart("ch-sub", {
        type: "doughnut",
        data: { labels: subs.map((x) => (x.id === catId ? "(not broken down)" : x.name)), datasets: [{ data: subs.map((x) => x.total), backgroundColor: palette, borderWidth: 0 }] },
        options: { cutout: "60%", plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: (x) => `${x.label}: ${money(x.raw)}` } } } },
      });
      el.querySelector("#top-items").innerHTML = d.top_items.length
        ? `<b>Top items from receipts</b><table>${d.top_items.slice(0, 8).map((i) => `<tr><td>${esc(i.name)}</td><td class="num muted">×${i.n}</td><td class="num">${money(i.total)}</td></tr>`).join("")}</table>`
        : `<span class="muted">Upload receipts to see which items drive this envelope.</span>`;
    };
    drawCat();

    el.querySelector("#cat-pick").onchange = (e) => {
      catId = +e.target.value; drawCat();
      el.querySelector("#full-breakdown").href = `#/spending?env=${catId}`;
    };
    el.querySelectorAll("[data-span]").forEach((c) => (c.onclick = () => { span = +c.dataset.span; this.render(el, alive); }));
    el.querySelectorAll("[data-year]").forEach((b) => (b.onclick = () => { year += +b.dataset.year; this.render(el, alive); }));
  },
};
