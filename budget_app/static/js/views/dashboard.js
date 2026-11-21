import { api, esc, money, money0, monthAdd, monthLabel, state, signClass } from "../app.js";
import { palette, chart } from "./charts.js";

function envCard(e) {
  const budget = e.carry_in + e.allocated;
  const pct = budget > 0 ? Math.min(100, (e.spent / budget) * 100) : e.spent > 0 ? 100 : 0;
  const cls = e.available < -0.004 ? "bad" : pct > 85 ? "warn" : "";
  return `<div class="env" data-cat="${e.id}" title="Click to see transactions">
      <div class="env-top"><span class="env-name">${esc(e.name)}</span>
        <span class="env-avail ${signClass(e.available)}">${money(e.available)}</span></div>
      <div class="bar ${cls}"><span style="width:${pct.toFixed(1)}%"></span></div>
      <div class="env-sub"><span>Spent ${money0(e.spent)}</span><span>of ${money0(budget)}</span></div>
    </div>`;
}

export default {
  async render(el, alive) {
    const m = state.month;
    const [s, h, merchants] = await Promise.all([
      api(`/summary?month=${m}`),
      api(`/history?end=${m}&start=${monthAdd(m, -11)}`),
      api(`/reports/merchants?start=${m}&end=${m}`),
    ]);
    if (!alive()) return;

    const groups = {};
    for (const e of s.envelopes) (groups[e.group] ||= []).push(e);
    const overspent = s.envelopes.filter((e) => e.available < -0.004);

    const alerts = [];
    if (s.uncategorized_count)
      alerts.push(`<a class="alert" href="#/transactions?filter=uncategorized"><span class="ico">🏷️</span>
        <span><b>${s.uncategorized_count} transactions</b> (${money(s.uncategorized_amount)}) still need a category.</span><span class="spacer"></span><span>Categorize →</span></a>`);
    if (s.to_review)
      alerts.push(`<a class="alert info" href="#/transactions?filter=review"><span class="ico">👀</span>
        <span><b>${s.to_review} auto-categorized transactions</b> are waiting for a quick review.</span><span class="spacer"></span><span>Review →</span></a>`);
    if (Math.abs(s.left_to_allocate) > 0.5 && s.income)
      alerts.push(`<a class="alert info" href="#/budget"><span class="ico">✉️</span>
        <span>${s.left_to_allocate > 0 ? `<b>${money(s.left_to_allocate)}</b> of this month's income isn't assigned to an envelope yet.`
          : `You've budgeted <b>${money(-s.left_to_allocate)}</b> more than this month's income.`}</span><span class="spacer"></span><span>Budget →</span></a>`);

    el.innerHTML = `<div class="stack">
      <div class="kpis">
        <div class="panel kpi"><div class="label">Income</div><div class="value">${money(s.income)}</div><div class="sub">${monthLabel(m)}</div></div>
        <div class="panel kpi"><div class="label">Budgeted</div><div class="value">${money(s.allocated)}</div>
          <div class="sub ${signClass(s.left_to_allocate)}">${money(s.left_to_allocate)} left to budget</div></div>
        <div class="panel kpi"><div class="label">Spent</div><div class="value">${money(s.spent)}</div>
          <div class="sub">${s.allocated ? Math.round((s.spent / s.allocated) * 100) + "% of budgeted" : "&nbsp;"}</div></div>
        <div class="panel kpi"><div class="label">In all envelopes</div><div class="value ${signClass(s.total_available)}">${money(s.total_available)}</div>
          <div class="sub ${overspent.length ? "bad" : "muted"}">${overspent.length ? overspent.length + " envelope(s) overspent" : "No envelopes overspent"}</div></div>
      </div>
      ${alerts.join("")}
      <div class="grid grid-2">
        <div class="panel"><div class="panel-head"><h2>Income vs. spending</h2><span class="muted small">last 12 months</span></div>
          <div class="chart-box"><canvas id="ch-trend"></canvas></div></div>
        <div class="panel"><div class="panel-head"><h2>Where it went</h2><span class="muted small">${monthLabel(m)}</span></div>
          <div class="chart-box"><canvas id="ch-groups"></canvas></div></div>
      </div>
      <div class="panel">
        <div class="panel-head"><h2>Envelopes</h2><span class="muted small">available = carried over + budgeted − spent</span></div>
        ${Object.entries(groups).map(([g, list]) => `<div class="group-title">${esc(g)}</div>
          <div class="envelopes" style="margin-bottom:16px">${list.map(envCard).join("")}</div>`).join("")}
      </div>
      <div class="panel"><div class="panel-head"><h2>Top merchants</h2><span class="muted small">${monthLabel(m)}</span></div>
        ${merchants.length ? `<div class="table-wrap"><table><thead><tr><th>Merchant</th><th class="num">Visits</th><th class="num">Total</th></tr></thead><tbody>
          ${merchants.slice(0, 10).map((r) => `<tr><td>${esc(r.merchant)}</td><td class="num">${r.n}</td><td class="num">${money(r.total)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">No card transactions imported for this month yet.<br><a href="#/transactions">Import a statement →</a></div>`}
      </div>
    </div>`;

    el.querySelectorAll(".env").forEach((c) => (c.onclick = () => (location.hash = `#/transactions?category=${c.dataset.cat}`)));

    chart("ch-trend", {
      type: "bar",
      data: {
        labels: h.months.map((x) => monthLabel(x, true)),
        datasets: [
          { label: "Income", data: h.income, backgroundColor: palette[1], borderRadius: 4 },
          { label: "Spent", data: h.spent, backgroundColor: palette[0], borderRadius: 4 },
        ],
      },
      options: { scales: { y: { ticks: { callback: (v) => money0(v) } } } },
    });

    const byGroup = {};
    for (const e of s.envelopes) if (e.spent > 0) byGroup[e.group] = (byGroup[e.group] || 0) + e.spent;
    const entries = Object.entries(byGroup).sort((a, b) => b[1] - a[1]);
    chart("ch-groups", {
      type: "doughnut",
      data: { labels: entries.map((x) => x[0]), datasets: [{ data: entries.map((x) => x[1]), backgroundColor: palette, borderWidth: 0 }] },
      options: { cutout: "62%", plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: (c) => `${c.label}: ${money(c.raw)}` } } } },
    });
  },
};
