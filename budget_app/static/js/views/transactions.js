import { api, catName, catOptions, esc, fail, hashParams, modal, money, monthLabel, render, state, toast } from "../app.js";

let filter = "all";
let search = "";
let selected = new Set();
let txns = [];

const titleCase = (s) => (s || "").toLowerCase().replace(/\b([a-z])/g, (c) => c.toUpperCase());
const shortDate = (d) => new Date(d + "T12:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

function catCell(t) {
  if (t.excluded) return `<span class="badge">Not budgeted (payment/transfer)</span>`;
  if (t.splits.length > 1) {
    return `<div class="small">${t.splits.map((s) => `<div>${esc(catName(s.category_id))} <span class="muted">${money(s.amount)}</span></div>`).join("")}</div>`;
  }
  const cid = t.splits[0]?.category_id || null;
  return `<select class="cat ${cid ? "" : "uncat"}" data-cat="${t.id}">${catOptions(cid)}</select>`;
}

function row(t) {
  const monthTag = t.date.slice(0, 7) !== t.budget_month ? `<span class="badge" title="Counts toward ${monthLabel(t.budget_month)}">→ ${monthLabel(t.budget_month, true)}</span>` : "";
  const auto = !t.reviewed && !t.excluded && t.splits.length ? `<span class="badge accent" title="Auto-categorized - click ✓ to confirm">auto</span>` : "";
  const receipt = t.receipt_id ? `<a class="badge good" href="#/receipts?open=${t.receipt_id}" title="Itemized from a receipt">🧾 receipt</a>` : "";
  return `<tr data-id="${t.id}" class="${t.excluded ? "excluded" : ""}">
    <td><input type="checkbox" data-sel="${t.id}" ${selected.has(t.id) ? "checked" : ""}></td>
    <td class="small" style="white-space:nowrap">${shortDate(t.date)}</td>
    <td><div class="desc-main">${esc(titleCase(t.merchant || t.description))}</div>
      <div class="desc-raw">${esc(t.description)} ${t.statement_category ? `· ${esc(t.statement_category)}` : ""} · ${esc(t.account || "")}</div>
      <div class="row" style="gap:4px;margin-top:2px">${auto}${receipt}${monthTag}${t.notes ? `<span class="badge" title="${esc(t.notes)}">note</span>` : ""}</div></td>
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
    </div>`,
    foot: `${t.source !== "statement" ? `<button class="danger" data-del>Delete</button>` : ""}<span class="spacer"></span>
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
      const del = root.querySelector("[data-del]");
      if (del) del.onclick = async () => {
        if (!confirm("Delete this transaction?")) return;
        await api(`/transactions/${t.id}`, { method: "DELETE" }); close(); render();
      };
    },
  });
}

export function importDialog() {
  const accounts = (state.status.accounts || []).filter((a) => a !== "Spreadsheet");
  modal({
    title: "Import a statement",
    body: `<div class="stack">
      <div class="dropzone" id="dz"><div class="big">📄</div><b>Drop your statement CSV here</b><br>or click to choose a file
        <input type="file" accept=".csv,text/csv" hidden></div>
      <div id="preview" class="muted small"></div>
      <div class="grid grid-2">
        <label class="field">Account name<input name="account" list="acct" value="${esc(accounts[0] || "SoFi Credit Card")}">
          <datalist id="acct">${accounts.map((a) => `<option value="${esc(a)}">`).join("")}</datalist></label>
        <label class="field">Counts toward budget month<input type="month" name="budget_month" value="${state.month}"></label>
      </div>
      <p class="small muted" style="margin:0">Re-importing the same file is safe - duplicates are skipped. Card payments are detected and left out of the budget.
        Most banks have a "Download transactions → CSV" option; SoFi, Chase, Capital One, Discover, Citi and Amex formats are recognized.</p>
    </div>`,
    foot: `<button data-close>Cancel</button><button class="primary" data-go disabled>Import</button>`,
    onMount(root, close) {
      let file = null;
      const dz = root.querySelector("#dz");
      const input = dz.querySelector("input");
      const pick = async (f) => {
        file = f;
        const fd = new FormData();
        fd.append("file", f);
        try {
          const p = await api("/import/statement/preview", { method: "POST", form: fd });
          root.querySelector("#preview").innerHTML = `<b>${esc(f.name)}</b>: ${p.count} transactions from ${p.first} to ${p.last},
            ${money(p.total_spent)} of spending${p.payments ? `, ${p.payments} card payment(s) skipped` : ""}.`;
          root.querySelector("[name=budget_month]").value = p.budget_month;
          root.querySelector("[data-go]").disabled = false;
        } catch (e) { root.querySelector("#preview").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
      };
      dz.onclick = () => input.click();
      input.onchange = () => input.files[0] && pick(input.files[0]);
      dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("drag"); };
      dz.ondragleave = () => dz.classList.remove("drag");
      dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("drag"); e.dataTransfer.files[0] && pick(e.dataTransfer.files[0]); };
      root.querySelector("[data-go]").onclick = async (ev) => {
        ev.target.disabled = true;
        ev.target.innerHTML = `<span class="spinner"></span> Importing`;
        const fd = new FormData();
        fd.append("file", file);
        fd.append("account", root.querySelector("[name=account]").value);
        fd.append("budget_month", root.querySelector("[name=budget_month]").value);
        try {
          const r = await api("/import/statement", { method: "POST", form: fd });
          close();
          toast(`Imported ${r.imported} transactions (${r.auto_categorized} auto-categorized${r.duplicates ? `, ${r.duplicates} duplicates skipped` : ""})`);
          if (r.budget_month && r.budget_month !== state.month) { state.month = r.budget_month; localStorage.setItem("month", r.budget_month); }
          location.hash = "#/transactions?filter=uncategorized";
          render();
        } catch (e) { fail(e); ev.target.disabled = false; ev.target.textContent = "Import"; }
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
          if (cid) afterCategorize(tx, cid);
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
