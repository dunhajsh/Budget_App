// Core: state, API helper, router, shared UI pieces.
import dashboard from "./views/dashboard.js";
import transactions from "./views/transactions.js";
import receipts from "./views/receipts.js";
import budget from "./views/budget.js";
import reports from "./views/reports.js";
import settings from "./views/settings.js";

const views = { dashboard, transactions, receipts, budget, reports, settings };
const titles = {
  dashboard: "Dashboard", transactions: "Transactions", receipts: "Receipts",
  budget: "Budget", reports: "Reports", settings: "Settings",
};

export const state = {
  month: localStorage.getItem("month") || null,
  categories: [],     // tree
  flat: [],           // [{id, name, label, parent_id, grp}]
  status: {},
};

// ---------------------------------------------------------------- helpers
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const fmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const fmt0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
export const money = (v) => fmt.format(v || 0);
export const money0 = (v) => fmt0.format(v || 0);
export const signClass = (v) => (v < -0.004 ? "bad" : v > 0.004 ? "good" : "muted");

export function monthLabel(m, short = false) {
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, mo - 1, 1).toLocaleString("en-US", { month: short ? "short" : "long", year: "numeric" });
}
export function monthAdd(m, n) {
  const [y, mo] = m.split("-").map(Number);
  const idx = y * 12 + (mo - 1) + n;
  return `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`;
}
export const today = () => new Date().toISOString().slice(0, 10);

export async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch("/api" + path, init);
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

export function toast(msg, { error = false, actions = [], timeout = 4500 } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<span>${esc(msg)}</span>`;
  for (const a of actions) {
    const b = document.createElement("button");
    b.className = "small";
    b.textContent = a.label;
    b.onclick = async () => { el.remove(); await a.onClick(); };
    el.appendChild(b);
  }
  document.getElementById("toasts").appendChild(el);
  if (timeout) setTimeout(() => el.remove(), actions.length ? timeout * 2.5 : timeout);
  return el;
}
export const fail = (e) => toast(e.message || String(e), { error: true });

export function modal({ title, body, foot = "", wide = false, onMount }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal ${wide ? "wide" : ""}">
      <div class="modal-head"><h2>${esc(title)}</h2><div class="spacer"></div><button class="ghost" data-close>✕</button></div>
      <div class="modal-body">${body}</div>
      ${foot ? `<div class="modal-foot">${foot}</div>` : ""}
    </div>`;
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  back.querySelectorAll("[data-close]").forEach((b) => (b.onclick = close));
  document.body.appendChild(back);
  const root = back.querySelector(".modal");
  if (onMount) onMount(root, close);
  return { root, close };
}

/** <option> list for every category, grouped by envelope. */
export function catOptions(selected, { empty = "— Uncategorized —" } = {}) {
  let html = empty !== null ? `<option value="">${esc(empty)}</option>` : "";
  let currentGroup = null;
  for (const p of state.categories) {
    if (p.grp !== currentGroup) {
      if (currentGroup !== null) html += "</optgroup>";
      html += `<optgroup label="${esc(p.grp)}">`;
      currentGroup = p.grp;
    }
    html += `<option value="${p.id}" ${p.id === selected ? "selected" : ""}>${esc(p.name)}</option>`;
    for (const c of p.children) {
      html += `<option value="${c.id}" ${c.id === selected ? "selected" : ""}>&nbsp;&nbsp;&nbsp;↳ ${esc(c.name)}</option>`;
    }
  }
  if (currentGroup !== null) html += "</optgroup>";
  return html;
}
export const catName = (id) => state.flat.find((c) => c.id === id)?.label || "Uncategorized";

export async function loadCategories() {
  const cats = await api("/categories");
  // keep groups contiguous in the dropdowns
  const order = [];
  for (const c of cats) if (!order.includes(c.grp)) order.push(c.grp);
  cats.sort((a, b) => order.indexOf(a.grp) - order.indexOf(b.grp) || a.sort_order - b.sort_order);
  state.categories = cats;
  state.flat = [];
  for (const p of cats) {
    state.flat.push({ id: p.id, name: p.name, label: p.name, parent_id: null, grp: p.grp });
    for (const c of p.children) state.flat.push({ id: c.id, name: c.name, label: `${p.name} › ${c.name}`, parent_id: p.id, grp: p.grp });
  }
}

export async function refreshStatus() {
  state.status = await api("/status");
  document.getElementById("ai-status").innerHTML = state.status.ai_enabled
    ? `<span class="badge good" title="Receipts are read by ${esc(state.status.ai_label || "")}">AI: ${esc(state.status.ai_label || "on")}</span>`
    : `<a href="#/settings">Turn on AI receipt reading →</a>`;
  try {
    const s = await api(`/summary?month=${state.month}`);
    const badge = document.getElementById("nav-review");
    const n = s.to_review + s.uncategorized_count;
    badge.textContent = n;
    badge.classList.toggle("hidden", !n);
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- quick add
export function quickAdd(prefill = {}) {
  const accounts = ["Checking", "Cash", ...(state.status.accounts || [])].filter((v, i, a) => a.indexOf(v) === i);
  modal({
    title: "Add an expense",
    body: `<div class="grid grid-2">
        <label class="field">Amount<input name="amount" class="money" style="width:100%" inputmode="decimal" placeholder="0.00" value="${esc(prefill.amount ?? "")}"></label>
        <label class="field">Date<input name="date" type="date" value="${prefill.date || today()}"></label>
        <label class="field" style="grid-column:1/-1">Description<input name="description" placeholder="e.g. Mortgage, Farmers market" value="${esc(prefill.description ?? "")}"></label>
        <label class="field" style="grid-column:1/-1">Category<select name="category_id" class="cat" style="max-width:none">${catOptions(prefill.category_id)}</select></label>
        <label class="field">Paid from<input name="account" list="acct-list" value="${esc(prefill.account || "Checking")}">
          <datalist id="acct-list">${accounts.map((a) => `<option value="${esc(a)}">`).join("")}</datalist></label>
        <label class="field">Budget month<input name="budget_month" type="month" value="${state.month}"></label>
      </div>
      <p class="small muted">Tip: use a negative amount for a refund. Card purchases come in automatically when you import your statement - use this for checks, cash, and bank-paid bills.</p>`,
    foot: `<button data-close>Cancel</button><button class="primary" data-save>Add expense</button>`,
    onMount(root, close) {
      root.querySelector("[name=amount]").focus();
      const save = async () => {
        const v = (n) => root.querySelector(`[name=${n}]`).value;
        const amount = parseFloat(v("amount").replace(/[$,]/g, ""));
        if (isNaN(amount)) return toast("Enter an amount", { error: true });
        try {
          await api("/transactions", { method: "POST", body: {
            amount, date: v("date"), description: v("description"), category_id: v("category_id") ? +v("category_id") : null,
            account: v("account"), budget_month: v("budget_month"),
          } });
          close();
          toast("Expense added");
          render();
        } catch (e) { fail(e); }
      };
      root.querySelector("[data-save]").onclick = save;
      root.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") save(); });
    },
  });
}

// ---------------------------------------------------------------- router
function currentView() {
  const name = (location.hash.replace(/^#\/?/, "").split("?")[0] || "dashboard");
  return views[name] ? name : "dashboard";
}
export function hashParams() {
  return new URLSearchParams(location.hash.split("?")[1] || "");
}

let renderToken = 0;
export async function render() {
  const name = currentView();
  const token = ++renderToken;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  document.getElementById("page-title").textContent = titles[name];
  document.getElementById("month-label").textContent = monthLabel(state.month);
  document.getElementById("month-switch").classList.toggle("hidden", views[name].monthless === true);
  // Fresh container each render so views can attach listeners without stacking them
  const old = document.getElementById("view");
  const el = document.createElement("div");
  el.id = "view";
  if (old.innerHTML) el.innerHTML = old.innerHTML; // avoid a flash while loading
  old.replaceWith(el);
  try {
    await views[name].render(el, () => token === renderToken);
  } catch (e) {
    console.error(e);
    el.innerHTML = `<div class="panel empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
  }
  refreshStatus();
}

export function setMonth(m) {
  state.month = m;
  localStorage.setItem("month", m);
  render();
}

async function boot() {
  state.status = await api("/status");
  if (!state.month) state.month = state.status.latest_month;
  await loadCategories();
  document.getElementById("month-switch").addEventListener("click", (e) => {
    const step = e.target.closest("button")?.dataset.step;
    if (step) setMonth(monthAdd(state.month, +step));
  });
  document.getElementById("quick-add").onclick = () => quickAdd();
  window.addEventListener("hashchange", render);
  if (!state.status.has_categories && !location.hash.includes("settings")) location.hash = "#/settings?welcome=1";
  render();
}
boot().catch((e) => {
  document.getElementById("view").innerHTML = `<div class="panel empty"><div class="big">⚠️</div>Couldn't start: ${esc(e.message)}</div>`;
});
