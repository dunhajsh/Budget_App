import { api, catName, catOptions, esc, fail, hashParams, modal, money, monthLabel, render, state, toast } from "../app.js";

let filter = "all";
let search = "";
let selected = new Set();
let txns = [];

// A spreadsheet entry that no statement line has been matched to yet (no real merchant name).
const unnamed = (t) => t.source === "excel" && / \(spreadsheet\)$/.test(t.description || "");
const titleCase = (s) => (s || "").toLowerCase().replace(/\b([a-z])/g, (c) => c.toUpperCase());
const shortDate = (d) => new Date(d + "T12:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

function catCell(t) {
  if (t.excluded && /^Counted as income/.test(t.notes || "")) return `<span class="badge good" title="${esc(t.notes)}">Counted as income</span>`;
  if (t.excluded) return /^Not in your spreadsheet/.test(t.notes || "")
    ? `<span class="badge warn" title="${esc(t.notes)}">Not in spreadsheet · not budgeted</span>`
    : `<span class="badge">Not budgeted (payment/transfer)</span>`;
  if (t.splits.length > 1) {
    return `<div class="small">${t.splits.map((s) => `<div>${esc(catName(s.category_id))} <span class="muted">${money(s.amount)}</span></div>`).join("")}</div>`;
  }
  const cid = t.splits[0]?.category_id || null;
  return `<select class="cat ${cid ? "" : "uncat"}" data-cat="${t.id}">${catOptions(cid)}</select>`;
}

function row(t) {
  const monthTag = t.date.slice(0, 7) !== t.budget_month ? `<span class="badge" title="Counts toward ${monthLabel(t.budget_month)}">→ ${monthLabel(t.budget_month, true)}</span>` : "";
  const auto = !t.reviewed && !t.excluded && t.splits.length ? `<span class="badge accent" title="Auto-categorized - click ✓ to confirm">auto</span>` : "";
  const receipt = t.receipt_id
    ? `<a class="badge ${t.receipt_count ? "good" : ""}" href="#/receipts?open=${t.receipt_id}" title="${t.receipt_count ? `Itemized from ${t.receipt_count > 1 ? `${t.receipt_count} receipts` : "a receipt"}` : "A receipt is waiting to be applied to this charge"}">🧾 ${t.receipt_count > 1 ? `${t.receipt_count} receipts` : t.receipt_count ? "receipt" : "receipt to apply"}</a>` : "";
  const sheet = t.source === "excel" ? `<span class="badge" title="From your budget spreadsheet${unnamed(t) ? "" : " - merchant name and date from your card/bank statement"}">spreadsheet</span>` : "";
  return `<tr data-id="${t.id}" class="${t.excluded ? "excluded" : ""}">
    <td><input type="checkbox" data-sel="${t.id}" ${selected.has(t.id) ? "checked" : ""}></td>
    <td class="small" style="white-space:nowrap">${shortDate(t.date)}</td>
    <td><div class="desc-main">${unnamed(t) ? `<span class="muted">Spreadsheet entry</span>` : esc(titleCase(t.merchant || t.description))}</div>
      <div class="desc-raw">${unnamed(t) ? "No matching statement line (entered by hand or combined)" : `${esc(t.description)} ${t.statement_category ? `· ${esc(t.statement_category)}` : ""} · ${esc(t.account || "")}`}</div>
      <div class="row" style="gap:4px;margin-top:2px">${sheet}${auto}${receipt}${monthTag}${t.notes ? `<span class="badge" title="${esc(t.notes)}">note</span>` : ""}</div></td>
    <td style="min-width:200px">${catCell(t)}</td>
    <td class="num ${t.amount < 0 ? "good" : ""}">${money(t.amount)}</td>
    <td style="white-space:nowrap">
      ${!t.reviewed && !t.excluded ? `<button class="small ghost" data-review="${t.id}" title="Looks right">✓</button>` : ""}
      ${!t.excluded ? `<button class="small ghost" data-split="${t.id}" title="Split across categories">✂ Split</button>` : ""}
      <button class="small ghost" data-more="${t.id}" title="More">⋯</button>
    </td></tr>`;
}

async function load(el, alive) {
  const p = hashParams();
  const cat = p.get("category");
  const qs = new URLSearchParams({ month: state.month });
  if (filter !== "all") qs.set("filter", filter);
  if (search) qs.set("q", search);
  if (cat) qs.set("category_id", cat);
  txns = await api(`/transactions?${qs}`);
  if (!alive()) return;
  const body = el.querySelector("#txn-body");
  const total = txns.filter((t) => !t.excluded).reduce((a, t) => a + t.amount, 0);
  el.querySelector("#txn-count").textContent = `${txns.length} transactions · ${money(total)}`;
  el.querySelector("#cat-filter").innerHTML = cat
    ? `<span class="badge accent">Category: ${esc(catName(+cat))} <a href="#/transactions" style="margin-left:4px">✕</a></span>` : "";
  body.innerHTML = txns.length ? txns.map(row).join("")
    : `<tr><td colspan="6"><div class="empty"><div class="big">📭</div>Nothing here for ${monthLabel(state.month)}.
        ${filter === "all" && !search ? `<br><br><button class="primary" data-import>Import a statement</button>` : ""}</div></td></tr>`;
  updateBulk(el);
}

function updateBulk(el) {
  const bar = el.querySelector("#bulkbar");
  bar.classList.toggle("hidden", !selected.size);
  el.querySelector("#bulk-n").textContent = `${selected.size} selected`;
}

async function afterCategorize(t, cid) {
  try {
    const sim = await api(`/transactions/${t.id}/similar`);
    const name = titleCase(sim.merchant || t.description);
    const label = catName(cid);
    const actions = [{
      label: sim.count ? `Yes, and update ${sim.count} more` : "Always",
      onClick: async () => {
        const r = await api(`/transactions/${t.id}`, { method: "PATCH", body: { category_id: cid, remember: true, apply_similar: true } });
        toast(`Rule saved: ${name} → ${label}${r.applied_to ? ` (${r.applied_to} updated)` : ""}`);
        render();
      },
    }];
    toast(`Always put “${name}” in ${label}?`, { actions, timeout: 6000 });
  } catch { /* non-critical */ }
}

function splitEditor(t) {
  let rows = t.splits.length ? t.splits.map((s) => ({ ...s })) : [{ category_id: null, amount: t.amount, memo: "" }];
  if (rows.length === 1) rows.push({ category_id: null, amount: 0, memo: "" });
  const draw = (root) => {
    const used = rows.reduce((a, r) => a + (parseFloat(r.amount) || 0), 0);
    const left = Math.round((t.amount - used) * 100) / 100;
    root.querySelector("#split-rows").innerHTML = rows.map((r, i) => `<tr>
        <td><select class="cat" data-i="${i}" data-f="category_id" style="max-width:none">${catOptions(r.category_id)}</select></td>
        <td><input class="money" data-i="${i}" data-f="amount" value="${r.amount}"></td>
        <td><input data-i="${i}" data-f="memo" value="${esc(r.memo || "")}" placeholder="note"></td>
        <td><button class="ghost small" data-del="${i}">✕</button></td></tr>`).join("");
    const rem = root.querySelector("#split-left");
    rem.textContent = left === 0 ? "✓ Fully assigned" : `${money(left)} ${left > 0 ? "left to assign" : "over"}`;
    rem.className = left === 0 ? "good" : "warn";
    root.querySelector("[data-save]").disabled = left !== 0;
  };
  modal({
    title: `Split ${money(t.amount)} · ${titleCase(t.merchant || t.description)}`,
    body: `<p class="muted small" style="margin-top:0">Divide one charge across envelopes - e.g. a Meijer trip that was groceries <i>and</i> toiletries.
        Or <a href="#/receipts">upload the receipt</a> and it will be split for you.</p>
      <table><thead><tr><th>Category</th><th class="num">Amount</th><th>Note</th><th></th></tr></thead><tbody id="split-rows"></tbody></table>
      <div class="row" style="margin-top:10px"><button class="small" data-add>＋ Add row</button><button class="small" data-rest>Put remainder in last row</button>
        <span class="spacer"></span><b id="split-left"></b></div>`,
    foot: `<button data-close>Cancel</button><button class="primary" data-save>Save split</button>`,
    onMount(root, close) {
      draw(root);
      root.addEventListener("input", (e) => {
        const i = e.target.dataset.i;
        if (i === undefined) return;
        const f = e.target.dataset.f;
        rows[i][f] = f === "category_id" ? (e.target.value ? +e.target.value : null) : e.target.value;
        if (f === "amount") {
          const used = rows.reduce((a, r) => a + (parseFloat(r.amount) || 0), 0);
          const left = Math.round((t.amount - used) * 100) / 100;
          const rem = root.querySelector("#split-left");
          rem.textContent = left === 0 ? "✓ Fully assigned" : `${money(left)} ${left > 0 ? "left to assign" : "over"}`;
          rem.className = left === 0 ? "good" : "warn";
          root.querySelector("[data-save]").disabled = left !== 0;
        }
      });
      root.addEventListener("click", async (e) => {
        if (e.target.dataset.del !== undefined) { rows.splice(+e.target.dataset.del, 1); draw(root); }
        if (e.target.matches("[data-add]")) { rows.push({ category_id: null, amount: 0, memo: "" }); draw(root); }
        if (e.target.matches("[data-rest]")) {
          const others = rows.slice(0, -1).reduce((a, r) => a + (parseFloat(r.amount) || 0), 0);
          rows[rows.length - 1].amount = Math.round((t.amount - others) * 100) / 100;
          draw(root);
        }
        if (e.target.matches("[data-save]")) {
          try {
            await api(`/transactions/${t.id}/splits`, { method: "PUT", body: { splits: rows.map((r) => ({ ...r, amount: parseFloat(r.amount) || 0 })) } });
            close(); toast("Split saved"); render();
          } catch (err) { fail(err); }
        }
      });
    },
  });
}

function moreMenu(t) {
  modal({
    title: titleCase(t.merchant || t.description),
    body: `<div class="stack">
      <div class="muted small">${esc(t.description)} · ${t.date} · ${esc(t.account || "")} · ${money(t.amount)}</div>
      <label class="field">Counts toward budget month<input type="month" name="budget_month" value="${t.budget_month}"></label>
      <label class="field">Note<input name="notes" value="${esc(t.notes || "")}" placeholder="e.g. Birthday gift for Mom"></label>
      <label class="row"><input type="checkbox" name="excluded" ${t.excluded ? "checked" : ""}> Not part of the budget (card payment, transfer, reimbursed)</label>
      ${t.amount < 0 && t.source === "statement" ? `<div class="small muted">A deposit that's really income (side job, gift)? <button class="small" data-income>Count as income</button></div>` : ""}
    </div>`,
    foot: `${t.source === "manual" || t.source === "recurring" ? `<button class="danger" data-del>Delete</button>` : ""}<span class="spacer"></span>
           <button data-close>Cancel</button><button class="primary" data-save>Save</button>`,
    onMount(root, close) {
      root.querySelector("[data-save]").onclick = async () => {
        try {
          await api(`/transactions/${t.id}`, { method: "PATCH", body: {
            budget_month: root.querySelector("[name=budget_month]").value,
            notes: root.querySelector("[name=notes]").value,
            excluded: root.querySelector("[name=excluded]").checked,
          } });
          close(); render();
        } catch (e) { fail(e); }
      };
      const inc = root.querySelector("[data-income]");
      if (inc) inc.onclick = async () => {
        try {
          const r = await api(`/transactions/${t.id}/to-income`, { method: "POST", body: {} });
          toast(`Added to ${monthLabel(r.month)} income`); close(); render();
        } catch (e) { fail(e); }
      };
      const del = root.querySelector("[data-del]");
      if (del) del.onclick = async () => {
        if (!confirm("Delete this transaction?")) return;
        await api(`/transactions/${t.id}`, { method: "DELETE" }); close(); render();
      };
    },
  });
}

export function importDialog() {
  modal({
    title: "Import statements",
    wide: true,
    body: `<div class="stack">
      <div class="dropzone" id="dz"><div class="big">📄</div><b>Drop card statements or bank account CSVs here</b><br>or click to choose · several at once is fine
        <input type="file" accept=".csv,text/csv" multiple hidden></div>
      <div id="files"></div>
      <p class="small muted" style="margin:0">Re-importing the same file is safe - duplicates are skipped.
        Every purchase counts in the month it was made. <b>Bank accounts</b>: bills count in the month they're paid, paychecks and interest go to
        income, transfers between your accounts and card payments are left out, other deposits (refunds, reimbursements) show up as credits to categorize.
        In months your spreadsheet already covers, statement lines take the place of the matching spreadsheet entries (keeping their envelopes),
        so those months still add up exactly to the spreadsheet; lines the spreadsheet doesn't have are shown but not budgeted.</p>
    </div>`,
    foot: `<button data-close>Cancel</button><button class="primary" data-go disabled>Import</button>`,
    onMount(root, close) {
      const files = [];
      const dz = root.querySelector("#dz");
      const input = dz.querySelector("input");
      const list = root.querySelector("#files");
      const draw = () => {
        list.innerHTML = files.length ? `<div class="table-wrap"><table><thead><tr><th>File</th><th>What's in it</th><th>Account</th></tr></thead><tbody>
          ${files.map((f, i) => {
            const p = f.p;
            if (!p) return `<tr><td>${esc(f.file.name)}</td><td colspan="2">${f.err ? `<span class="bad">${esc(f.err)}</span>` : `<span class="spinner"></span>`}</td></tr>`;
            const bits = [`${p.count} rows, ${p.first} → ${p.last}`];
            if (p.kind === "bank") {
              if (p.income) bits.push(`${p.income} paycheck/interest`);
              if (p.credits) bits.push(`${p.credits} other deposits`);
              if (p.transfers + p.payments) bits.push(`${p.transfers + p.payments} transfers/card payments left out`);
            } else if (p.payments) bits.push(`${p.payments} card payment(s) left out`);
            if (p.in_history) bits.push(`<span class="accent">${p.in_history} in months your spreadsheet covers (through ${monthLabel(p.history_through, true)}) - these replace the matching spreadsheet entries; month totals stay the same</span>`);
            return `<tr><td><b>${esc(f.file.name)}</b><div class="small muted">${p.kind === "bank" ? "🏦 Bank account" : "💳 Card statement"}</div></td>
              <td class="small">${bits.join(" · ")}</td>
              <td><input data-acct="${i}" value="${esc(f.account)}" list="acct" style="width:170px"></td></tr>`;
          }).join("")}</tbody></table></div>
          <datalist id="acct">${(state.status.accounts || []).filter((a) => a !== "Spreadsheet").map((a) => `<option value="${esc(a)}">`).join("")}</datalist>` : "";
        list.querySelectorAll("[data-acct]").forEach((x) => (x.oninput = () => { files[+x.dataset.acct].account = x.value; }));
        list.querySelectorAll("[data-month]").forEach((x) => (x.oninput = () => { files[+x.dataset.month].month = x.value; }));
        root.querySelector("[data-go]").disabled = !files.some((f) => f.p);
      };
      const add = async (picked) => {
        for (const file of picked) {
          const f = { file };
          files.push(f);
          draw();
          const fd = new FormData();
          fd.append("file", file);
          try {
            f.p = await api("/import/statement/preview", { method: "POST", form: fd });
            f.account = f.p.account;
            f.month = f.p.budget_month;
          } catch (e) { f.err = e.message; }
          draw();
        }
      };
      dz.onclick = () => input.click();
      input.onchange = () => add([...input.files]);
      dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("drag"); };
      dz.ondragleave = () => dz.classList.remove("drag");
      dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("drag"); add([...e.dataTransfer.files]); };
      root.querySelector("[data-go]").onclick = async (ev) => {
        ev.target.disabled = true;
        ev.target.innerHTML = `<span class="spinner"></span> Importing`;
        // Card statements first, oldest first: what's learned from older files helps the newer ones.
        const todo = files.filter((f) => f.p).sort((a, b) => (a.p.kind === b.p.kind ? (a.p.first < b.p.first ? -1 : 1) : a.p.kind === "card" ? -1 : 1));
        const results = [];
        try {
          for (const f of todo) {
            const fd = new FormData();
            fd.append("file", f.file);
            fd.append("account", f.account || "");
            results.push({ name: f.file.name, ...(await api("/import/statement", { method: "POST", form: fd })) });
          }
        } catch (e) { fail(e); }
        close();
        if (results.length) showResults(results);
        render();
      };
    },
  });
}

function showResults(results) {
  const sum = (k) => results.reduce((a, r) => a + (r[k] || 0), 0);
  const income = results.flatMap((r) => r.income_added || []);
  const rules = results.flatMap((r) => r.rules_learned || []);
  const months = {};
  results.forEach((r) => Object.entries(r.months || (r.kind === "card" && r.imported ? { [r.budget_month]: r.imported } : {}))
    .forEach(([m, n]) => (months[m] = (months[m] || 0) + n)));
  const incByMonth = {};
  income.forEach((i) => (incByMonth[i.month] = (incByMonth[i.month] || 0) + i.amount));
  modal({
    title: "Import finished",
    body: `<div class="stack">
      <div class="kpis">
        <div class="kpi"><div class="label">New transactions</div><div class="value">${sum("imported")}</div>
          <div class="sub">${Object.entries(months).sort().map(([m, n]) => `${monthLabel(m, true)}: ${n}`).join(" · ") || "-"}</div></div>
        <div class="kpi"><div class="label">Auto-categorized</div><div class="value">${sum("auto_categorized")}</div><div class="sub">${sum("left_out")} transfers/payments left out</div></div>
        <div class="kpi"><div class="label">Income added</div><div class="value">${money(income.reduce((a, i) => a + i.amount, 0))}</div>
          <div class="sub">${Object.entries(incByMonth).sort().map(([m, a]) => `${monthLabel(m, true)}: ${money(a)}`).join(" · ") || "-"}</div></div>
        <div class="kpi"><div class="label">History matched</div><div class="value">${sum("history_matched")}</div><div class="sub">spreadsheet entries now have merchant names</div></div>
      </div>
      ${(() => {
        const h = {};
        results.forEach((r) => Object.entries(r.history || {}).forEach(([k, n]) => (h[k] = (h[k] || 0) + n)));
        const errs = results.filter((r) => r.history_error);
        if (!Object.keys(h).length && !errs.length) return "";
        return `<div><b>Months your spreadsheet covers</b> - statement lines now fill them in, and every envelope still matches the spreadsheet:
          <div class="small muted" style="margin-top:4px">${[["exact", "matched an entry exactly"], ["split", "split across several entries"], ["combined", "combined into one entry"],
            ["near", "matched an entry with a small typo (difference kept as its own line)"], ["pairs", "purchase + return that cancel out"],
            ["not_in_sheet", "not in the spreadsheet - shown, but not budgeted"], ["left_out", "transfers/card payments"]]
            .filter(([k]) => h[k]).map(([k, l]) => `${h[k]} ${l}`).join(" · ")}</div>
          ${errs.map((r) => `<div class="small bad">${esc(r.name)}: ${esc(r.history_error)}</div>`).join("")}</div>`;
      })()}
      ${rules.length ? `<div><b>Learned ${rules.length} merchant rules from your spreadsheet history</b>${sum("recategorized") ? ` (and re-sorted ${sum("recategorized")} unreviewed transactions)` : ""}:
        <div class="small muted" style="margin-top:4px;columns:2">${rules.map((r) => `${esc(r.pattern)} → ${esc(r.category)}`).join("<br>")}</div></div>` : ""}
      ${!sum("imported") && !income.length && results.every((r) => r.kind === "card") ? `<div class="alert info"><span class="ico">ℹ️</span><span>
        ${results.map((r) => monthLabel(r.budget_month)).join(", ")} ${results.length > 1 ? "are" : "is"} already in your budget from your spreadsheet, so nothing was added
        (that would count every purchase twice). ${sum("history_matched") ? `${sum("history_matched")} spreadsheet entries now show the card's merchant names and dates.` : "The spreadsheet entries were already matched to this statement earlier."}
        You'll find them on the Transactions page under that month, marked <b>spreadsheet</b>.</span></div>` : ""}
      ${sum("duplicates") ? `<div class="small muted">${sum("duplicates")} rows were already imported or matched earlier and were skipped.</div>` : ""}
      ${sum("history_skipped") ? `<div class="small muted">${sum("history_skipped")} rows fall in months your spreadsheet already covers but didn't match an entry there (e.g. card payments, transfers, or entries you combined) - they weren't added.</div>` : ""}
      <table class="small">${results.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.account || "")}</td><td class="num">${r.imported} new</td><td class="num">${r.history_matched} matched to history</td></tr>`).join("")}</table>
    </div>`,
    foot: `<button data-close>Close</button><button class="primary" data-go>${sum("imported") ? "Review new transactions" : "Show transactions"}</button>`,
    onMount(root, close) {
      root.querySelector("[data-go]").onclick = () => {
        const latest = Object.keys(months).sort().pop() || results.map((r) => r.budget_month).filter(Boolean).sort().pop();
        if (latest && latest !== state.month) { state.month = latest; localStorage.setItem("month", latest); }
        close();
        location.hash = sum("imported") ? "#/transactions?filter=uncategorized" : "#/transactions?filter=all";
        render();
      };
    },
  });
}

export default {
  async render(el, alive) {
    const p = hashParams();
    if (p.get("filter")) filter = p.get("filter");
    selected = new Set();
    el.innerHTML = `<div class="panel">
      <div class="panel-head">
        <div class="chips" id="chips">
          ${[["all", "All"], ["uncategorized", "Needs category"], ["review", "Needs review"], ["split", "Split"]]
            .map(([k, l]) => `<span class="chip ${filter === k ? "active" : ""}" data-f="${k}">${l}</span>`).join("")}
        </div>
        <span id="cat-filter"></span>
        <input id="search" placeholder="Search…" value="${esc(search)}" style="width:180px">
        <span class="spacer"></span>
        <button data-rules title="Re-run your merchant rules on uncategorized transactions">⚡ Run rules</button>
        ${state.status.ai_enabled ? `<button data-ai title="Ask the AI to suggest categories for the rest">✨ Suggest categories</button>` : ""}
        <button data-reviewall>✓ Mark all reviewed</button>
        <button class="primary" data-import>⬆ Import statement</button>
      </div>
      <div class="muted small" id="txn-count" style="margin-bottom:8px"></div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:28px"><input type="checkbox" id="sel-all"></th><th>Date</th><th>Description</th><th>Category</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody id="txn-body"><tr><td colspan="6" class="empty"><span class="spinner"></span></td></tr></tbody>
      </table></div>
      <div class="bulkbar hidden" id="bulkbar">
        <b id="bulk-n"></b>
        <select class="cat" id="bulk-cat" style="max-width:240px">${catOptions(null, { empty: "Set category…" })}</select>
        <button class="small" data-bulk="review">Mark reviewed</button>
        <button class="small" data-bulk="exclude">Exclude from budget</button>
        <span class="spacer"></span>
        <button class="small" data-bulk="clear">Clear</button>
      </div>
    </div>`;

    el.querySelector("#chips").onclick = (e) => {
      const f = e.target.dataset.f;
      if (!f) return;
      filter = f;
      el.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.f === f));
      load(el, alive);
    };
    let t;
    el.querySelector("#search").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { search = e.target.value; load(el, alive); }, 250); };

    el.addEventListener("change", async (e) => {
      const tgt = e.target;
      if (tgt.dataset.cat) {
        const tx = txns.find((x) => x.id === +tgt.dataset.cat);
        const cid = tgt.value ? +tgt.value : null;
        try {
          await api(`/transactions/${tx.id}`, { method: "PATCH", body: { category_id: cid } });
          tx.splits = cid ? [{ category_id: cid, amount: tx.amount }] : [];
          tx.reviewed = 1;
          tgt.classList.toggle("uncat", !cid);
          tgt.closest("tr").querySelector("[data-review]")?.remove();
          tgt.closest("tr").querySelector(".badge.accent")?.remove();
          if (cid && !unnamed(tx)) afterCategorize(tx, cid);
          refreshBadge();
        } catch (err) { fail(err); }
      }
      if (tgt.dataset.sel) {
        tgt.checked ? selected.add(+tgt.dataset.sel) : selected.delete(+tgt.dataset.sel);
        updateBulk(el);
      }
      if (tgt.id === "sel-all") {
        selected = new Set(tgt.checked ? txns.map((x) => x.id) : []);
        el.querySelectorAll("[data-sel]").forEach((c) => (c.checked = tgt.checked));
        updateBulk(el);
      }
      if (tgt.id === "bulk-cat" && tgt.value) {
        await api("/transactions/bulk", { method: "POST", body: { ids: [...selected], category_id: +tgt.value } });
        toast(`Categorized ${selected.size} transactions`);
        render();
      }
    });

    el.addEventListener("click", async (e) => {
      const b = e.target.closest("button, [data-import]");
      if (!b) return;
      const tx = (k) => txns.find((x) => x.id === +b.dataset[k]);
      try {
        if (b.dataset.import !== undefined) return importDialog();
        if (b.dataset.split) return splitEditor(tx("split"));
        if (b.dataset.more) return moreMenu(tx("more"));
        if (b.dataset.review) {
          await api(`/transactions/${b.dataset.review}`, { method: "PATCH", body: { reviewed: true } });
          b.closest("tr").querySelector(".badge.accent")?.remove();
          b.remove();
          return refreshBadge();
        }
        if (b.dataset.reviewall !== undefined) {
          const ids = txns.filter((x) => !x.reviewed && x.splits.length).map((x) => x.id);
          await api("/transactions/bulk", { method: "POST", body: { ids, reviewed: true } });
          toast(`Marked ${ids.length} reviewed`);
          return render();
        }
        if (b.dataset.rules !== undefined) {
          const r = await api("/rules/apply", { method: "POST", body: { month: state.month } });
          toast(`Rules categorized ${r.categorized} transactions`);
          return render();
        }
        if (b.dataset.ai !== undefined) {
          b.disabled = true;
          b.innerHTML = `<span class="spinner"></span> Thinking…`;
          const r = await api("/transactions/ai-categorize", { method: "POST", body: { month: state.month } });
          toast(`Suggested categories for ${r.categorized} of ${r.total} - they're marked “auto” for review`);
          filter = "review";
          return render();
        }
        if (b.dataset.bulk) {
          const ids = [...selected];
          if (b.dataset.bulk === "clear") { selected.clear(); el.querySelectorAll("[data-sel]").forEach((c) => (c.checked = false)); return updateBulk(el); }
          await api("/transactions/bulk", { method: "POST", body: b.dataset.bulk === "review" ? { ids, reviewed: true } : { ids, excluded: true } });
          return render();
        }
      } catch (err) { fail(err); b.disabled = false; }
    });

    await load(el, alive);
  },
};

async function refreshBadge() {
  const s = await api(`/summary?month=${state.month}`);
  const badge = document.getElementById("nav-review");
  const n = s.to_review + s.uncategorized_count;
  badge.textContent = n;
  badge.classList.toggle("hidden", !n);
}
