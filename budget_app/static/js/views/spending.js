// Spending breakdown: envelopes -> subcategories -> what's in them (stores, receipt items, charges).
import { api, esc, hashParams, money, money0, monthAdd, monthLabel, state } from "../app.js";
import { chart, hatch, otherGray, series } from "./charts.js";
import { editCharge } from "./charge.js";

const MAX_COLORS = 7;          // subcategories with their own color; the rest fold into "Other"
let envId = null;
let span = 12;
let share = false;
let cmp = null;                // [monthA, monthB]
let sel = null;                // what the detail panel shows: {seg, start, end, compare}
let itemQuery = "";
let linkRead = null;

const mShort = (m) => { const [y, mo] = m.split("-"); return `${new Date(+y, +mo - 1, 1).toLocaleString("en-US", { month: "short" })} ’${y.slice(2)}`; };
const sum = (a) => a.reduce((x, y) => x + y, 0);
/** Average per month, counted from the first month with any spending (subcategories that started later
 *  aren't averaged over months when they didn't exist yet). Months from index `upto` on are left out. */
function avgSince(values, upto = values.length) {
  const first = values.findIndex((v) => Math.abs(v) > 0.004);
  if (first < 0 || first >= upto) return null;
  return sum(values.slice(first, upto)) / (upto - first);
}
const pct = (a, b) => (Math.abs(b) > 0.5 ? Math.round(((a - b) / Math.abs(b)) * 100) : null);

/** "▲ $40 (+12%)" - spending more reads as bad, less as good; arrow + text, never color alone. */
function delta(now, before, { pctOnly = false } = {}) {
  const d = now - before;
  if (Math.abs(d) < 0.5) return `<span class="muted">—</span>`;
  const p = pct(now, before);
  const txt = pctOnly ? (p === null ? "new" : `${p > 0 ? "+" : ""}${p}%`) : `${money0(Math.abs(d))}${p === null ? "" : ` (${p > 0 ? "+" : ""}${p}%)`}`;
  return `<span class="${d > 0 ? "bad" : "good"}">${d > 0 ? "▲" : "▼"} ${txt}</span>`;
}

/** The pieces an envelope is made of, in a fixed order with fixed colors. */
function segments(env, months) {
  const colors = series();
  const kids = env.children.filter((c) => c.alltime || c.spent.some((v) => v));
  const colored = new Set([...kids].sort((a, b) => Math.abs(b.alltime) - Math.abs(a.alltime)).slice(0, kids.length > MAX_COLORS + 1 ? MAX_COLORS - 1 : MAX_COLORS).map((c) => c.id));
  const segs = [];
  let slot = 0;
  for (const c of kids) {
    if (colored.has(c.id)) segs.push({ key: `c${c.id}`, label: c.name, ids: [c.id], values: c.spent, color: colors[slot++] });
  }
  const rest = kids.filter((c) => !colored.has(c.id));
  if (rest.length) {
    segs.push({ key: "other", label: rest.length === 1 ? rest[0].name : `Other (${rest.length})`, ids: rest.map((c) => c.id), other: true,
      values: months.map((_, i) => sum(rest.map((c) => c.spent[i]))), color: otherGray() });
  }
  if (env.own.some((v) => v) || !kids.length) {
    segs.push({ key: "own", label: kids.length ? "Not broken down" : env.name, ids: [env.id], own: kids.length > 0,
      values: env.own, color: kids.length ? "hatch" : colors[0] });
  }
  return { segs, kids, rest };
}

const envTotal = (env) => env.own.map((v, i) => v + sum(env.children.map((c) => c.spent[i])));

function spark(values) {
  if (values.length < 2) return "";
  const w = 64, h = 18, lo = Math.min(...values), hi = Math.max(...values), r = hi - lo || 1;
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * (w - 4) + 2).toFixed(1)},${(h - 3 - ((v - lo) / r) * (h - 6)).toFixed(1)}`);
  const [lx, ly] = pts[pts.length - 1].split(",");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts.join(" ")}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><circle cx="${lx}" cy="${ly}" r="2.2" fill="currentColor"/></svg>`;
}

export default {
  async render(el, alive) {
    const end = state.month;
    const start = monthAdd(end, -(span - 1));
    const ov = await api(`/reports/spending?start=${start}&end=${end}`);
    if (!alive()) return;
    const months = ov.months;
    const last = months.length - 1;
    const envs = ov.envelopes.map((e) => ({ ...e, total: envTotal(e) })).filter((e) => e.total.some((v) => v) || e.allocated.some((v) => v));

    // links like #/spending?env=9 or #/spending?env=9&sub=37&m=2026-10 (sub: a subcategory id, or "own")
    const hp = hashParams();
    const link = hp.toString();
    let linkSel = null;
    if (link !== linkRead) {
      linkRead = link;
      if (+hp.get("env")) { envId = +hp.get("env"); sel = null; linkSel = hp.get("sub") ? { sub: hp.get("sub"), m: hp.get("m") } : null; }
    }
    if (!envs.find((e) => e.id === envId)) {
      envId = (envs.find((e) => e.children.length && e.total[last]) || [...envs].sort((a, b) => b.total[last] - a.total[last])[0])?.id;
    }
    const env = envs.find((e) => e.id === envId);
    if (!env) { el.innerHTML = `<div class="panel empty"><div class="big">📊</div>No spending yet.</div>`; return; }
    if (!cmp || !months.includes(cmp[0]) || !months.includes(cmp[1])) cmp = [months[last], months[Math.max(0, last - 1)]];
    if (sel && !(months.includes(sel.start) && months.includes(sel.end))) sel = null;   // month or range changed
    const { segs, kids } = segments(env, months);
    const total = env.total;

    // ------------------------------------------------ envelopes this month
    const prior = months.slice(0, last);
    const rowsEnv = envs.map((e) => ({ e, now: e.total[last], before: e.total[last - 1] || 0,
      avg: prior.length ? sum(e.total.slice(0, last)) / prior.length : 0 }))
      .filter((r) => r.now || r.before || r.avg).sort((a, b) => b.now - a.now || b.avg - a.avg);
    const maxNow = Math.max(1, ...rowsEnv.map((r) => Math.max(r.now, r.avg)));
    const monthName = monthLabel(end);

    // ------------------------------------------------ envelope breakdown matrix rows
    const matrixRows = [
      ...kids.filter((c) => c.spent.some((v) => v)).map((c) => {
        const seg = segs.find((s) => s.ids.includes(c.id));
        return { key: `c${c.id}`, label: c.name, values: c.spent, color: seg.color, seg: seg.other ? { key: `c${c.id}`, label: c.name, ids: [c.id], values: c.spent } : seg };
      }),
      ...segs.filter((s) => s.key === "own" && s.values.some((v) => v)).map((s) => ({ key: "own", label: s.label, values: s.values, color: s.color, seg: s })),
    ];
    const heatMax = Math.max(1, ...matrixRows.flatMap((r) => r.values));
    const heat = (v) => (v > 0 ? `background: color-mix(in srgb, var(--accent) ${Math.round((v / heatMax) * 45)}%, transparent)` : "");
    const swatch = (c) => `<i class="swatch ${c === "hatch" ? "hatched" : ""}" style="${c === "hatch" ? "" : `background:${c}`}"></i>`;
    const segByKey = Object.fromEntries(matrixRows.map((r) => [r.key, r.seg]));
    if (sel && sel.envId === envId) {
      const fresh = sel.seg.key === "all" ? { ...sel.seg, values: total } : segByKey[sel.seg.key] || segs.find((x) => x.key === sel.seg.key);
      if (fresh) sel.seg = fresh;
    }

    // ------------------------------------------------ compare
    const [A, B] = cmp;
    const ia = months.indexOf(A), ib = months.indexOf(B);
    const cmpRows = matrixRows.map((r) => ({ ...r, a: r.values[ia], b: r.values[ib] })).filter((r) => r.a || r.b);
    const monthOpts = (v) => [...months].reverse().map((m) => `<option value="${m}" ${m === v ? "selected" : ""}>${monthLabel(m)}</option>`).join("");

    el.innerHTML = `<div class="stack">
      <div class="panel">
        <div class="panel-head"><h2>${esc(monthName)} by envelope</h2><span class="muted small">compared with ${esc(monthLabel(months[last - 1] || end))} and the ${prior.length}-month average before it · click an envelope to break it down</span></div>
        <div class="table-wrap"><table class="env-table">
          <thead><tr><th>Envelope</th><th style="width:34%"></th><th class="num">${esc(mShort(end))}</th><th class="num">${esc(mShort(months[last - 1] || end))}</th><th class="num">Change</th><th class="num">Avg / mo</th><th class="num">vs avg</th></tr></thead>
          <tbody>${rowsEnv.map((r) => `<tr class="clickable ${r.e.id === envId ? "selected" : ""} ${!r.now && !r.before ? "quiet hidden" : ""}" data-env="${r.e.id}">
              <td><b>${esc(r.e.name)}</b>${r.e.children.length ? ` <span class="muted small">${r.e.children.length} sub</span>` : ""}</td>
              <td><div class="hbar" title="${esc(money(r.now))} this month · avg ${esc(money(r.avg))}"><span style="width:${Math.max(0, (r.now / maxNow) * 100)}%"></span>${r.avg > 0 ? `<em style="left:${(r.avg / maxNow) * 100}%"></em>` : ""}</div></td>
              <td class="num"><b>${money0(r.now)}</b></td><td class="num muted">${money0(r.before)}</td><td class="num">${delta(r.now, r.before)}</td>
              <td class="num muted">${money0(r.avg)}</td><td class="num">${delta(r.now, r.avg, { pctOnly: true })}</td></tr>`).join("")}</tbody>
          <tfoot><tr><td>Total</td><td></td><td class="num">${money0(sum(rowsEnv.map((r) => r.now)))}</td><td class="num">${money0(sum(rowsEnv.map((r) => r.before)))}</td>
            <td class="num">${delta(sum(rowsEnv.map((r) => r.now)), sum(rowsEnv.map((r) => r.before)))}</td><td class="num">${money0(sum(rowsEnv.map((r) => r.avg)))}</td>
            <td class="num">${delta(sum(rowsEnv.map((r) => r.now)), sum(rowsEnv.map((r) => r.avg)), { pctOnly: true })}</td></tr></tfoot>
        </table></div>
        <p class="small muted" style="margin:8px 0 0">The bar is this month; the tick mark is the average.
          ${rowsEnv.some((r) => !r.now && !r.before) ? `<a href="javascript:void 0" id="show-quiet">Show ${rowsEnv.filter((r) => !r.now && !r.before).length} envelopes with nothing spent in either month</a>` : ""}</p>
      </div>

      <div class="panel" id="sp-break">
        <div class="panel-head"><h2>Breakdown</h2>
          <select id="sp-env">${envs.map((e) => `<option value="${e.id}" ${e.id === envId ? "selected" : ""}>${esc(e.name)}${e.children.length ? ` (${e.children.length})` : ""}</option>`).join("")}</select>
          <span class="spacer"></span>
          <div class="chips">${[6, 12, 24].map((n) => `<span class="chip ${span === n ? "active" : ""}" data-span="${n}">${n < 12 ? `${n} mo` : `${n / 12} yr`}</span>`).join("")}</div>
          <div class="chips">${[["$", false], ["% share", true]].map(([l, v]) => `<span class="chip ${share === v ? "active" : ""}" data-share="${v}">${l}</span>`).join("")}</div>
        </div>
        <div class="chart-box" style="height:300px"><canvas id="sp-stack" role="img" aria-label="${esc(env.name)} spending by subcategory per month"></canvas></div>
        <p class="small muted" style="margin:6px 0 12px">Click a bar segment or a table cell to see what's in it.${segs.some((s) => s.own && s.values.some((v) => v))
          ? ` ${swatch("hatch")} <b>Not broken down</b> is money filed on ${esc(env.name)} itself: spreadsheet months (tracked per envelope only) and charges without a receipt.` : ""}</p>
        <div class="table-wrap"><table class="matrix sp-matrix">
          <thead><tr><th>${esc(env.name)}</th>${months.map((m, i) => `<th class="num ${i === last ? "focus" : ""}">${esc(mShort(m))}</th>`).join("")}<th class="num">Avg</th><th class="num">Total</th></tr></thead>
          <tbody>${matrixRows.map((r) => `<tr><td class="cat"><a href="javascript:void 0" data-row="${r.key}">${swatch(r.color)}${esc(r.label)}</a></td>
              ${r.values.map((v, i) => `<td class="num cell ${i === last ? "focus" : ""}" style="${heat(v)}" data-cell="${r.key}" data-m="${months[i]}">${v ? money0(v) : ""}</td>`).join("")}
              <td class="num muted">${money0(avgSince(r.values) || 0)}</td><td class="num"><b>${money0(sum(r.values))}</b></td></tr>`).join("")}</tbody>
          <tfoot><tr><td>Total spent</td>${total.map((v, i) => `<td class="num ${i === last ? "focus" : ""}">${v ? money0(v) : ""}</td>`).join("")}<td class="num">${money0(sum(total) / months.length)}</td><td class="num">${money0(sum(total))}</td></tr>
            <tr><td class="muted">Budgeted</td>${env.allocated.map((v, i) => `<td class="num muted ${i === last ? "focus" : ""}">${v ? money0(v) : ""}</td>`).join("")}<td class="num muted">${money0(sum(env.allocated) / months.length)}</td><td class="num muted">${money0(sum(env.allocated))}</td></tr></tfoot>
        </table></div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Compare months</h2>
          <select id="cmp-a">${monthOpts(A)}</select><span class="muted">vs</span><select id="cmp-b">${monthOpts(B)}</select>
          <span class="muted small">${esc(env.name)}</span></div>
        ${cmpRows.length ? `<div class="grid grid-2">
          <div class="chart-box" style="height:${Math.max(160, cmpRows.length * 34 + 60)}px"><canvas id="sp-cmp" role="img" aria-label="${esc(env.name)}: ${esc(monthLabel(A))} vs ${esc(monthLabel(B))}"></canvas></div>
          <div class="table-wrap"><table class="matrix">
            <thead><tr><th></th><th class="num">${esc(mShort(A))}</th><th class="num">${esc(mShort(B))}</th><th class="num">Change</th><th></th></tr></thead>
            <tbody>${cmpRows.map((r) => `<tr><td class="cat">${swatch(r.color)}${esc(r.label)}</td><td class="num"><b>${money0(r.a)}</b></td><td class="num muted">${money0(r.b)}</td>
              <td class="num">${delta(r.a, r.b)}</td><td><button class="small ghost" data-why="${r.key}">What changed?</button></td></tr>`).join("")}</tbody>
            <tfoot><tr><td>Total</td><td class="num">${money0(total[ia])}</td><td class="num">${money0(total[ib])}</td><td class="num">${delta(total[ia], total[ib])}</td>
              <td><button class="small ghost" data-why="all">What changed?</button></td></tr></tfoot>
          </table></div></div>` : `<div class="muted">No ${esc(env.name)} spending in either month.</div>`}
      </div>

      <div class="panel ${sel ? "" : "hidden"}" id="sp-detail"></div>
    </div>`;

    // ------------------------------------------------ charts
    const surface = getComputedStyle(document.documentElement).getPropertyValue("--panel").trim();
    const pattern = hatch();
    const stackData = segs.map((s) => ({
      label: s.label, seg: s, data: share ? s.values.map((v, i) => (total[i] ? (v / total[i]) * 100 : 0)) : s.values,
      backgroundColor: s.color === "hatch" ? pattern : s.color, borderColor: surface, borderWidth: { top: 2 }, borderSkipped: false, stack: "spent",
      barPercentage: 0.8, categoryPercentage: 0.85,
    }));
    const datasets = [...stackData];
    if (!share && env.allocated.some((v) => v)) {
      datasets.push({ type: "line", label: "Budgeted", data: env.allocated, borderColor: getComputedStyle(document.documentElement).getPropertyValue("--muted").trim(),
        borderDash: [5, 4], borderWidth: 2, pointRadius: 0, pointHitRadius: 6, tension: 0, stack: "budget" });
    }
    chart("sp-stack", {
      type: "bar",
      data: { labels: months.map(mShort), datasets },
      options: {
        interaction: { mode: "index", intersect: false },
        scales: { x: { stacked: true }, y: { stacked: true, max: share ? 100 : undefined, ticks: { callback: (v) => (share ? `${v}%` : money0(v)) } } },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } },
          tooltip: {
            itemSort: (a, b) => b.raw - a.raw,
            filter: (x) => Math.abs(x.raw) > 0.004,
            callbacks: {
              label: (x) => (x.dataset.seg && share ? ` ${x.dataset.label}: ${Math.round(x.raw)}% · ${money(x.dataset.seg.values[x.dataIndex])}` : ` ${x.dataset.label}: ${money(x.raw)}`),
              footer: (xs) => (xs.length ? `Total spent: ${money(total[xs[0].dataIndex])}` : ""),
            },
          },
        },
        onClick: (evt, _els, ch) => {
          const hit = ch.getElementsAtEventForMode(evt, "nearest", { intersect: true }, false)[0];
          const ds = hit && ch.data.datasets[hit.datasetIndex];
          if (ds?.seg) openDetail({ seg: ds.seg, start: months[hit.index], end: months[hit.index] });
        },
        onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? "pointer" : "default"; },
      },
    });

    if (cmpRows.length) {
      chart("sp-cmp", {
        type: "bar",
        data: {
          labels: cmpRows.map((r) => r.label),
          datasets: [
            { label: monthLabel(A), data: cmpRows.map((r) => r.a), backgroundColor: series()[0], borderRadius: 4, borderSkipped: "start", barPercentage: 0.8, categoryPercentage: 0.7 },
            { label: monthLabel(B), data: cmpRows.map((r) => r.b), backgroundColor: otherGray(), borderRadius: 4, borderSkipped: "start", barPercentage: 0.8, categoryPercentage: 0.7 },
          ],
        },
        options: {
          indexAxis: "y",
          interaction: { mode: "index", intersect: false },
          scales: { x: { ticks: { callback: (v) => money0(v) } }, y: { grid: { display: false } } },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } },
            tooltip: { callbacks: { label: (x) => ` ${x.dataset.label}: ${money(x.raw)}`,
              footer: (xs) => { const r = cmpRows[xs[0].dataIndex]; const d = r.a - r.b; return `Change: ${d >= 0 ? "+" : "−"}${money(Math.abs(d))}`; } } } },
          onClick: (evt, _els, ch) => {
            const hit = ch.getElementsAtEventForMode(evt, "nearest", { intersect: true }, false)[0];
            if (hit) openDetail({ seg: cmpRows[hit.index].seg, start: A < B ? A : B, end: A < B ? B : A, compare: [A, B] });
          },
          onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? "pointer" : "default"; },
        },
      });
    }

    // ------------------------------------------------ detail (drill-down)
    const detailEl = el.querySelector("#sp-detail");
    const openDetail = (s) => { sel = { ...s, envId }; itemQuery = ""; drawDetail(true); };
    const drawDetail = async (scroll) => {
      if (!sel || sel.envId !== envId) { sel = null; detailEl.classList.add("hidden"); return; }
      const s = sel.seg;
      const q = new URLSearchParams({ start: sel.start, end: sel.end });
      if (s.own) q.set("own", "1");
      if (s.other) q.set("ids", s.ids.join(","));
      const cid = s.own || s.other ? env.id : s.ids[0];
      detailEl.classList.remove("hidden");
      detailEl.innerHTML = `<div class="muted"><span class="spinner"></span> Loading…</div>`;
      if (scroll) detailEl.scrollIntoView({ behavior: "smooth", block: "start" });
      const d = await api(`/reports/spending/${cid}?${q}`);
      if (!alive() || sel?.seg !== s) return;
      const cmpMode = sel.compare && sel.compare[0] !== sel.compare[1];
      const [ca, cb] = sel.compare || [];
      const inPeriod = (m) => (cmpMode ? m === ca || m === cb : m >= sel.start && m <= sel.end);
      const txns = d.transactions.filter((t) => inPeriod(t.budget_month));
      const spent = sum(txns.map((t) => t.amount));
      const single = sel.start === sel.end;
      const periodLabel = cmpMode ? `${monthLabel(ca)} vs ${monthLabel(cb)}` : single ? monthLabel(sel.start) : `${monthLabel(sel.start, true)} – ${monthLabel(sel.end, true)}`;
      const title = s.own ? `${env.name} · not broken down` : s.other ? `${env.name} · ${s.label}` : (kids.length ? `${env.name} › ${s.label}` : s.label);
      const segValues = s.values || [];
      const mi = months.indexOf(sel.end);

      // items: in compare mode sort by what changed
      let items = d.items.map((i) => ({ ...i, a: i.by_month[ca] || 0, b: i.by_month[cb] || 0 }));
      if (cmpMode) items = items.filter((i) => i.a || i.b).sort((x, y) => Math.abs(y.a - y.b) - Math.abs(x.a - x.b));
      const shown = items.filter((i) => !itemQuery || i.name.toLowerCase().includes(itemQuery) || i.stores.join(" ").toLowerCase().includes(itemQuery));
      const itemsTotal = sum(d.items.map((i) => sum(Object.entries(i.by_month).filter(([m]) => inPeriod(m)).map(([, v]) => v))));
      const stores = [];
      for (const t of txns) { const x = stores.find((y) => y.name === t.who); if (x) { x.total += t.amount; x.n++; } else stores.push({ name: t.who, total: t.amount, n: 1 }); }
      stores.sort((a, b) => b.total - a.total);
      const charges = new Set(txns.map((t) => t.id)).size;
      const prevV = mi > 0 ? segValues[mi - 1] : null;
      const avgV = mi > 0 ? avgSince(segValues, mi) : null;

      detailEl.innerHTML = `
        <div class="panel-head"><h2>${esc(title)}</h2><span class="badge accent">${esc(periodLabel)}</span><span class="spacer"></span>
          ${single && months.length > 1 ? `<button class="small" data-period="range">Show all ${months.length} months</button>` : ""}
          ${!single && !cmpMode ? `<button class="small" data-period="month">Just ${esc(monthLabel(end))}</button>` : ""}
          <button class="ghost" data-close-detail title="Close">✕</button></div>
        <div class="kpis" style="margin-bottom:16px">
          <div class="kpi"><div class="label">Spent</div><div class="value">${money(cmpMode ? (segValues[months.indexOf(ca)] || 0) : spent)}</div>
            <div class="sub">${cmpMode ? `vs ${money(segValues[months.indexOf(cb)] || 0)} in ${esc(mShort(cb))}` : single && prevV !== null ? `${delta(spent, prevV)} vs ${esc(mShort(months[mi - 1]))}` : `${money(spent / Math.max(1, months.filter(inPeriod).length))} per month`}</div></div>
          <div class="kpi"><div class="label">Charges</div><div class="value">${charges}</div><div class="sub">${charges ? `${money(spent / charges)} average` : "none"}</div></div>
          <div class="kpi"><div class="label">Receipt items</div><div class="value">${sum(d.items.map((i) => i.buys.filter((b) => inPeriod(b.month)).length))}</div>
            <div class="sub">${itemsTotal ? `${money(itemsTotal)} before tax` : "no receipts here yet"}</div></div>
          <div class="kpi"><div class="label">${single ? "vs average" : "Stores"}</div><div class="value">${single ? (avgV !== null ? delta(spent, avgV, { pctOnly: true }) : "—") : stores.length}</div>
            <div class="sub">${single ? (avgV !== null ? `avg ${money(avgV)} / mo before` : "") : esc(stores[0]?.name || "")}</div></div>
        </div>
        <div class="grid grid-2">
          <div><b class="small">By store</b><div class="chart-box" style="height:${Math.max(120, Math.min(8, stores.length) * 30 + 40)}px"><canvas id="sp-stores" role="img" aria-label="By store"></canvas></div></div>
          <div><b class="small">${esc(s.label)} by month</b> <span class="muted small">click a month</span><div class="chart-box" style="height:200px"><canvas id="sp-month" role="img" aria-label="By month"></canvas></div></div>
        </div>

        <div class="panel-head" style="margin:18px 0 8px"><h3 style="margin:0">${cmpMode ? "What changed, item by item" : "What it's made of"}</h3>
          ${d.items.length ? `<input id="item-q" type="search" placeholder="Search items or stores" value="${esc(itemQuery)}" style="width:220px">` : ""}
          <span class="muted small">${cmpMode ? `sorted by the biggest change between ${esc(mShort(ca))} and ${esc(mShort(cb))}` : "items from your receipts, grouped by name · click one for its price history"}</span></div>
        ${d.items.length ? `<div class="table-wrap" style="max-height:520px"><table class="items-breakdown">
          <thead><tr><th>Item</th>${cmpMode ? `<th class="num">${esc(mShort(ca))}</th><th class="num">${esc(mShort(cb))}</th><th class="num">Change</th>`
            : `<th class="num">Bought</th><th class="num">Total</th><th class="num">Avg paid</th><th class="num">Low – high</th><th class="num">Last paid</th><th title="What you paid each time, oldest to newest; the arrow compares the last purchase with your average">Each time</th>`}<th>Where</th></tr></thead>
          <tbody>${shown.slice(0, 300).map((i) => {
            const k = esc(i.key);
            const trend = i.n >= 2 ? `<span class="muted">${spark(i.buys.map((b) => b.amount))}</span> ${i.n >= 3 ? delta(i.last, i.avg, { pctOnly: true }) : ""}` : `<span class="muted small">once</span>`;
            return `<tr class="clickable" data-item="${k}"><td><b>${esc(i.name)}</b>${i.unchecked ? ` <span class="badge" title="On receipts you haven't reviewed yet - the category is a guess">✨ ${i.unchecked} unchecked</span>` : ""}
                ${i.categories.length > 1 || (d.category.parent_id === null && i.categories[0] !== d.category.name) ? `<div class="desc-raw">${esc(i.categories.join(", "))}</div>` : ""}</td>
              ${cmpMode ? `<td class="num"><b>${money(i.a)}</b></td><td class="num muted">${money(i.b)}</td><td class="num">${delta(i.a, i.b)}</td>`
                : `<td class="num">×${i.n}</td><td class="num"><b>${money(i.total)}</b></td><td class="num">${money(i.avg)}</td>
                   <td class="num muted">${i.n > 1 ? `${money(i.low)} – ${money(i.high)}` : ""}</td><td class="num">${money(i.last)}</td><td>${trend}</td>`}
              <td class="small muted">${esc(i.stores.join(", "))}</td></tr>
              <tr class="buys hidden" data-buys="${k}"><td colspan="${cmpMode ? 5 : 8}"><table class="matrix">${i.buys.slice().reverse().map((b) => `<tr>
                <td>${esc(b.date || "")}</td><td>${esc(b.store || "")}</td><td class="num">${money(b.amount)}</td><td class="muted">${esc(b.category || "")}</td>
                <td>${b.checked ? "" : `<span class="badge">✨ unchecked</span>`}</td><td><a href="#/receipts?open=${b.receipt_id}">receipt →</a></td></tr>`).join("")}</table></td></tr>`;
          }).join("")}</tbody></table></div>
          ${shown.length > 300 ? `<p class="muted small">Showing the first 300 of ${shown.length}.</p>` : ""}`
          : `<div class="alert info"><span class="ico">🧾</span><div>No receipt items in this period yet. Upload receipts (Meijer PDFs, photos, the Amazon order export) and
              apply them to their card charges to see exactly what these ${esc(money0(spent))} were spent on.</div></div>`}

        <h3 style="margin:20px 0 8px">Charges <span class="muted small">(${txns.length}) · click a charge to change its category, split it or add a note</span></h3>
        <div class="table-wrap" style="max-height:420px"><table>
          <thead><tr><th>Date</th><th>Where</th><th class="num">Here</th><th class="num">Whole charge</th><th>From</th><th></th></tr></thead>
          <tbody>${txns.map((t) => `<tr class="clickable" data-tid="${t.id}" title="Edit this charge"><td>${esc(t.date)}</td><td><span class="desc-main">${esc(t.who)}</span>${t.who !== t.description ? `<div class="desc-raw">${esc(t.description)}</div>` : ""}
              ${t.category && t.category !== s.label && !s.own ? `<div class="desc-raw">${esc(t.category)}</div>` : ""}</td>
            <td class="num"><b>${money(t.amount)}</b></td><td class="num muted">${Math.abs(t.txn_amount - t.amount) > 0.004 ? money(t.txn_amount) : ""}</td>
            <td><span class="badge">${t.source === "excel" ? "spreadsheet" : esc(t.account || t.source)}</span></td>
            <td class="small" style="white-space:nowrap">${t.receipt_id ? `<a href="#/receipts?open=${t.receipt_id}">receipt</a> · ` : ""}<button class="small ghost" data-edit="${t.id}">✎ Edit</button></td></tr>`).join("")}</tbody>
        </table></div>`;

      chart("sp-stores", {
        type: "bar",
        data: { labels: stores.slice(0, 8).map((x) => x.name), datasets: [{ label: "Spent", data: stores.slice(0, 8).map((x) => x.total), backgroundColor: series()[0], borderRadius: 4, borderSkipped: "start", barPercentage: 0.75 }] },
        options: { indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: { label: (x) => ` ${money(x.raw)} · ${stores[x.dataIndex].n} charge${stores[x.dataIndex].n === 1 ? "" : "s"}` } } },
          scales: { x: { ticks: { callback: (v) => money0(v) } }, y: { grid: { display: false } } } },
      });
      const picked = (i) => inPeriod(months[i]);
      chart("sp-month", {
        type: "bar",
        data: { labels: months.map(mShort), datasets: [{ label: s.label, data: segValues,
          backgroundColor: months.map((_, i) => (picked(i) ? series()[0] : otherGray())), borderRadius: 4, borderSkipped: "start", barPercentage: 0.8 }] },
        options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: (x) => ` ${money(x.raw)}` } } },
          scales: { y: { ticks: { callback: (v) => money0(v) } } },
          onClick: (evt, _els, ch) => {
            const hit = ch.getElementsAtEventForMode(evt, "nearest", { intersect: false, axis: "x" }, false)[0];
            if (hit) openDetail({ seg: s, start: months[hit.index], end: months[hit.index] });
          },
          onHover: (evt) => { evt.native.target.style.cursor = "pointer"; } },
      });

      detailEl.querySelector("[data-close-detail]").onclick = () => { sel = null; detailEl.classList.add("hidden"); };
      detailEl.querySelectorAll("[data-period]").forEach((b) => (b.onclick = () => {
        openDetail(b.dataset.period === "range" ? { seg: s, start: months[0], end: months[last] } : { seg: s, start: end, end });
      }));
      detailEl.querySelectorAll("[data-tid]").forEach((tr) => (tr.onclick = (e) => {
        if (e.target.closest("a")) return;
        editCharge(+tr.dataset.tid, { onSaved: refresh });
      }));
      detailEl.querySelectorAll("[data-item]").forEach((tr) => (tr.onclick = (e) => {
        if (e.target.closest("a")) return;
        detailEl.querySelector(`[data-buys="${CSS.escape(tr.dataset.item)}"]`)?.classList.toggle("hidden");
      }));
      const qi = detailEl.querySelector("#item-q");
      if (qi) {
        qi.oninput = () => {
          itemQuery = qi.value.trim().toLowerCase();
          const pos = qi.selectionStart;
          drawDetail(false).then(() => { const n = detailEl.querySelector("#item-q"); if (n) { n.focus(); n.setSelectionRange(pos, pos); } });
        };
      }
    };
    if (linkSel) {
      const seg = segByKey[linkSel.sub === "own" ? "own" : `c${linkSel.sub}`];
      const m = months.includes(linkSel.m) ? linkSel.m : null;
      if (seg) openDetail({ seg, start: m || months[0], end: m || months[last] });
    } else if (sel) await drawDetail(false);

    // ------------------------------------------------ controls
    const rerender = () => this.render(el, alive);
    // after a charge is edited: redraw everything, keep the open drill-down and where you were on the page
    const refresh = async () => {
      const y = document.scrollingElement.scrollTop;
      el.style.minHeight = `${el.offsetHeight}px`;
      try { await rerender(); } finally { el.style.minHeight = ""; }
      document.scrollingElement.scrollTop = y;
    };
    const sq = el.querySelector("#show-quiet");
    if (sq) sq.onclick = () => { el.querySelectorAll("tr.quiet").forEach((r) => r.classList.remove("hidden")); sq.remove(); };
    el.querySelectorAll("[data-env]").forEach((tr) => (tr.onclick = () => {
      envId = +tr.dataset.env; sel = null; rerender().then(() => el.querySelector("#sp-break")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }));
    el.querySelector("#sp-env").onchange = (e) => { envId = +e.target.value; sel = null; rerender(); };
    el.querySelectorAll("[data-span]").forEach((c) => (c.onclick = () => { span = +c.dataset.span; rerender(); }));
    el.querySelectorAll("[data-share]").forEach((c) => (c.onclick = () => { share = c.dataset.share === "true"; rerender(); }));
    el.querySelectorAll("[data-cell]").forEach((td) => (td.onclick = () => {
      if (td.textContent.trim()) openDetail({ seg: segByKey[td.dataset.cell], start: td.dataset.m, end: td.dataset.m });
    }));
    el.querySelectorAll("[data-row]").forEach((a) => (a.onclick = () => openDetail({ seg: segByKey[a.dataset.row], start: months[0], end: months[last] })));
    el.querySelector("#cmp-a").onchange = (e) => { cmp = [e.target.value, B]; rerender(); };
    el.querySelector("#cmp-b").onchange = (e) => { cmp = [A, e.target.value]; rerender(); };
    el.querySelectorAll("[data-why]").forEach((b) => (b.onclick = () => {
      const seg = b.dataset.why === "all" ? { key: "all", label: env.name, ids: [env.id], values: total } : segByKey[b.dataset.why];
      openDetail({ seg, start: A < B ? A : B, end: A < B ? B : A, compare: [A, B] });
    }));
  },
};
