// "Edit charge" dialog: category (or a split over several), note, budget month, left out of the budget.
// Used where a charge is shown outside the Transactions page (e.g. the Spending drill-down).
import { api, catName, catOptions, esc, fail, modal, money, monthLabel, toast } from "../app.js";

const titleCase = (s) => (s || "").toLowerCase().replace(/\b([a-z])/g, (c) => c.toUpperCase());
const unnamed = (t) => t.source === "excel" && / \(spreadsheet\)$/.test(t.description || "");
const cents = (v) => Math.round((parseFloat(v) || 0) * 100);
const key = (rows) => JSON.stringify(rows.map((r) => [r.category_id || null, cents(r.amount), r.memo || ""]));

/** Open the editor for a charge (its id or a transaction row). onSaved runs after anything was saved. */
export async function editCharge(idOrTxn, { onSaved } = {}) {
  let t;
  try { t = typeof idOrTxn === "object" ? idOrTxn : await api(`/transactions/${idOrTxn}`); } catch (e) { return fail(e); }
  const name = unnamed(t) ? "Spreadsheet entry" : titleCase(t.merchant || t.description);
  const start = t.splits.length ? t.splits.map((s) => ({ category_id: s.category_id, amount: s.amount, memo: s.memo || "" }))
    : [{ category_id: null, amount: t.amount, memo: "" }];
  let rows = start.map((r) => ({ ...r }));
  const original = key(start);
  // While splitting, the first row keeps whatever the other rows don't take - until you type its amount yourself.
  let autoFirst = true;
  const fillFirst = () => {
    if (autoFirst && rows.length > 1) rows[0].amount = (cents(t.amount) - rows.slice(1).reduce((a, r) => a + cents(r.amount), 0)) / 100;
  };

  const draw = (root) => {
    const multi = rows.length > 1;
    root.querySelector("#ce-rows").innerHTML = rows.map((r, i) => `<tr>
        <td><select class="cat" data-i="${i}" data-f="category_id" style="max-width:none">${catOptions(r.category_id)}</select></td>
        ${multi ? `<td style="width:110px"><input class="money" data-i="${i}" data-f="amount" value="${r.amount}"></td>` : ""}
        <td><input data-i="${i}" data-f="memo" value="${esc(r.memo || "")}" placeholder="note (optional)"></td>
        ${multi ? `<td><button class="ghost small" data-del="${i}" title="Remove this part">✕</button></td>` : ""}</tr>`).join("");
    root.querySelector("#ce-amt-head").classList.toggle("hidden", !multi);
    update(root);
  };
  const update = (root) => {
    const multi = rows.length > 1;
    const left = (cents(t.amount) - rows.reduce((a, r) => a + cents(r.amount), 0)) / 100;
    const rem = root.querySelector("#ce-left");
    rem.textContent = !multi ? "" : left === 0 ? "✓ Adds up" : `${money(left)} ${left > 0 ? "left to assign" : "over"}`;
    rem.className = left === 0 ? "good" : "warn";
    root.querySelector("#ce-rest").classList.toggle("hidden", !multi || left === 0);
    const excluded = root.querySelector("[name=excluded]").checked;
    root.querySelector("#ce-cats").classList.toggle("dim", excluded);
    const ruleBox = root.querySelector("#ce-rule");
    const cid = rows[0].category_id;
    const showRule = !multi && cid && !excluded && !unnamed(t) && t.source !== "excel";
    ruleBox.classList.toggle("hidden", !showRule);
    if (showRule) ruleBox.querySelector("span").innerHTML = `Always put <b>${esc(name)}</b> in <b>${esc(catName(cid))}</b> (a rule for future charges)`;
    root.querySelector("[data-save]").disabled = !excluded && multi && left !== 0;
  };

  modal({
    title: "Edit charge",
    wide: true,
    body: `<div class="stack">
      <div class="row" style="align-items:flex-start">
        <div style="flex:1;min-width:0"><div class="desc-main">${esc(name)}</div>
          <div class="desc-raw">${unnamed(t) ? "From your budget spreadsheet" : esc(t.description)} · ${esc(t.date)} · ${esc(t.account || "")}</div>
          <div class="row" style="gap:4px;margin-top:4px">
            ${t.source === "excel" ? `<span class="badge">spreadsheet</span>` : ""}
            ${t.receipt_id ? `<a class="badge ${t.receipt_count ? "good" : ""}" href="#/receipts?open=${t.receipt_id}">🧾 ${t.receipt_count > 1 ? `${t.receipt_count} receipts` : t.receipt_count ? "receipt" : "receipt to apply"}</a>` : ""}
            ${t.date.slice(0, 7) !== t.budget_month ? `<span class="badge">counts toward ${esc(monthLabel(t.budget_month, true))}</span>` : ""}</div></div>
        <div class="kpi" style="text-align:right"><div class="value">${money(t.amount)}</div></div>
      </div>
      ${t.receipt_count ? `<div class="alert info"><span class="ico">🧾</span><span>This charge is split by the items on ${t.receipt_count > 1 ? `${t.receipt_count} receipts` : "its receipt"}.
        Your changes here are kept, but applying the receipt again would split it by the items again. To re-file single items,
        <a href="#/receipts?open=${t.receipt_id}">edit the receipt</a> instead.</span></div>` : ""}
      ${t.source === "excel" ? `<p class="small muted" style="margin:0">This entry comes from your budget spreadsheet. Filing it under a subcategory of the same envelope
        keeps the envelope's total the same; moving it to another envelope changes both envelopes' totals for ${esc(monthLabel(t.budget_month))}.</p>` : ""}
      <div id="ce-cats">
        <table><thead><tr><th>Category</th><th class="num hidden" id="ce-amt-head">Amount</th><th>Note</th><th></th></tr></thead><tbody id="ce-rows"></tbody></table>
        <div class="row" style="margin-top:8px"><button class="small" data-add>＋ Split into another category</button>
          <button class="small hidden" id="ce-rest">Put the rest in the last row</button><span class="spacer"></span><b id="ce-left"></b></div>
        <label class="row small hidden" id="ce-rule" style="margin-top:8px"><input type="checkbox" name="remember"> <span></span></label>
      </div>
      <div class="grid grid-2">
        <label class="field">Counts toward budget month<input type="month" name="budget_month" value="${esc(t.budget_month)}"></label>
        <label class="field">Note on the charge<input name="notes" value="${esc(t.notes || "")}" placeholder="e.g. Birthday gift"></label>
      </div>
      <label class="row"><input type="checkbox" name="excluded" ${t.excluded ? "checked" : ""}> Not part of the budget (card payment, transfer, reimbursed)</label>
    </div>`,
    foot: `<a href="#/transactions?month=${esc(t.budget_month)}" class="small" data-close>Open in Transactions</a><span class="spacer"></span>
      <button data-close>Cancel</button><button class="primary" data-save>Save</button>`,
    onMount(root, close) {
      draw(root);
      root.addEventListener("input", (e) => {
        const i = e.target.dataset.i;
        if (i !== undefined) {
          const f = e.target.dataset.f;
          rows[i][f] = f === "category_id" ? (e.target.value ? +e.target.value : null) : e.target.value;
          if (f === "amount" && +i === 0) autoFirst = false;
          if (f === "amount" && +i > 0 && autoFirst) {
            fillFirst();
            const first = root.querySelector('#ce-rows input[data-i="0"][data-f="amount"]');
            if (first) first.value = rows[0].amount;
          }
        }
        update(root);
      });
      root.addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.del !== undefined) { rows.splice(+b.dataset.del, 1); if (rows.length === 1) rows[0].amount = t.amount; else fillFirst(); draw(root); }
        if (b.matches("[data-add]")) {
          const left = (cents(t.amount) - rows.reduce((a, r) => a + cents(r.amount), 0)) / 100;
          rows.push({ category_id: null, amount: autoFirst ? 0 : left, memo: "" });
          draw(root);
          root.querySelectorAll("#ce-rows select")[rows.length - 1]?.focus();
        }
        if (b.id === "ce-rest") {
          const others = rows.slice(0, -1).reduce((a, r) => a + cents(r.amount), 0);
          rows[rows.length - 1].amount = (cents(t.amount) - others) / 100;
          draw(root);
        }
      });
      root.querySelector("[data-save]").onclick = async (ev) => {
        const btn = ev.target;
        btn.disabled = true;
        const excluded = root.querySelector("[name=excluded]").checked;
        const clean = rows.filter((r, i) => rows.length === 1 || i === 0 || cents(r.amount) || r.category_id);
        try {
          let rule = null;
          if (!excluded && key(clean) !== original) {
            const single = clean.length === 1 && !clean[0].memo;
            if (single) {
              const remember = root.querySelector("[name=remember]").checked && !root.querySelector("#ce-rule").classList.contains("hidden");
              await api(`/transactions/${t.id}`, { method: "PATCH", body: { category_id: clean[0].category_id, remember } });
              if (remember) rule = catName(clean[0].category_id);
            } else {
              await api(`/transactions/${t.id}/splits`, { method: "PUT", body: { splits: clean.map((r) => ({ ...r, amount: (rows.length === 1 ? cents(t.amount) : cents(r.amount)) / 100 })) } });
            }
          }
          await api(`/transactions/${t.id}`, { method: "PATCH", body: {
            budget_month: root.querySelector("[name=budget_month]").value || t.budget_month,
            notes: root.querySelector("[name=notes]").value,
            excluded,
            reviewed: true,
          } });
          close();
          toast(rule ? `Saved · rule added: ${name} → ${rule}` : "Charge saved");
          if (onSaved) await onSaved();
        } catch (err) { fail(err); btn.disabled = false; }
      };
    },
  });
}
