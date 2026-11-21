import { api, esc, fail, money, monthLabel, render, signClass, state, toast, today } from "../app.js";

export default {
  async render(el, alive) {
    const m = state.month;
    const [s, income, recurring] = await Promise.all([
      api(`/summary?month=${m}`), api(`/income?month=${m}`), api(`/recurring?month=${m}`),
    ]);
    if (!alive()) return;
    const pendingBills = recurring.filter((r) => r.active && !r.added);

    const groups = {};
    for (const e of s.envelopes) if (!e.archived || e.available || e.allocated) (groups[e.group] ||= []).push(e);

    el.innerHTML = `<div class="stack">
      <div class="grid grid-3">
        <div class="panel kpi"><div class="label">Income this month</div><div class="value">${money(s.income)}</div></div>
        <div class="panel kpi"><div class="label">Budgeted</div><div class="value" id="k-alloc">${money(s.allocated)}</div></div>
        <div class="panel kpi"><div class="label">Left to budget</div><div class="value ${signClass(s.left_to_allocate)}" id="k-left">${money(s.left_to_allocate)}</div>
          <div class="sub">Aim for $0 - every dollar gets a job</div></div>
      </div>

      <div class="grid grid-2">
        <div class="panel">
          <div class="panel-head"><h2>Income</h2><span class="spacer"></span></div>
          <table><tbody>
            ${income.map((i) => `<tr><td>${esc(i.description || "Income")}</td><td class="small muted">${i.date || ""}</td>
              <td class="num">${money(i.amount)}</td><td style="width:30px"><button class="ghost small" data-delinc="${i.id}">✕</button></td></tr>`).join("") ||
              `<tr><td class="muted small">No income recorded for ${monthLabel(m)} yet.</td></tr>`}
          </tbody></table>
          <div class="row" style="margin-top:10px">
            <input id="inc-desc" placeholder="Paycheck" style="flex:1;min-width:120px">
            <input id="inc-date" type="date" value="${today().slice(0, 7) === m ? today() : m + "-01"}">
            <input id="inc-amt" class="money" placeholder="0.00">
            <button class="primary" data-addinc>Add</button>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Recurring bills</h2><span class="spacer"></span><a href="#/settings?tab=recurring" class="small">Manage →</a></div>
          ${recurring.length ? `<table><tbody>${recurring.filter((r) => r.active).map((r) => `<tr>
              <td>${esc(r.name)}<div class="small muted">${esc(r.category || "Uncategorized")} · day ${r.day_of_month} · ${esc(r.account)}</div></td>
              <td class="num">${money(r.amount)}</td>
              <td>${r.added ? `<span class="badge good">added</span>` : `<span class="badge warn">not yet</span>`}</td></tr>`).join("")}</tbody></table>
            <div class="row" style="margin-top:10px"><span class="spacer"></span>
              <button class="primary" data-addbills ${pendingBills.length ? "" : "disabled"}>Add ${pendingBills.length} bill(s) to ${monthLabel(m, true)}</button></div>`
            : `<div class="muted small">Set up bills you pay every month from checking (mortgage, car payment, giving, student loans…)
               and add them all with one click. <a href="#/settings?tab=recurring">Set up recurring bills →</a></div>`}
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Envelopes · ${monthLabel(m)}</h2><span class="spacer"></span>
          <button data-fill="defaults" title="Fill empty envelopes with each envelope's usual amount">Fill with usual amounts</button>
          <button data-fill="last_month">Copy last month</button>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Envelope</th><th class="num">Carried in</th><th class="num">Budgeted</th><th class="num">Spent</th><th class="num">Available</th><th class="num">Usual</th></tr></thead>
          <tbody>
          ${Object.entries(groups).map(([g, list]) => `<tr class="group-row"><td colspan="6">${esc(g)}</td></tr>` +
            list.map((e) => `<tr>
              <td>${esc(e.name)}${e.archived ? ` <span class="badge">archived</span>` : ""}</td>
              <td class="num ${signClass(e.carry_in)}">${money(e.carry_in)}</td>
              <td class="num"><input class="money" data-alloc="${e.id}" value="${e.allocated ? e.allocated.toFixed(2) : ""}" placeholder="0.00"></td>
              <td class="num"><a href="#/transactions?category=${e.id}">${money(e.spent)}</a></td>
              <td class="num ${signClass(e.available)}" data-avail="${e.id}"><b>${money(e.available)}</b></td>
              <td class="num muted"><a href="#" data-usual="${e.id}" title="Use this amount">${money(e.default_amount)}</a></td>
            </tr>`).join("")).join("")}
          </tbody>
          <tfoot><tr><td>Total</td><td class="num">${money(s.envelopes.reduce((a, e) => a + e.carry_in, 0))}</td>
            <td class="num" id="f-alloc">${money(s.allocated)}</td><td class="num">${money(s.spent)}</td>
            <td class="num" id="f-avail">${money(s.total_available)}</td><td></td></tr></tfoot>
        </table></div>
        <p class="small muted">Changes save automatically. "Usual" is each envelope's default (edit it in Settings → Categories).</p>
      </div>
    </div>`;

    const env = Object.fromEntries(s.envelopes.map((e) => [e.id, e]));
    const recompute = () => {
      const alloc = s.envelopes.reduce((a, e) => a + e.allocated, 0);
      el.querySelector("#k-alloc").textContent = money(alloc);
      el.querySelector("#f-alloc").textContent = money(alloc);
      const left = s.income - alloc;
      const k = el.querySelector("#k-left");
      k.textContent = money(left);
      k.className = "value " + signClass(left);
      el.querySelector("#f-avail").textContent = money(s.envelopes.reduce((a, e) => a + e.available, 0));
    };
    const saveAlloc = async (input) => {
      const e = env[input.dataset.alloc];
      const v = Math.round((parseFloat(input.value.replace(/[$,]/g, "")) || 0) * 100) / 100;
      if (v === e.allocated) return;
      try {
        await api("/allocations", { method: "PUT", body: { month: m, items: [{ category_id: e.id, amount: v }] } });
        e.available = Math.round((e.available + v - e.allocated) * 100) / 100;
        e.allocated = v;
        const cell = el.querySelector(`[data-avail="${e.id}"]`);
        cell.innerHTML = `<b>${money(e.available)}</b>`;
        cell.className = "num " + signClass(e.available);
        recompute();
      } catch (err) { fail(err); }
    };
    el.querySelectorAll("[data-alloc]").forEach((i) => {
      i.onchange = () => saveAlloc(i);
      i.onkeydown = (e) => { if (e.key === "Enter") i.blur(); };
    });
    el.querySelectorAll("[data-usual]").forEach((a) => (a.onclick = (ev) => {
      ev.preventDefault();
      const input = el.querySelector(`[data-alloc="${a.dataset.usual}"]`);
      input.value = env[a.dataset.usual].default_amount.toFixed(2);
      saveAlloc(input);
    }));

    el.addEventListener("click", async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      try {
        if (b.dataset.fill) {
          const overwrite = s.allocated > 0 && confirm("Replace amounts you've already entered? (Cancel = only fill empty envelopes)");
          const r = await api("/allocations/fill", { method: "POST", body: { month: m, mode: b.dataset.fill, overwrite } });
          toast(`Filled ${r.filled} envelopes`);
          render();
        }
        if (b.dataset.addinc !== undefined) {
          const amount = parseFloat(el.querySelector("#inc-amt").value.replace(/[$,]/g, ""));
          if (isNaN(amount)) return toast("Enter an amount", { error: true });
          await api("/income", { method: "POST", body: { month: m, amount, date: el.querySelector("#inc-date").value, description: el.querySelector("#inc-desc").value || "Paycheck" } });
          render();
        }
        if (b.dataset.delinc) { await api(`/income/${b.dataset.delinc}`, { method: "DELETE" }); render(); }
        if (b.dataset.addbills !== undefined) {
          const r = await api("/recurring/add-month", { method: "POST", body: { month: m } });
          toast(`Added ${r.added} bills to ${monthLabel(m)}`);
          render();
        }
      } catch (err) { fail(err); }
    });
  },
};
