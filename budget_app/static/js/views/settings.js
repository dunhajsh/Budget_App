import { api, catOptions, esc, fail, hashParams, loadCategories, money, render, state, toast } from "../app.js";
import { importDialog } from "./transactions.js";

const TABS = [
  ["import", "Import & data"], ["categories", "Categories"], ["rules", "Merchant rules"],
  ["cardmap", "Card categories"], ["recurring", "Recurring bills"], ["ai", "AI & receipts"],
];
let tab = "import";

async function importTab(el) {
  const batches = await api("/import/batches");
  const welcome = hashParams().get("welcome");
  el.innerHTML = `<div class="stack">
    ${welcome || !state.status.has_categories ? `<div class="alert info"><span class="ico">👋</span><span><b>Welcome!</b> Start by importing your existing budget
      spreadsheet - your envelopes, their usual amounts, every past month, and the balances you've carried over all come across.</span></div>` : ""}
    <div class="grid grid-2">
      <div class="panel"><h2>Budget spreadsheet (.xlsx)</h2>
        <p class="small muted">One sheet per year (named "2024", "2025"…), with categories down column A and Spent/Total columns per month.
          Each amount inside a cell formula like <code>=54.47+252.96</code> becomes its own history entry.</p>
        <div class="dropzone" id="xz"><div class="big">📊</div><b>Drop the workbook</b> or click<input type="file" accept=".xlsx,.xlsm" hidden></div>
        <label class="row small" style="margin-top:8px"><input type="checkbox" id="xreplace" checked> Replace history previously imported from a spreadsheet</label>
      </div>
      <div class="panel"><h2>Card statements</h2>
        <p class="small muted">Download your statement as CSV from your card's website and import it - every purchase arrives pre-sorted using your rules.</p>
        <button class="primary" data-stmt>⬆ Import statement</button>
        <h3 style="margin-top:18px">Import history</h3>
        <table>${batches.filter((b) => b.kind === "statement").map((b) => `<tr><td>${esc(b.filename)}<div class="small muted">${b.created_at} · ${b.row_count} rows · ${b.budget_month}</div></td>
          <td><button class="small danger" data-undo="${b.id}">Undo</button></td></tr>`).join("") || `<tr><td class="muted small">No statements imported yet.</td></tr>`}</table>
      </div>
    </div>
    <div class="panel"><h2>Backup</h2>
      <p class="small muted">Everything lives in one file: <code>data/budget.db</code> inside the app folder. Download a copy now and then.</p>
      <a class="btn" href="/api/export/backup.db">⬇ Download backup</a>
      <a class="btn" href="/api/export/year/${state.month.slice(0, 4)}.xlsx">⬇ Export ${state.month.slice(0, 4)} to Excel</a>
    </div>
  </div>`;
  const dz = el.querySelector("#xz");
  const input = dz.querySelector("input");
  const go = async (f) => {
    dz.innerHTML = `<span class="spinner"></span> Importing ${esc(f.name)}…`;
    const fd = new FormData();
    fd.append("file", f);
    fd.append("replace", el.querySelector("#xreplace").checked ? "1" : "0");
    try {
      const r = await api("/import/excel", { method: "POST", form: fd });
      toast(`Imported ${r.years.length} years: ${r.transactions} history entries, ${r.income_entries} paychecks`);
      await loadCategories();
      location.hash = "#/dashboard";
    } catch (e) { fail(e); render(); }
  };
  dz.onclick = () => input.click();
  input.onchange = () => input.files[0] && go(input.files[0]);
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("drag"); };
  dz.ondragleave = () => dz.classList.remove("drag");
  dz.ondrop = (e) => { e.preventDefault(); e.dataTransfer.files[0] && go(e.dataTransfer.files[0]); };
  el.querySelector("[data-stmt]").onclick = importDialog;
  el.querySelectorAll("[data-undo]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Remove every transaction from this import?")) return;
    const r = await api(`/import/batches/${b.dataset.undo}`, { method: "DELETE" });
    toast(`Removed ${r.deleted} transactions`);
    render();
  }));
}

async function categoriesTab(el) {
  const cats = await api("/categories?archived=1");
  const groups = [...new Set(cats.map((c) => c.grp))];
  el.innerHTML = `<div class="panel">
    <div class="panel-head"><h2>Envelopes & subcategories</h2><span class="spacer"></span>
      <input id="new-name" placeholder="New envelope name"><button class="primary" data-new>Add envelope</button></div>
    <p class="small muted" style="margin-top:0">Envelopes hold money. Subcategories (e.g. Grocery › Produce) roll up into their envelope and show you
      what the money went to. Receipt items are sorted into subcategories automatically.</p>
    <datalist id="groups">${groups.map((g) => `<option value="${esc(g)}">`).join("")}</datalist>
    <div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Group</th><th class="num">Usual monthly</th><th></th></tr></thead>
      <tbody>${cats.map((p) => `
        <tr class="${p.archived ? "excluded" : ""}"><td><input data-f="name" data-id="${p.id}" value="${esc(p.name)}" style="font-weight:600;width:100%"></td>
          <td><input data-f="grp" data-id="${p.id}" value="${esc(p.grp)}" list="groups"></td>
          <td class="num"><input class="money" data-f="default_amount" data-id="${p.id}" value="${p.default_amount.toFixed(2)}"></td>
          <td style="white-space:nowrap"><button class="small" data-sub="${p.id}">＋ Sub</button>
            <button class="small ghost" data-arch="${p.id}" data-v="${p.archived ? 0 : 1}">${p.archived ? "Restore" : "Archive"}</button></td></tr>
        ${p.children.map((c) => `<tr class="tree-child ${c.archived ? "excluded" : ""}"><td>↳ <input data-f="name" data-id="${c.id}" value="${esc(c.name)}"></td><td></td><td></td>
          <td><button class="small ghost danger" data-del="${c.id}">Remove</button></td></tr>`).join("")}`).join("")}
      </tbody></table></div></div>`;
  el.addEventListener("change", async (e) => {
    const i = e.target;
    if (!i.dataset.f) return;
    const v = i.dataset.f === "default_amount" ? parseFloat(i.value) || 0 : i.value;
    try { await api(`/categories/${i.dataset.id}`, { method: "PATCH", body: { [i.dataset.f]: v } }); await loadCategories(); toast("Saved"); }
    catch (err) { fail(err); }
  });
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    try {
      if (b.dataset.new !== undefined) {
        const name = el.querySelector("#new-name").value.trim();
        if (!name) return;
        await api("/categories", { method: "POST", body: { name } });
      } else if (b.dataset.sub) {
        const name = prompt("Subcategory name:");
        if (!name) return;
        await api("/categories", { method: "POST", body: { name, parent_id: +b.dataset.sub } });
      } else if (b.dataset.arch) {
        await api(`/categories/${b.dataset.arch}`, { method: "PATCH", body: { archived: +b.dataset.v } });
      } else if (b.dataset.del) {
        const r = await api(`/categories/${b.dataset.del}`, { method: "DELETE" });
        if (r.message) toast(r.message);
      } else return;
      await loadCategories();
      render();
    } catch (err) { fail(err); }
  });
}

async function rulesTab(el) {
  const rules = await api("/rules");
  el.innerHTML = `<div class="panel">
    <div class="panel-head"><h2>Merchant rules</h2><span class="muted small">${rules.length} rules</span></div>
    <p class="small muted" style="margin-top:0">When a card description contains the text, it goes to that category. Rules are created automatically when
      you answer “Always put … in …?” on the Transactions page. Longer (more specific) rules win - e.g. “MEIJER EXPRESS” beats “MEIJER”.</p>
    <div class="row" style="margin-bottom:12px"><input id="r-pat" placeholder="Description contains… e.g. ULTA" style="flex:1">
      <select id="r-cat" class="cat">${catOptions(null, { empty: "Category…" })}</select><button class="primary" data-add>Add rule</button></div>
    <div class="table-wrap"><table><thead><tr><th>When description contains</th><th>Category</th><th class="num">Used</th><th></th></tr></thead><tbody>
      ${rules.map((r) => `<tr><td><code>${esc(r.pattern)}</code></td>
        <td><select class="cat" data-rcat="${r.id}">${catOptions(r.category_id, { empty: null })}</select></td>
        <td class="num muted">${r.hits}</td><td><button class="small ghost danger" data-del="${r.id}">✕</button></td></tr>`).join("") ||
        `<tr><td colspan="4" class="muted small">No rules yet.</td></tr>`}
    </tbody></table></div></div>`;
  el.querySelector("[data-add]").onclick = async () => {
    const pattern = el.querySelector("#r-pat").value.trim();
    const cid = el.querySelector("#r-cat").value;
    if (!pattern || !cid) return toast("Enter text and pick a category", { error: true });
    try { await api("/rules", { method: "POST", body: { pattern, category_id: +cid } }); render(); } catch (e) { fail(e); }
  };
  el.querySelectorAll("[data-rcat]").forEach((s) => (s.onchange = () => api(`/rules/${s.dataset.rcat}`, { method: "PATCH", body: { category_id: +s.value } }).then(() => toast("Saved"))));
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => { await api(`/rules/${b.dataset.del}`, { method: "DELETE" }); render(); }));
}

async function cardmapTab(el) {
  const items = await api("/statement-map");
  el.innerHTML = `<div class="panel">
    <h2>Card categories → envelopes</h2>
    <p class="small muted">Your card labels each purchase (Groceries, Dining & Drinks…). When no merchant rule matches, this mapping is used as a
      starting guess - those transactions are marked “auto” so you can confirm them.</p>
    ${items.length ? `<table><tbody>${items.map((i) => `<tr><td>${esc(i.statement_category)}</td>
      <td><select class="cat" data-sc="${esc(i.statement_category)}">${catOptions(i.category_id, { empty: "— No default —" })}</select></td></tr>`).join("")}</tbody></table>`
      : `<div class="muted small">Import a statement first - its categories will show up here.</div>`}
  </div>`;
  el.querySelectorAll("[data-sc]").forEach((s) => (s.onchange = async () => {
    await api("/statement-map", { method: "PUT", body: { items: [{ statement_category: s.dataset.sc, category_id: s.value ? +s.value : null }] } });
    toast("Saved - use “Run rules” on Transactions to apply it to uncategorized items");
  }));
}

async function recurringTab(el) {
  const [list, sugg] = await Promise.all([api(`/recurring?month=${state.month}`), api("/recurring/suggestions")]);
  el.innerHTML = `<div class="stack"><div class="panel">
    <h2>Recurring bills</h2>
    <p class="small muted">Bills paid outside the credit card (mortgage, car payment, giving, loans). Add them to a month with one click on the Budget page.</p>
    <table><thead><tr><th>Name</th><th>Category</th><th class="num">Amount</th><th class="num">Day</th><th>Paid from</th><th>Active</th><th></th></tr></thead><tbody>
      ${list.map((r) => `<tr>
        <td><input data-rf="name" data-id="${r.id}" value="${esc(r.name)}"></td>
        <td><select class="cat" data-rf="category_id" data-id="${r.id}">${catOptions(r.category_id)}</select></td>
        <td class="num"><input class="money" data-rf="amount" data-id="${r.id}" value="${r.amount.toFixed(2)}"></td>
        <td class="num"><input data-rf="day_of_month" data-id="${r.id}" value="${r.day_of_month}" style="width:50px"></td>
        <td><input data-rf="account" data-id="${r.id}" value="${esc(r.account)}" style="width:110px"></td>
        <td><input type="checkbox" data-rf="active" data-id="${r.id}" ${r.active ? "checked" : ""}></td>
        <td><button class="small ghost danger" data-del="${r.id}">✕</button></td></tr>`).join("")}
      <tr><td><input id="n-name" placeholder="e.g. Mortgage"></td><td><select class="cat" id="n-cat">${catOptions(null)}</select></td>
        <td class="num"><input class="money" id="n-amt" placeholder="0.00"></td><td class="num"><input id="n-day" value="1" style="width:50px"></td>
        <td><input id="n-acct" value="Checking" style="width:110px"></td><td></td><td><button class="primary small" data-add>Add</button></td></tr>
    </tbody></table></div>
    ${sugg.length ? `<div class="panel"><h2>Suggestions from your history</h2>
      <p class="small muted">These exact amounts showed up in the same envelope most months over the last year.</p>
      <table><tbody>${sugg.slice(0, 20).map((s, i) => `<tr><td>${esc(s.category)}</td><td class="num">${money(s.amount)}</td>
        <td class="muted small">${s.months} of the last 12 months</td><td><button class="small" data-sugg="${i}">＋ Add</button></td></tr>`).join("")}</tbody></table></div>` : ""}
  </div>`;
  el.querySelectorAll("[data-rf]").forEach((i) => (i.onchange = async () => {
    const f = i.dataset.rf;
    const v = f === "active" ? (i.checked ? 1 : 0) : f === "amount" ? parseFloat(i.value) || 0 : f === "day_of_month" ? parseInt(i.value) || 1
      : f === "category_id" ? (i.value ? +i.value : null) : i.value;
    await api(`/recurring/${i.dataset.id}`, { method: "PATCH", body: { [f]: v } });
    toast("Saved");
  }));
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => { await api(`/recurring/${b.dataset.del}`, { method: "DELETE" }); render(); }));
  el.querySelector("[data-add]").onclick = async () => {
    const amount = parseFloat(el.querySelector("#n-amt").value);
    if (isNaN(amount)) return toast("Enter an amount", { error: true });
    await api("/recurring", { method: "POST", body: {
      name: el.querySelector("#n-name").value || "Bill", category_id: +el.querySelector("#n-cat").value || null, amount,
      day_of_month: +el.querySelector("#n-day").value || 1, account: el.querySelector("#n-acct").value,
    } });
    render();
  };
  el.querySelectorAll("[data-sugg]").forEach((b) => (b.onclick = async () => {
    const s = sugg[+b.dataset.sugg];
    const name = prompt(`Name for this ${money(s.amount)} ${s.category} bill:`, s.category);
    if (!name) return;
    await api("/recurring", { method: "POST", body: { name, category_id: s.category_id, amount: s.amount, day_of_month: 1, account: "Checking" } });
    render();
  }));
}

async function aiTab(el) {
  const s = await api("/settings");
  el.innerHTML = `<div class="stack"><div class="panel">
    <h2>Automatic receipt reading</h2>
    <p class="small muted">Receipts, pasted receipt text, Amazon items and leftover card transactions are read and categorized by Claude
      (Anthropic's AI). You need an API key from <a href="https://console.anthropic.com/" target="_blank">console.anthropic.com</a> → API Keys.
      A typical receipt costs a few cents to read. Images are sent to Anthropic for processing only when you upload them.</p>
    <div class="row"><input id="key" type="password" placeholder="${s.api_key_set ? "Key saved " + esc(s.api_key_hint || "(from environment)") : "sk-ant-…"}" style="flex:1;max-width:420px">
      <button class="primary" data-savekey>Save key</button>${s.api_key_source === "settings" ? `<button data-clearkey>Remove</button>` : ""}</div>
    <div class="small" style="margin-top:6px">${s.api_key_set ? `<span class="good">● Connected</span>${s.api_key_source === "environment" ? " (ANTHROPIC_API_KEY environment variable)" : ""}` : `<span class="muted">● Not set</span>`}</div>
  </div>
  <div class="panel"><h2>Household notes for categorizing</h2>
    <p class="small muted">Plain-English hints the AI follows when sorting purchases. Be specific about who/what each envelope is for.</p>
    <textarea id="notes" placeholder="e.g.\nUlta purchases go to Beauty.\nDepop and T.J. Maxx are usually Clothes.\nMeijer Express is gas for the car.\nAnything for the dog goes to Miscellaneous › Pets.">${esc(s.categorization_notes)}</textarea>
    <div class="row" style="margin-top:8px"><span class="spacer"></span><button class="primary" data-savenotes>Save notes</button></div>
  </div></div>`;
  el.querySelector("[data-savekey]").onclick = async () => {
    const k = el.querySelector("#key").value.trim();
    if (!k) return;
    await api("/settings", { method: "PUT", body: { anthropic_api_key: k } });
    toast("API key saved");
    render();
  };
  const ck = el.querySelector("[data-clearkey]");
  if (ck) ck.onclick = async () => { await api("/settings", { method: "PUT", body: { anthropic_api_key: "" } }); render(); };
  el.querySelector("[data-savenotes]").onclick = async () => {
    await api("/settings", { method: "PUT", body: { categorization_notes: el.querySelector("#notes").value } });
    toast("Notes saved");
  };
}

const RENDER = { import: importTab, categories: categoriesTab, rules: rulesTab, cardmap: cardmapTab, recurring: recurringTab, ai: aiTab };

export default {
  monthless: true,
  async render(el) {
    const p = hashParams();
    if (p.get("tab") && RENDER[p.get("tab")]) tab = p.get("tab");
    el.innerHTML = `<div class="chips" style="margin-bottom:16px" id="tabs">${TABS.map(([k, l]) => `<span class="chip ${tab === k ? "active" : ""}" data-tab="${k}">${l}</span>`).join("")}</div><div id="tab"></div>`;
    el.querySelector("#tabs").onclick = (e) => {
      if (!e.target.dataset.tab) return;
      tab = e.target.dataset.tab;
      location.hash = `#/settings?tab=${tab}`;
    };
    await RENDER[tab](el.querySelector("#tab"));
  },
};
