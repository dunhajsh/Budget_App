import { api, catOptions, esc, fail, hashParams, modal, money, render, state, toast } from "../app.js";
import { scanPanel } from "./email.js";

const STATUS = {
  pending: ["Reading…", "accent"],
  parsed: ["Needs review", "warn"],
  applied: ["Applied", "good"],
  reviewed: ["Reviewed", "good"],
  error: ["Needs attention", "bad"],
  duplicate: ["Duplicate", "warn"],
};

function card(r) {
  const [label, cls] = STATUS[r.status] || [r.status, ""];
  const icon = r.source === "amazon" ? "📦" : r.source === "text" ? "📝" : r.source === "email" ? "✉️" : "🧾";
  const extra = [r.n_charges > 1 ? `+ ${r.n_charges - 1} more charge${r.n_charges > 2 ? "s" : ""}` : "",
    r.n_charges && r.n_shared > 0 ? `shared with ${r.n_shared} other receipt${r.n_shared > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ");
  const match = r.transaction_id
    ? `<div class="small muted">${r.n_charges ? "↔" : "suggested:"} ${esc(r.txn_description)} · ${money(r.txn_amount)}${extra ? ` · ${extra}` : ""}</div>`
    : `<div class="small warn">Not matched to a card charge yet</div>`;
  return `<div class="receipt-card" data-open="${r.id}">
    <div class="row"><b>${icon} ${esc(r.merchant || r.filename || "Receipt")}</b><span class="spacer"></span><span class="badge ${cls}">${label}</span></div>
    <div class="row" style="margin-top:6px"><span class="muted small">${r.purchase_date || "date unknown"} · ${r.item_count} items${r.uncategorized ? ` · <span class="warn">${r.uncategorized} uncategorized</span>` : ""}</span>
      <span class="spacer"></span><b>${r.total != null ? money(r.total) : ""}</b></div>
    ${match}
  </div>`;
}

// Where an item's category came from (cleared when you change it yourself).
const srcMark = (it) => it.cat_source === "memory" ? `<span title="Filed the way you filed it on an earlier receipt">🧠</span>`
  : it.cat_source === "ai" ? `<span class="muted" title="AI guess - check it; when you save, your choice is remembered for next time">✨</span>` : "";

export async function openReceipt(id) {
  let r;
  try { r = await api(`/receipts/${id}`); } catch (e) { return fail(e); }
  let items = r.items.map((i) => ({ ...i }));
  // The charge(s) this receipt goes on, and other receipts paid in the same charge.
  const known = new Map();   // id -> charge, for everything shown
  for (const o of r.candidates) for (const t of o.transactions) known.set(t.id, t);
  for (const t of r.links) known.set(t.id, t);
  if (r.transaction && !known.has(r.transaction.id)) known.set(r.transaction.id, r.transaction);
  let picked = r.links.length ? r.links.map((t) => t.id) : r.transaction_id ? [r.transaction_id] : [];
  let withReceipts = [];
  if (!picked.length && r.suggested != null) picked = r.candidates[r.suggested].transactions.map((t) => t.id);
  const suggested = !r.links.length && r.candidates.find((o) => o.transactions.length && o.transactions.every((t) => picked.includes(t.id)) && o.transactions.length === picked.length);
  if (suggested) withReceipts = suggested.with_receipts.map((w) => w.id);
  const withInfo = new Map(r.candidates.flatMap((o) => o.with_receipts).map((w) => [w.id, w]));

  const preview = r.stored_path
    ? (r.stored_path.toLowerCase().endsWith(".pdf")
      ? `<iframe src="/api/receipts/${r.id}/file"></iframe>`
      : `<a href="/api/receipts/${r.id}/file" target="_blank"><img src="/api/receipts/${r.id}/file" alt="receipt"></a>`)
    : r.source === "email" && r.email
      ? `<div class="email-src"><div class="small muted">✉️ ${esc(r.email.sender || "")} · ${esc(r.email.date || "")}${r.email.account ? ` · to ${esc(r.email.account)}` : ""}</div>
          <b>${esc(r.email.subject || "")}</b><pre>${esc(r.source_text || "")}</pre></div>`
      : `<div class="empty">${r.source === "amazon" ? "📦<br>Imported from your Amazon order history" : "📝<br>Entered as text"}</div>`;

  const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));
  const chargeLine = (t) => `<div class="row" style="gap:8px"><div><b>${esc(t.description)}</b>
      <div class="small muted">${esc(t.date)} · ${esc(t.account || "")}${t.covered ? ` · <span class="warn">${money(t.covered)} already explained by other receipts</span>` : ""}</div></div>
      <span class="spacer"></span><b>${money(t.amount)}</b></div>`;
  const optHtml = () => r.candidates.length
    ? r.candidates.map((o, i) => {
        const ids = o.transactions.map((t) => t.id);
        const on = sameSet(ids, picked) && sameSet(o.with_receipts.map((w) => w.id), withReceipts);
        const sumCharges = o.transactions.reduce((a, t) => a + t.amount, 0);
        const sumReceipts = (r.total || 0) + o.with_receipts.reduce((a, w) => a + w.total, 0);
        return `<div class="cand ${on ? "sel" : ""}" data-opt="${i}"><input type="radio" name="opt" ${on ? "checked" : ""}>
          <div style="flex:1">
            <div class="row" style="gap:6px;margin-bottom:4px"><span class="badge ${o.current ? "good" : o.score >= 90 ? "accent" : ""}">${esc(o.why)}</span>
              ${o.transactions.length > 1 || o.with_receipts.length ? `<span class="small muted">${o.with_receipts.length
                ? `this receipt ${money(r.total)} + ${o.with_receipts.map((w) => money(w.total)).join(" + ")} = ${money(sumReceipts)}`
                : `${o.transactions.map((t) => money(t.amount)).join(" + ")} = ${money(sumCharges)}`} ✓</span>` : ""}</div>
            ${o.transactions.map(chargeLine).join("")}
            ${o.with_receipts.map((w) => `<div class="small" style="margin-top:3px">＋ together with <a href="javascript:void 0" data-openrec="${w.id}">receipt #${w.id}</a>
              · ${esc(w.merchant || "")} ${esc(w.purchase_date || "")} · ${money(w.total)}${w.status === "parsed" ? ` <span class="badge warn">not reviewed yet</span>` : ""}</div>`).join("")}
          </div></div>`;
      }).join("")
    : `<div class="muted small">No card charge with this exact amount or this store's name nearby. Import that month's statement first,
        search for the charge below - or record it as a cash/debit purchase.</div>`;
  const pickedHtml = () => {
    if (!picked.length) return `<span class="muted small">Nothing picked yet.</span>`;
    const charges = picked.map((id) => known.get(id)).filter(Boolean);
    const sumC = charges.reduce((a, t) => a + t.amount, 0);
    const sumR = (r.total || 0) + withReceipts.reduce((a, id) => a + (withInfo.get(id)?.total || 0), 0);
    const diff = Math.round((sumC - sumR) * 100) / 100;
    return `${charges.map((t) => `<span class="pill">${esc(t.date)} · ${esc(t.description.slice(0, 26))} · <b>${money(t.amount)}</b>
        <button class="ghost small" data-unpick="${t.id}" title="Remove">✕</button></span>`).join("")}
      ${withReceipts.length ? `<span class="small muted">with receipt${withReceipts.length > 1 ? "s" : ""} ${withReceipts.map((id) => `#${id}`).join(", ")}</span>` : ""}
      <div class="small" style="margin-top:4px">${charges.length > 1 || withReceipts.length ? `Charges ${money(sumC)} · receipts ${money(sumR)} ` : ""}${
        Math.abs(diff) < 0.01 ? `<span class="good">✓ adds up</span>`
        : charges.length === 1 && !withReceipts.length ? `<span class="muted">charge is ${money(Math.abs(diff))} ${diff > 0 ? "more" : "less"} than the receipt (tip, or tax differences) - the categories are fitted to the charge</span>`
        : `<span class="warn">${money(Math.abs(diff))} ${diff > 0 ? "of the charges isn't on these receipts - it stays uncategorized" : "more on the receipts than charged - shares are scaled down"}</span>`}</div>`;
  };

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
            <thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Price</th><th title="Taxed">Tax</th><th>Category <span class="muted small" title="🧠 = filed the way you did before · ✨ = AI guess. Saving remembers your choices for future receipts.">ⓘ</span></th><th></th></tr></thead>
            <tbody id="items"></tbody></table></div>
          <div class="row" style="margin-top:8px"><button class="small" data-additem>＋ Add item</button><span class="spacer"></span><span id="check" class="small"></span></div>
        </div>
        <div>
          <div class="row" style="margin-bottom:8px"><h3>Card charge</h3><span class="spacer"></span>
            ${r.links.length ? `<button class="small" data-unapply title="Take this receipt off its charge(s); their categories stay as they are">Take off charge</button>` : ""}</div>
          ${r.links.length ? `<div class="alert info small" style="margin-bottom:8px"><span class="ico">✓</span><div>Applied to ${r.links.map((t) =>
              `<b>${esc(t.description)}</b> ${esc(t.date)} · ${money(t.amount)}${t.other_receipts.length ? ` (together with ${t.other_receipts.map((o) =>
                `<a href="javascript:void 0" data-openrec="${o.id}">receipt #${o.id}</a> ${money(o.total)}`).join(", ")})` : ""}`).join("; ")}</div></div>` : ""}
          <div id="sheet-warn"></div>
          <div class="small muted" style="margin-bottom:6px">Suggestions match the exact amount or the store name. A receipt can go on several charges
            (charged in parts), and several receipts can go on one charge.</div>
          <div id="cands">${optHtml()}</div>
          <div class="pick-box">
            <div class="small" style="margin-bottom:4px"><b>Selected:</b></div><div id="picked">${pickedHtml()}</div>
            <div class="row" style="margin-top:8px;gap:6px"><input id="charge-q" type="search" placeholder="Find another charge: store name or amount" style="flex:1">
              <span class="small muted">click a result to add it</span></div>
            <div id="charge-results"></div>
          </div>
        </div>
      </div></div>`,
    foot: `<button class="danger" data-delete>Delete</button>
      ${r.stored_path || r.source === "text" || r.source === "email" ? `<button data-reparse ${state.status.ai_enabled ? "" : "disabled title='Turn on AI in Settings'"}>✨ Re-read</button>` : ""}
      <span class="spacer"></span>
      <button data-cash>Record as cash/debit</button>
      <button data-save title="Save your changes and mark this receipt as reviewed">Save</button>
      <button class="primary" data-apply>Save & split the charge</button>`,
    onMount(root, close) {
      const drawItems = () => {
        root.querySelector("#items").innerHTML = items.map((it, i) => `<tr>
          <td><input class="name" data-i="${i}" data-f="name" value="${esc(it.name)}"></td>
          <td><input class="qty" data-i="${i}" data-f="quantity" value="${it.quantity ?? ""}"></td>
          <td><input class="money" data-i="${i}" data-f="amount" value="${it.amount}" style="width:80px"></td>
          <td><input type="checkbox" data-i="${i}" data-f="taxable" ${it.taxable ? "checked" : ""}></td>
          <td><div class="row" style="gap:4px;flex-wrap:nowrap"><select class="cat ${it.category_id ? "" : "uncat"}" data-i="${i}" data-f="category_id">${catOptions(it.category_id)}</select>
            <span class="src" data-src="${i}">${srcMark(it)}</span></div></td>
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
          if (f === "category_id") {
            e.target.classList.toggle("uncat", !e.target.value);
            items[i].cat_source = null;   // your choice now - it'll be remembered when you save
            const m = root.querySelector(`[data-src="${i}"]`);
            if (m) m.innerHTML = "";
          }
        }
        if (e.target.id === "all-cat" && e.target.value) {
          items.forEach((it) => { it.category_id = +e.target.value; it.cat_source = null; });
          e.target.value = "";
          drawItems();
          return;
        }
        check();
      });
      const drawPick = () => {
        root.querySelector("#cands").innerHTML = optHtml();
        root.querySelector("#picked").innerHTML = pickedHtml();
        const sheet = state.status.spreadsheet_through;
        const inSheet = picked.map((id) => known.get(id)).filter((t) => t && sheet && t.budget_month <= sheet);
        root.querySelector("#sheet-warn").innerHTML = inSheet.length
          ? `<div class="alert warn small" style="margin-bottom:8px"><span class="ico">⚠️</span><span>${esc(inSheet.map((t) => t.budget_month).join(", "))} is a month your
             spreadsheet covers. Splitting the charge changes that month's envelopes, so it won't match the spreadsheet any more.</span></div>` : "";
      };
      drawPick();
      let searchTimer;
      root.querySelector("#charge-q").addEventListener("input", (e) => {
        clearTimeout(searchTimer);
        const q = e.target.value.trim();
        searchTimer = setTimeout(async () => {
          const box = root.querySelector("#charge-results");
          if (!q) { box.innerHTML = ""; return; }
          try {
            const res = await api(`/receipts/${r.id}/charges?q=${encodeURIComponent(q)}`);
            res.forEach((t) => known.set(t.id, t));
            box.innerHTML = res.length ? res.slice(0, 12).map((t) => `<div class="cand small" data-add="${t.id}">${chargeLine(t)}
                <span class="badge">${picked.includes(t.id) ? "added" : "＋ add"}</span></div>`).join("")
              : `<div class="muted small" style="margin-top:6px">No charges found.</div>`;
          } catch (err) { fail(err); }
        }, 250);
      });

      root.addEventListener("click", async (e) => {
        const orc = e.target.closest("[data-openrec]");
        if (orc) { close(); openReceipt(+orc.dataset.openrec); return; }
        const o = e.target.closest("[data-opt]");
        if (o) {
          const opt = r.candidates[+o.dataset.opt];
          picked = opt.transactions.map((t) => t.id);
          withReceipts = opt.with_receipts.map((w) => w.id);
          drawPick();
        }
        const add = e.target.closest("[data-add]");
        if (add && !picked.includes(+add.dataset.add)) {
          picked.push(+add.dataset.add);
          add.querySelector(".badge").textContent = "added";
          drawPick();
        }
        const un = e.target.closest("[data-unpick]");
        if (un) { picked = picked.filter((id) => id !== +un.dataset.unpick); if (!picked.length) withReceipts = []; drawPick(); }
        if (e.target.dataset.delitem !== undefined) { items.splice(+e.target.dataset.delitem, 1); drawItems(); }
        if (e.target.matches("[data-additem]")) { items.push({ name: "", quantity: 1, amount: 0, taxable: true, category_id: null }); drawItems(); }
      });

      const save = async (reviewed = false) => {
        const body = {
          reviewed,
          merchant: val("merchant"), purchase_date: val("purchase_date") || null,
          total: parseFloat(val("total")) || null, subtotal: parseFloat(val("subtotal")) || null, tax: parseFloat(val("tax")) || 0,
          items: items.map((i) => ({ ...i, amount: parseFloat(i.amount) || 0, quantity: parseFloat(i.quantity) || null })),
          ...(r.links.length ? {} : { transaction_id: picked[0] || null }),   // remembered as the suggestion
        };
        return api(`/receipts/${r.id}`, { method: "PATCH", body });
      };
      root.querySelector("[data-save]").onclick = async () => {
        try {
          const res = await save(true);
          const m = res.memory || {};
          close();
          toast(`Receipt saved and marked as reviewed${m.learned ? ` · learned ${m.learned} item${m.learned > 1 ? "s" : ""}` : ""}` +
            `${m.updated_other_receipts ? ` · re-filed ${m.updated_other_receipts} matching item${m.updated_other_receipts > 1 ? "s" : ""} on other receipts` : ""}`);
          render();
        } catch (e) { fail(e); }
      };
      root.querySelector("[data-apply]").onclick = async () => {
        if (!picked.length) return toast("Pick the card charge(s) this receipt belongs to (or record it as cash/debit)", { error: true });
        if (items.some((i) => !i.category_id) && !confirm("Some items have no category - their share will stay uncategorized. Continue?")) return;
        try {
          await save();
          await api(`/receipts/${r.id}/apply`, { method: "POST", body: { transaction_ids: picked, with_receipts: withReceipts } });
          close();
          toast(`${picked.length > 1 ? `${picked.length} charges` : "Charge"} split by category ✓${withReceipts.length ? ` · together with ${withReceipts.length} other receipt${withReceipts.length > 1 ? "s" : ""}` : ""}`);
          render();
        } catch (e) { fail(e); }
      };
      const ua = root.querySelector("[data-unapply]");
      if (ua) ua.onclick = async () => {
        if (!confirm("Take this receipt off its charge(s)? Charges that still have other receipts are re-split; otherwise their categories stay as they are.")) return;
        try { await api(`/receipts/${r.id}/unapply`, { method: "POST" }); close(); openReceipt(r.id); render(); } catch (e) { fail(e); }
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
    const isDone = (r) => r.status === "applied" || r.status === "reviewed";
    const open = list.filter((r) => !isDone(r));
    const done = list.filter(isDone);
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
      <div><div class="panel-head"><h2>Done</h2><span class="badge">${done.length}</span></div>
        ${done.length ? `<div class="receipt-list">${done.map(card).join("")}</div>` : `<div class="muted small">Receipts you've saved after reviewing them, or applied to a card charge, show up here.</div>`}</div>
    </div>`;

    const dz = el.querySelector("#rz");
    const input = dz.querySelector("input");
    const upload = async (files) => {
      if (!files.length) return;
      dz.innerHTML = `<span class="spinner"></span> Uploading ${files.length} receipt(s)…`;
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      try {
        const res = await api("/receipts", { method: "POST", form: fd });
        toast(`${res.length} receipt(s) uploaded - they're being read in the background. You can keep using the app.`);
        await render();
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
    // While receipts are being read in the background, refresh the list every few seconds.
    const reading = open.filter((r) => r.status === "pending").length;
    if (reading) {
      const note = document.createElement("div");
      note.className = "alert info";
      note.innerHTML = `<span class="spinner"></span><span>Reading ${reading} receipt(s) in the background${state.status.ai_local ? " with your local model" : ""}…
        This page updates by itself.</span>`;
      el.querySelector(".stack").prepend(note);
      setTimeout(() => { if (alive()) render(); }, 4000);
    }
    scanPanel(el.querySelector("#email-scan"), alive).catch(() => {});
    const want = hashParams().get("open");
    if (want) { history.replaceState(null, "", "#/receipts"); openReceipt(+want); }
  },
};
