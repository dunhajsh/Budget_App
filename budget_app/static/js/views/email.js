// Email receipts: account settings (Settings → Email) and the "Check email" panel on the Receipts page.
import { api, esc, fail, modal, render, state, toast } from "../app.js";

const PROVIDERS = { gmail: "Gmail", outlook: "Outlook / Hotmail", other: "Other (IMAP)" };

function firstOfLastMonth() {
  const d = new Date();
  const m = new Date(d.getFullYear(), d.getMonth() - 1, 1);
  return `${m.getFullYear()}-${String(m.getMonth() + 1).padStart(2, "0")}-01`;
}

const HELP_GMAIL = `<ol class="small" style="margin:6px 0 0 18px;padding:0">
  <li>Google Account → <b>Security</b> → turn on <b>2-Step Verification</b> (if it isn't already).</li>
  <li>Open <a href="https://myaccount.google.com/apppasswords" target="_blank">myaccount.google.com/apppasswords</a>, name it "Budget", and click <b>Create</b>.</li>
  <li>Paste the 16-letter password below. It only works for reading mail, and you can revoke it there anytime.</li>
</ol>
<p class="small muted" style="margin:6px 0 0">The app searches Gmail's <b>Purchases</b> category (Gmail sorts receipts there automatically) plus anything you label <b>Receipts</b>.</p>`;

const HELP_OUTLOOK = `<p class="small" style="margin:6px 0 0">Microsoft has turned off password sign-in for most personal Outlook.com accounts, so this may not connect. Try it:</p>
<ol class="small" style="margin:6px 0 0 18px;padding:0">
  <li><a href="https://account.microsoft.com/security" target="_blank">account.microsoft.com/security</a> → <b>Advanced security options</b> → <b>App passwords</b> → create one (needs two-step verification on).</li>
  <li>Paste it below. If Outlook rejects it, use forwarding instead:</li>
</ol>
<p class="small" style="margin:6px 0 0"><b>Forwarding (always works):</b> in Outlook.com → Settings → Mail → <b>Rules</b> → <i>Add new rule</i>:
  <i>Subject includes</i> "receipt", "order", "your purchase", "invoice" → <i>Forward to</i> one of your Gmail addresses. Then in that Gmail account, add a filter for mail from your Outlook address that applies the label <b>Receipts</b>.</p>`;

export async function emailTab(el) {
  const [accts, seen] = await Promise.all([api("/email/accounts"), api("/email/seen")]);
  el.innerHTML = `<div class="stack">
  <div class="panel">
    <h2>Email receipts</h2>
    <p class="small muted" style="margin-top:0">The app looks through your mailboxes for order confirmations and digital receipts (Amazon, Meijer, Target, Walmart, Venmo,
      subscriptions…), reads the items with AI, and matches each one to its card charge. It's <b>read-only</b>: nothing is marked read, moved, sent or deleted.
      Passwords are kept in Windows Credential Manager, not in the budget file.${state.status.ai_local ? " Emails are read by your local model, so they never leave this computer." : ""}</p>
    ${accts.length ? `<div class="table-wrap"><table><thead><tr><th>Account</th><th>Looks at</th><th>Since</th><th>Found</th><th>Last checked</th><th></th></tr></thead><tbody>
      ${accts.map((a) => `<tr>
        <td><b>${esc(a.address)}</b><div class="small muted">${PROVIDERS[a.provider] || a.provider}${a.active ? "" : " · paused"}</div>
          ${a.last_error ? `<div class="small bad" style="max-width:420px">${esc(a.last_error)}</div>` : ""}
          ${a.has_password ? "" : `<div class="small bad">Password missing - re-enter it.</div>`}</td>
        <td class="small">${a.provider === "gmail" && a.search ? `Gmail search: <code>${esc(a.search)}</code>` : `Folder <code>${esc(a.folder)}</code>, purchase-like subjects`}</td>
        <td><input type="date" value="${esc(a.since)}" data-since="${a.id}" style="width:140px"></td>
        <td class="small">${a.counts.receipt || 0} receipts<div class="muted">${(a.counts.skipped || 0) + (a.counts.duplicate || 0)} skipped</div></td>
        <td class="small muted">${a.last_checked ? esc(a.last_checked.slice(0, 16)) : "never"}</td>
        <td class="num" style="white-space:nowrap"><button data-edit="${a.id}">Edit</button>
          <button data-pause="${a.id}" data-v="${a.active ? 0 : 1}">${a.active ? "Pause" : "Resume"}</button>
          <button class="danger" data-del="${a.id}">Remove</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="muted small">No accounts yet.</div>`}
  </div>

  <div class="panel" id="add">
    <h2>Add an account</h2>
    <div class="grid grid-3" style="align-items:end">
      <label class="field">Provider<select id="e-prov">${Object.entries(PROVIDERS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <label class="field">Email address<input id="e-addr" type="email" placeholder="you@gmail.com" autocomplete="off"></label>
      <label class="field">App password<input id="e-pw" type="password" placeholder="16-letter app password" autocomplete="new-password"></label>
    </div>
    <div id="e-help"></div>
    <details style="margin-top:10px"><summary class="small">Options</summary>
      <div class="grid grid-3" style="margin-top:8px">
        <label class="field">Look at emails since<input id="e-since" type="date" value="${firstOfLastMonth()}"></label>
        <label class="field" id="f-search">Gmail search<input id="e-search" value="category:purchases OR label:receipts"></label>
        <label class="field" id="f-folder" hidden>Folder<input id="e-folder" value="INBOX"></label>
        <label class="field" id="f-host" hidden>IMAP server<input id="e-host" placeholder="imap.example.com"></label>
      </div>
    </details>
    <div class="row" style="margin-top:12px"><span class="small muted" id="e-msg"></span><span class="spacer"></span>
      <button class="primary" data-add>Connect</button></div>
  </div>

  ${seen.length ? `<div class="panel"><h2>Recently looked at</h2>
    <p class="small muted" style="margin-top:0">Emails the scan checked. If a real receipt was skipped, click <b>Read again</b> and it's read on the next scan.</p>
    <div class="table-wrap" style="max-height:420px;overflow:auto"><table><thead><tr><th>Date</th><th>From</th><th>Subject</th><th>Result</th><th></th></tr></thead><tbody>
    ${seen.slice(0, 150).map((s) => `<tr><td class="small">${esc(s.sent_at || "")}</td><td class="small">${esc((s.sender || "").replace(/<.*>/, "").trim() || s.sender)}</td>
      <td class="small">${esc(s.subject)}</td>
      <td class="small">${s.status === "receipt" ? `<a href="#/receipts?open=${s.receipt_id}" class="good">Receipt</a>`
        : s.status === "retry" ? `<span class="accent">Will read next scan</span>`
        : `<span class="${s.status === "error" ? "bad" : "muted"}">${esc(s.reason || s.status)}</span>`}</td>
      <td>${s.status === "skipped" || s.status === "error" ? `<button class="small" data-retry="${s.account_id}" data-mid="${esc(s.message_id)}">Read again</button>` : ""}</td></tr>`).join("")}
    </tbody></table></div></div>` : ""}
  </div>`;

  const $ = (q) => el.querySelector(q);
  const syncProvider = () => {
    const p = $("#e-prov").value;
    $("#e-help").innerHTML = p === "gmail" ? HELP_GMAIL : p === "outlook" ? HELP_OUTLOOK : "";
    $("#f-search").hidden = p !== "gmail";
    $("#f-folder").hidden = p === "gmail";
    $("#f-host").hidden = p !== "other";
    $("#e-addr").placeholder = p === "gmail" ? "you@gmail.com" : p === "outlook" ? "you@outlook.com" : "you@example.com";
  };
  $("#e-prov").onchange = syncProvider;
  $("#e-addr").oninput = () => {
    const v = $("#e-addr").value.toLowerCase();
    if (v.endsWith("@gmail.com")) $("#e-prov").value = "gmail";
    else if (/@(outlook|hotmail|live|msn)\./.test(v)) $("#e-prov").value = "outlook";
    else return;
    syncProvider();
  };
  syncProvider();

  $("[data-add]").onclick = async (e) => {
    const p = $("#e-prov").value;
    const body = { provider: p, address: $("#e-addr").value.trim(), password: $("#e-pw").value, since: $("#e-since").value,
      ...(p === "gmail" ? { search: $("#e-search").value.trim() } : { folder: $("#e-folder").value.trim() || "INBOX" }),
      ...(p === "other" ? { host: $("#e-host").value.trim() } : {}) };
    if (!body.address || !body.password) return toast("Enter the address and app password", { error: true });
    e.target.disabled = true;
    $("#e-msg").innerHTML = `<span class="spinner"></span> Signing in…`;
    try {
      await api("/email/accounts", { method: "POST", body });
      toast(`Connected ${body.address}`);
      render();
    } catch (err) {
      $("#e-msg").innerHTML = `<span class="bad">${esc(err.message)}</span>`;
      e.target.disabled = false;
    }
  };

  el.querySelectorAll("[data-since]").forEach((i) => (i.onchange = async () => {
    try { await api(`/email/accounts/${i.dataset.since}`, { method: "PATCH", body: { since: i.value } }); toast("Saved"); } catch (err) { fail(err); }
  }));
  el.querySelectorAll("[data-pause]").forEach((b) => (b.onclick = async () => {
    await api(`/email/accounts/${b.dataset.pause}`, { method: "PATCH", body: { active: +b.dataset.v } }); render();
  }));
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Remove this account? Its saved password is deleted too. Receipts already found are kept.")) return;
    await api(`/email/accounts/${b.dataset.del}`, { method: "DELETE" }); render();
  }));
  el.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => editDialog(accts.find((a) => a.id === +b.dataset.edit))));
  el.querySelectorAll("[data-retry]").forEach((b) => (b.onclick = async () => {
    await api("/email/seen/retry", { method: "POST", body: { account_id: +b.dataset.retry, message_id: b.dataset.mid } });
    b.closest("td").previousElementSibling.innerHTML = `<span class="accent">Will read next scan</span>`;
    b.remove();
  }));
}

function editDialog(a) {
  modal({
    title: `Edit ${a.address}`,
    body: `<div class="stack">
      ${a.provider === "gmail" ? `<label class="field">Gmail search<input name="search" value="${esc(a.search || "")}"></label>
        <div class="small muted">Any Gmail search works, e.g. <code>category:purchases OR label:receipts OR from:meijer.com</code></div>`
        : `<label class="field">Folder<input name="folder" value="${esc(a.folder)}"></label>`}
      <label class="field">New app password (leave blank to keep)<input name="password" type="password" autocomplete="new-password"></label>
    </div>`,
    foot: `<button data-close>Cancel</button><button class="primary" data-save>Save</button>`,
    onMount(m, close) {
      m.querySelector("[data-close]").onclick = close;
      m.querySelector("[data-save]").onclick = async (e) => {
        const body = {};
        m.querySelectorAll("input").forEach((i) => { if (i.value || i.name !== "password") body[i.name] = i.value; });
        e.target.disabled = true;
        try { await api(`/email/accounts/${a.id}`, { method: "PATCH", body }); close(); render(); }
        catch (err) { fail(err); e.target.disabled = false; }
      };
    },
  });
}

// ------------------------------------------------------------------ Receipts page panel

export async function scanPanel(el, alive) {
  const accts = await api("/email/accounts");
  const active = accts.filter((a) => a.active);
  if (!active.length) {
    el.innerHTML = `<button style="width:100%;justify-content:center" onclick="location.hash='#/settings?tab=email'">✉️ Connect email accounts</button>`;
    return;
  }
  const last = active.map((a) => a.last_checked).filter(Boolean).sort().pop();
  const draw = (st) => {
    if (st.running) {
      const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
      el.innerHTML = `<div class="small"><span class="spinner"></span> ${esc(st.step || "Checking email…")}</div>
        <div class="bar" style="margin:6px 0"><span style="width:${pct}%"></span></div>
        <button class="small" data-cancel>Stop</button>`;
      el.querySelector("[data-cancel]").onclick = () => api("/email/scan/cancel", { method: "POST" });
      return;
    }
    const res = (st.results || []);
    const found = res.reduce((n, r) => n + (r.receipts || 0), 0);
    const errs = res.filter((r) => r.error);
    el.innerHTML = `<button style="width:100%;justify-content:center" data-scan>✉️ Check email (${active.length} account${active.length > 1 ? "s" : ""})</button>
      <div class="small muted" style="margin-top:4px">${st.finished_at ? `Last run found ${found} new receipt${found === 1 ? "" : "s"}.` : last ? `Last checked ${esc(last.slice(0, 16))}` : "Not checked yet."}
        ${res.some((r) => r.remaining) ? " More emails left - run it again." : ""} <a href="#/settings?tab=email">Accounts</a></div>
      ${errs.map((r) => `<div class="small bad">${esc(r.address)}: ${esc(r.error)}</div>`).join("")}`;
    el.querySelector("[data-scan]").onclick = async () => {
      try { draw(await api("/email/scan", { method: "POST", body: {} })); poll(); } catch (e) { fail(e); }
    };
  };
  let wasRunning = false;
  const poll = async () => {
    if (!alive()) return;
    const st = await api("/email/scan");
    if (!alive()) return;
    draw(st);
    if (st.running) { wasRunning = true; setTimeout(poll, 2000); }
    else if (wasRunning) { wasRunning = false; render(); }
  };
  draw(await api("/email/scan"));
  poll();
}
