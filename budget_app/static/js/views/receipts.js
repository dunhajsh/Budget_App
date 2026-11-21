import { api, catOptions, esc, fail, hashParams, modal, money, render, state, toast } from "../app.js";
import { scanPanel } from "./email.js";

const STATUS = {
  pending: ["Reading…", "accent"],
  parsed: ["Needs review", "warn"],
  applied: ["Applied", "good"],
  error: ["Needs attention", "bad"],
};

function card(r) {
  const [label, cls] = STATUS[r.status] || [r.status, ""];
  const icon = r.source === "amazon" ? "📦" : r.source === "text" ? "📝" : r.source === "email" ? "✉️" : "🧾";
  const match = r.transaction_id
    ? `<div class="small muted">↔ ${esc(r.txn_description)} · ${money(r.txn_amount)}</div>`
    : `<div class="small warn">Not matched to a card charge yet</div>`;
  return `<div class="receipt-card" data-open="${r.id}">
    <div class="row"><b>${icon} ${esc(r.merchant || r.filename || "Receipt")}</b><span class="spacer"></span><span class="badge ${cls}">${label}</span></div>
    <div class="row" style="margin-top:6px"><span class="muted small">${r.purchase_date || "date unknown"} · ${r.item_count} items${r.uncategorized ? ` · <span class="warn">${r.uncategorized} uncategorized</span>` : ""}</span>
      <span class="spacer"></span><b>${r.total != null ? money(r.total) : ""}</b></div>
    ${match}
  </div>`;
}

export async function openReceipt(id) {
  let r;
  try { r = await api(`/receipts/${id}`); } catch (e) { return fail(e); }
  let items = r.items.map((i) => ({ ...i }));
  let chosen = r.transaction_id || null;

  const preview = r.stored_path
    ? (r.stored_path.toLowerCase().endsWith(".pdf")
      ? `<iframe src="/api/receipts/${r.id}/file"></iframe>`
      : `<a href="/api/receipts/${r.id}/file" target="_blank"><img src="/api/receipts/${r.id}/file" alt="receipt"></a>`)
    : r.source === "email" && r.email
      ? `<div class="email-src"><div class="small muted">✉️ ${esc(r.email.sender || "")} · ${esc(r.email.date || "")}${r.email.account ? ` · to ${esc(r.email.account)}` : ""}</div>
          <b>${esc(r.email.subject || "")}</b><pre>${esc(r.source_text || "")}</pre></div>`
      : `<div class="empty">${r.source === "amazon" ? "📦<br>Imported from your Amazon order history" : "📝<br>Entered as text"}</div>`;

  const candHtml = (cands) => cands.length
    ? cands.map((c) => `<div class="cand ${c.id === chosen ? "sel" : ""}" data-cand="${c.id}">
        <input type="radio" name="cand" ${c.id === chosen ? "checked" : ""}>
        <div><b>${esc(c.description)}</b><div class="small muted">${c.date} · ${esc(c.account || "")}</div></div>
        <span class="spacer"></span><b>${money(c.amount)}</b>
        ${c.score >= 85 ? `<span class="badge good">best match</span>` : ""}</div>`).join("")
    : `<div class="muted small">No card charge with a similar amount and date. Import that month's statement first - or record it as a cash/debit purchase below.</div>`;

  modal({
    title: `${r.source === "amazon" ? "Amazon order" : "Receipt"}${r.merchant ? " · " + r.merchant : ""}`,
    wide: true,
    body: `<div class="receipt-detail">
      <div class="receipt-img">${preview}</div>
      <div class="stack">
        ${r.error ? `<div class="alert"><span class="ico">⚠️</span><span>${esc(r.error)}</span></div>` : ""}
        <div class="grid grid-3">
          <label class="field">Store<input name="merchant" value="${esc(r.merchant || "")}"></label>
          <label class="field">Date<input name="purchase_date" type="date" value="${r.purchase_date || ""}"></label>
          <label class="field">Total charged<input name="total" class="money" style="width:100%" value="${r.total ?? ""}"></label>
          <label class="field">Subtotal<input name="subtotal" class="money" style="width:100%" value="${r.subtotal ?? ""}"></label>
          <label class="field">Tax<input name="tax" class="money" style="width:100%" value="${r.tax ?? ""}" ${r.source === "amazon" ? "disabled title='Included in item prices'" : ""}></label>
        </div>
        <div>
          <div class="row" style="margin-bottom:6px"><h3>Items</h3><span class="spacer"></span>
            <select class="cat" id="all-cat" style="max-width:220px">${catOptions(null, { empty: "Set all items to…" })}</select></div>
          <div class="table-wrap" style="max-height:330px;overflow:auto"><table class="items-table">
            <thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Price</th><th title="Taxed">Tax</th><th>Category</th><th></th></tr></thead>
            <tbody id="items"></tbody></table></div>
          <div class="row" style="margin-top:8px"><button class="small" data-additem>＋ Add item</button><span class="spacer"></span><span id="check" class="small"></span></div>
        </div>
        <div>
          <h3 style="margin-bottom:8px">Card charge</h3>
          <div id="cands">${candHtml(r.candidates)}</div>
        </div>
      </div></div>`,
    foot: `<button class="danger" data-delete>Delete</button>
      ${r.stored_path || r.source === "text" || r.source === "email" ? `<button data-reparse ${state.status.ai_enabled ? "" : "disabled title='Turn on AI in Settings'"}>✨ Re-read</button>` : ""}
      <span class="spacer"></span>
      <button data-cash>Record as cash/debit</button>
      <button data-save>Save</button>
      <button class="primary" data-apply>Save & split the charge</button>`,
    onMount(root, close) {
      const drawItems = () => {
        root.querySelector("#items").innerHTML = items.map((it, i) => `<tr>
          <td><input class="name" data-i="${i}" data-f="name" value="${esc(it.name)}"></td>
          <td><input class="qty" data-i="${i}" data-f="quantity" value="${it.quantity ?? ""}"></td>
          <td><input class="money" data-i="${i}" data-f="amount" value="${it.amount}" style="width:80px"></td>
          <td><input type="checkbox" data-i="${i}" data-f="taxable" ${it.taxable ? "checked" : ""}></td>
          <td><select class="cat ${it.category_id ? "" : "uncat"}" data-i="${i}" data-f="category_id">${catOptions(it.category_id)}</select></td>
          <td><button class="ghost small" data-delitem="${i}">✕</button></td></tr>`).join("") ||
          `<tr><td colspan="6" class="muted small">No items yet - add them, or click Re-read.</td></tr>`;
        check();
      };
      const val = (n) => root.querySelector(`[name=${n}]`).value;
      const check = () => {
        const sum = items.reduce((a, i) => a + (parseFloat(i.amount) || 0), 0);
        const tax = r.source === "amazon" ? 0 : parseFloat(val("tax")) || 0;
        const total = parseFloat(val("total")) || 0;
        const diff = Math.round((total - sum - tax) * 100) / 100;
        const el = root.querySelector("#check");
        el.innerHTML = `Items ${money(sum)}${tax ? ` + tax ${money(tax)}` : ""} = ${money(sum + tax)}` +
          (Math.abs(diff) < 0.02 ? ` <span class="good">✓ matches total</span>`
            : ` <span class="warn">${money(diff)} off from total - the difference is spread proportionally</span>`);
      };
      drawItems();

      root.addEventListener("input", (e) => {
        const i = e.target.dataset.i;
        if (i !== undefined) {
          const f = e.target.dataset.f;
          items[i][f] = f === "taxable" ? e.target.checked : f === "category_id" ? (e.target.value ? +e.target.value : null) : e.target.value;
          if (f === "category_id") e.target.classList.toggle("uncat", !e.target.value);
        }
        if (e.target.id === "all-cat" && e.target.value) {
          items.forEach((it) => (it.category_id = +e.target.value));
          e.target.value = "";
          drawItems();
          return;
        }
        check();
      });
      root.addEventListener("click", async (e) => {
        const c = e.target.closest("[data-cand]");
        if (c) {
          chosen = +c.dataset.cand;
          root.querySelectorAll(".cand").forEach((x) => { x.classList.toggle("sel", +x.dataset.cand === chosen); x.querySelector("input").checked = +x.dataset.cand === chosen; });
        }
        if (e.target.dataset.delitem !== undefined) { items.splice(+e.target.dataset.delitem, 1); drawItems(); }
        if (e.target.matches("[data-additem]")) { items.push({ name: "", quantity: 1, amount: 0, taxable: true, category_id: null }); drawItems(); }
      });

      const save = async () => {
        const body = {
          merchant: val("merchant"), purchase_date: val("purchase_date") || null,
          total: parseFloat(val("total")) || null, subtotal: parseFloat(val("subtotal")) || null, tax: parseFloat(val("tax")) || 0,
          items: items.map((i) => ({ ...i, amount: parseFloat(i.amount) || 0, quantity: parseFloat(i.quantity) || null })),
          transaction_id: chosen,
        };
        return api(`/receipts/${r.id}`, { method: "PATCH", body });
      };
      root.querySelector("[data-save]").onclick = async () => { try { await save(); close(); toast("Receipt saved"); render(); } catch (e) { fail(e); } };
      root.querySelector("[data-apply]").onclick = async () => {
        if (!chosen) return toast("Pick the card charge this receipt belongs to (or record it as cash/debit)", { error: true });
        if (items.some((i) => !i.category_id) && !confirm("Some items have no category - their share will stay uncategorized. Continue?")) return;
        try {
          await save();
          await api(`/receipts/${r.id}/apply`, { method: "POST", body: { transaction_id: chosen } });
          close(); toast("Charge split by category ✓"); render();
        } catch (e) { fail(e); }
      };
      root.querySelector("[data-cash]").onclick = async () => {
        const account = prompt("Paid with (account name):", "Debit card");
        if (!account) return;
        try {
          await save();
          await api(`/receipts/${r.id}/apply`, { method: "POST", body: { create_new: true, account, budget_month: state.month } });
          close(); toast("Recorded as a new transaction"); render();
        } catch (e) { fail(e); }
      };
      root.querySelector("[data-delete]").onclick = async () => {
        if (!confirm("Delete this receipt? (The card transaction stays, with its current categories.)")) return;
        await api(`/receipts/${r.id}`, { method: "DELETE" }); close(); render();
      };
      const rp = root.querySelector("[data-reparse]");
      if (rp) rp.onclick = async () => {
        rp.disabled = true; rp.innerHTML = `<span class="spinner"></span> Reading…`;
        try { await api(`/receipts/${r.id}/parse`, { method: "POST" }); close(); openReceipt(r.id); }
        catch (e) { fail(e); rp.disabled = false; rp.textContent = "✨ Re-read"; }
      };
    },
  });
}

function amazonDialog() {
  const since = new Date();
  since.setMonth(since.getMonth() - 3);
  modal({
    title: "Import Amazon orders",
    body: `<div class="stack">
      <ol class="small" style="margin:0;padding-left:18px">
        <li>On amazon.com go to <b>Account → Your Account → Request Your Data</b> (or search "Request my data").</li>
        <li>Choose <b>Your Orders</b> and submit. Amazon emails you a download link (usually within a few hours).</li>
        <li>Upload the <b>.zip</b> here as-is (or the <code>Retail.OrderHistory.1.csv</code> inside it).</li>
      </ol>
      <div class="dropzone" id="az"><div class="big">📦</div><b>Drop the Amazon zip or CSV</b><br>or click to choose<input type="file" accept=".zip,.csv" hidden></div>
      <label class="field">Only import orders shipped on or after<input type="date" name="since" value="${since.toISOString().slice(0, 10)}"></label>
      <p class="small muted" style="margin:0">Each shipment is matched to the Amazon charge on your card with the same amount${state.status.ai_enabled ? ", and every item is sorted into a category for you" : ""}.</p>
    </div>`,
    foot: `<button data-close>Cancel</button>`,
    onMount(root, close) {
      const dz = root.querySelector("#az");
      const input = dz.querySelector("input");
      const go = async (f) => {
        dz.innerHTML = `<span class="spinner"></span> Importing ${esc(f.name)}…`;
        const fd = new FormData();
        fd.append("file", f);
        fd.append("since", root.querySelector("[name=since]").value);
        try {
          const r = await api("/import/amazon", { method: "POST", form: fd });
          close();
          toast(`Amazon: ${r.shipments} shipments imported, ${r.matched} matched to card charges${r.already_imported ? `, ${r.already_imported} already imported` : ""}`);
          if (r.ai_error) toast(r.ai_error, { error: true });
          render();
        } catch (e) { fail(e); close(); }
      };
      dz.onclick = () => input.click();
      input.onchange = () => input.files[0] && go(input.files[0]);
      dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("drag"); };
      dz.ondragleave = () => dz.classList.remove("drag");
      dz.ondrop = (e) => { e.preventDefault(); e.dataTransfer.files[0] && go(e.dataTransfer.files[0]); };
    },
  });
}

function textDialog() {
  modal({
    title: "Paste a receipt",
    body: `<p class="small muted" style="margin-top:0">Paste the text of an emailed receipt (e.g. a Meijer.com order or pickup confirmation).</p>
      <textarea id="rtext" style="min-height:260px" placeholder="Paste receipt text here…"></textarea>`,
    foot: `<button data-close>Cancel</button><button class="primary" data-go>Read receipt</button>`,
    onMount(root, close) {
      root.querySelector("[data-go]").onclick = async (e) => {
        const text = root.querySelector("#rtext").value.trim();
        if (!text) return;
        e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span> Reading…`;
        const fd = new FormData();
        fd.append("text", text);
        try {
          const res = await api("/receipts", { method: "POST", form: fd });
          close(); render();
          if (res[0]) openReceipt(res[0].id);
        } catch (err) { fail(err); e.target.disabled = false; e.target.textContent = "Read receipt"; }
      };
    },
  });
}

export default {
  monthless: true,
  async render(el, alive) {
    const list = await api("/receipts");
    if (!alive()) return;
    const open = list.filter((r) => r.status !== "applied");
    const done = list.filter((r) => r.status === "applied");
    el.innerHTML = `<div class="stack">
      ${state.status.ai_enabled ? "" : `<div class="alert info"><span class="ico">✨</span><span>Turn on AI in <a href="#/settings">Settings</a> (a free local model, or Claude)
        and receipts will be read automatically - every line item extracted, categorized, and matched to the right card charge.
        Without it you can still type items in by hand.</span></div>`}
      <div class="grid grid-3">
        <div class="dropzone" id="rz" style="grid-column:span 2"><div class="big">🧾</div><b>Drop receipt photos or PDFs here</b><br>
          or click to choose (several at once is fine) · JPG, PNG, PDF
          <input type="file" accept="image/*,.pdf" multiple hidden></div>
        <div class="stack">
          <button style="width:100%;justify-content:center" data-amazon>📦 Import Amazon orders</button>
          <button style="width:100%;justify-content:center" data-text>📝 Paste receipt text</button>
          <div id="email-scan"></div>
          <div class="small muted">Meijer: open the Meijer app → <i>Purchases</i> → a receipt → screenshot it, or save the emailed receipt as PDF.</div>
        </div>
      </div>
      <div><div class="panel-head"><h2>To review</h2><span class="badge">${open.length}</span></div>
        ${open.length ? `<div class="receipt-list">${open.map(card).join("")}</div>` : `<div class="muted small">All caught up.</div>`}</div>
      <div><div class="panel-head"><h2>Applied</h2><span class="badge">${done.length}</span></div>
        ${done.length ? `<div class="receipt-list">${done.map(card).join("")}</div>` : `<div class="muted small">Receipts you've applied to a card charge show up here.</div>`}</div>
    </div>`;

    const dz = el.querySelector("#rz");
    const input = dz.querySelector("input");
    const upload = async (files) => {
      if (!files.length) return;
      dz.innerHTML = `<span class="spinner"></span> ${state.status.ai_enabled ? `Reading ${files.length} receipt(s) - about ${state.status.ai_local ? "20 seconds to 2 minutes" : "10–30 seconds"} each…` : "Uploading…"}`;
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      try {
        const res = await api("/receipts", { method: "POST", form: fd });
        const errs = res.filter((r) => r.status === "error");
        if (errs.length) toast(errs[0].error, { error: true });
        await render();
        if (res.length === 1) openReceipt(res[0].id);
      } catch (e) { fail(e); render(); }
    };
    dz.onclick = () => input.click();
    input.onchange = () => upload([...input.files]);
    dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("drag"); };
    dz.ondragleave = () => dz.classList.remove("drag");
    dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("drag"); upload([...e.dataTransfer.files]); };
    el.querySelector("[data-amazon]").onclick = amazonDialog;
    el.querySelector("[data-text]").onclick = () => {
      if (!state.status.ai_enabled) return toast("Reading pasted text needs AI turned on (Settings).", { error: true });
      textDialog();
    };
    el.querySelectorAll("[data-open]").forEach((c) => (c.onclick = () => openReceipt(+c.dataset.open)));
    scanPanel(el.querySelector("#email-scan"), alive).catch(() => {});
    const want = hashParams().get("open");
    if (want) { history.replaceState(null, "", "#/receipts"); openReceipt(+want); }
  },
};
