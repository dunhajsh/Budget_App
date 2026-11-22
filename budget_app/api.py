"""JSON API used by the single-page front end."""
import io
import json
import mimetypes
import os
import re
from datetime import date, datetime

from flask import Blueprint, abort, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from . import ai, emailscan, itemmemory, matching, receipts, spending
from .budget import categories_tree, history, month_add, month_summary
from .categorize import (apply_rules_to_uncategorized, find_category, merchant_key, set_single_category,
                         upsert_rule)
from .db import get_db, get_setting, rows, set_setting
from .importers import amazon, excel, statement

bp = Blueprint("api", __name__, url_prefix="/api")

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _month(value=None):
    m = value or request.args.get("month")
    if not m:
        return date.today().strftime("%Y-%m")
    if not MONTH_RE.match(m):
        abort(400, "month must be YYYY-MM")
    return m


def _body():
    return request.get_json(silent=True) or {}


@bp.errorhandler(ValueError)
def _value_error(e):
    return jsonify(error=str(e)), 400


@bp.errorhandler(400)
@bp.errorhandler(404)
def _http_error(e):
    return jsonify(error=getattr(e, "description", str(e))), e.code


# ------------------------------------------------------------------ status / settings

@bp.get("/status")
def status():
    db = get_db()
    latest = db.execute(
        "SELECT MAX(budget_month) m FROM transactions WHERE source != 'excel'"
    ).fetchone()["m"]
    latest_alloc = db.execute("SELECT MAX(month) m FROM allocations").fetchone()["m"]
    months = [r["m"] for r in db.execute(
        "SELECT DISTINCT budget_month m FROM transactions UNION SELECT DISTINCT month FROM allocations ORDER BY m DESC"
    )]
    return jsonify(
        ai_enabled=ai.enabled(db),
        ai_label=ai.describe(db),
        ai_local=ai.provider(db) == "ollama",
        has_categories=bool(db.execute("SELECT 1 FROM categories LIMIT 1").fetchone()),
        latest_month=max(filter(None, [latest, latest_alloc]), default=date.today().strftime("%Y-%m")),
        spreadsheet_through=db.execute("SELECT MAX(budget_month) FROM transactions WHERE source = 'excel'").fetchone()[0],
        months=months,
        accounts=[r["account"] for r in db.execute(
            "SELECT DISTINCT account FROM transactions WHERE account IS NOT NULL AND source != 'excel' ORDER BY account")],
    )


@bp.get("/settings")
def get_settings():
    db = get_db()
    key = get_setting(db, "anthropic_api_key") or ""
    return jsonify(
        api_key_set=bool(key) or bool(os.environ.get("ANTHROPIC_API_KEY")),
        api_key_source="settings" if key else ("environment" if os.environ.get("ANTHROPIC_API_KEY") else None),
        api_key_hint=("…" + key[-4:]) if key else "",
        categorization_notes=get_setting(db, "categorization_notes") or "",
        default_account=get_setting(db, "default_account") or "Credit Card",
        ai_provider=ai.provider(db),
        ollama_url=ai.ollama_url(db),
        ollama_model=ai.ollama_model(db),
        ollama_think=get_setting(db, "ollama_think") or "auto",
        income_month_offset=int(get_setting(db, "income_month_offset") or 0),
    )


@bp.put("/settings")
def put_settings():
    db = get_db()
    b = _body()
    if "anthropic_api_key" in b:
        set_setting(db, "anthropic_api_key", (b["anthropic_api_key"] or "").strip())
    if b.get("ai_provider") not in (None, "anthropic", "ollama"):
        abort(400, "ai_provider must be anthropic or ollama")
    if b.get("ollama_think") not in (None, "auto", "always", "never"):
        abort(400, "ollama_think must be auto, always or never")
    if "income_month_offset" in b:
        if str(b["income_month_offset"]) not in ("0", "1"):
            abort(400, "income_month_offset must be 0 or 1")
        set_setting(db, "income_month_offset", str(b["income_month_offset"]))
    for k in ("categorization_notes", "default_account", "ai_provider", "ollama_url", "ollama_model", "ollama_think"):
        if k in b:
            set_setting(db, k, (b[k] or "").strip() if k.startswith("ollama") else b[k])
    db.commit()
    return get_settings()


@bp.get("/ai/ollama-models")
def ollama_models():
    db = get_db()
    url = request.args.get("url")
    if url:  # test a URL before saving it
        set_setting(db, "ollama_url", url.strip())
    try:
        return jsonify(ok=True, url=ai.ollama_url(db), models=ai.ollama_models(db))
    except ai.AIError as e:
        return jsonify(ok=False, url=ai.ollama_url(db), error=str(e), models=[])
    finally:
        if url:
            db.rollback()


# ------------------------------------------------------------------ categories

@bp.get("/categories")
def list_categories():
    db = get_db()
    return jsonify(categories_tree(db, include_archived=request.args.get("archived") == "1"))


@bp.post("/categories")
def create_category():
    db = get_db()
    b = _body()
    name = (b.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    parent = b.get("parent_id")
    order = db.execute("SELECT COALESCE(MAX(sort_order),0)+1 v FROM categories").fetchone()["v"]
    cur = db.execute(
        "INSERT INTO categories(name, parent_id, grp, default_amount, sort_order) VALUES (?, ?, ?, ?, ?)",
        (name, parent, b.get("grp") or ("" if parent else excel.group_for(name)), float(b.get("default_amount") or 0), order),
    )
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@bp.patch("/categories/<int:cid>")
def update_category(cid):
    db = get_db()
    b = _body()
    for field in ("name", "grp", "default_amount", "archived", "sort_order", "parent_id"):
        if field in b:
            db.execute(f"UPDATE categories SET {field} = ? WHERE id = ?", (b[field], cid))
    db.commit()
    return jsonify(ok=True)


@bp.delete("/categories/<int:cid>")
def delete_category(cid):
    db = get_db()
    used = db.execute(
        """SELECT COUNT(*) n FROM splits WHERE category_id IN (SELECT id FROM categories WHERE id = ? OR parent_id = ?)""",
        (cid, cid),
    ).fetchone()["n"]
    if used:
        db.execute("UPDATE categories SET archived = 1 WHERE id = ?", (cid,))
        db.commit()
        return jsonify(archived=True, message=f"Archived instead of deleted - {used} transactions use it.")
    db.execute("DELETE FROM categories WHERE id = ?", (cid,))
    db.commit()
    return jsonify(deleted=True)


# ------------------------------------------------------------------ summary / reports

@bp.get("/summary")
def summary():
    return jsonify(month_summary(get_db(), _month()))


@bp.get("/history")
def get_history():
    end = _month(request.args.get("end"))
    start = _month(request.args.get("start") or month_add(end, -11))
    return jsonify(history(get_db(), start, end))


@bp.get("/reports/merchants")
def report_merchants():
    db = get_db()
    end = _month(request.args.get("end"))
    start = _month(request.args.get("start") or month_add(end, -2))
    q = """SELECT t.merchant, COUNT(*) n, ROUND(SUM(t.amount),2) total
           FROM transactions t WHERE t.excluded = 0 AND t.source NOT IN ('excel')
             AND t.budget_month BETWEEN ? AND ? GROUP BY t.merchant ORDER BY total DESC LIMIT 25"""
    return jsonify(rows(db.execute(q, (start, end))))


@bp.get("/reports/category/<int:cid>")
def report_category(cid):
    db = get_db()
    end = _month(request.args.get("end"))
    start = _month(request.args.get("start") or month_add(end, -11))
    subs = rows(db.execute(
        """SELECT c.id, c.name, ROUND(COALESCE(SUM(s.amount),0),2) total
           FROM categories c LEFT JOIN splits s ON s.category_id = c.id
           LEFT JOIN transactions t ON t.id = s.transaction_id
           WHERE (c.id = ? OR c.parent_id = ?) AND (t.id IS NULL OR (t.excluded = 0 AND t.budget_month BETWEEN ? AND ?))
           GROUP BY c.id ORDER BY total DESC""",
        (cid, cid, start, end),
    ))
    items = rows(db.execute(
        """SELECT ri.name, ROUND(SUM(ri.amount),2) total, COUNT(*) n FROM receipt_items ri
           JOIN receipts r ON r.id = ri.receipt_id
           JOIN categories c ON c.id = ri.category_id
           WHERE (c.id = ? OR c.parent_id = ?) AND r.purchase_date BETWEEN ? AND ?
           GROUP BY LOWER(ri.name) ORDER BY total DESC LIMIT 25""",
        (cid, cid, start + "-01", end + "-31"),
    ))
    return jsonify(subcategories=subs, top_items=items)


@bp.get("/reports/spending")
def report_spending():
    end = _month(request.args.get("end"))
    start = _month(request.args.get("start") or month_add(end, -11))
    return jsonify(spending.overview(get_db(), start, end))


@bp.get("/reports/spending/<int:cid>")
def report_spending_detail(cid):
    end = _month(request.args.get("end"))
    start = _month(request.args.get("start") or end)
    ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.strip().isdigit()]
    return jsonify(spending.detail(get_db(), cid, start, end, only_own=request.args.get("own") == "1", only_ids=ids or None))


# ------------------------------------------------------------------ transactions

def _txn_rows(db, where, args, limit=2000):
    q = f"""SELECT t.*, (SELECT json_group_array(json_object('id', s.id, 'category_id', s.category_id,
                        'amount', s.amount, 'memo', s.memo)) FROM splits s WHERE s.transaction_id = t.id) AS splits_json,
                   COALESCE((SELECT l.receipt_id FROM receipt_links l WHERE l.transaction_id = t.id ORDER BY l.receipt_id LIMIT 1),
                            (SELECT r.id FROM receipts r WHERE r.transaction_id = t.id LIMIT 1)) AS receipt_id,
                   (SELECT COUNT(*) FROM receipt_links l WHERE l.transaction_id = t.id) AS receipt_count
            FROM transactions t WHERE {where} ORDER BY t.date DESC, t.id DESC LIMIT {int(limit)}"""
    out = []
    for r in db.execute(q, args):
        d = dict(r)
        d["splits"] = json.loads(d.pop("splits_json") or "[]")
        out.append(d)
    return out


@bp.get("/transactions")
def list_transactions():
    db = get_db()
    where, args = ["t.replaced_by IS NULL"], []
    month = request.args.get("month")
    start, end = request.args.get("start"), request.args.get("end")
    if start or end:  # a range of budget months (either end may be open)
        if start:
            where.append("t.budget_month >= ?")
            args.append(_month(start))
        if end:
            where.append("t.budget_month <= ?")
            args.append(_month(end))
    elif month:
        where.append("t.budget_month = ?")
        args.append(_month(month))
    if request.args.get("include_excel") == "0":
        where.append("t.source != 'excel'")
    f = request.args.get("filter")
    if f == "uncategorized":
        where.append("t.excluded = 0 AND ABS(t.amount - COALESCE((SELECT SUM(amount) FROM splits s "
                     "WHERE s.transaction_id = t.id AND s.category_id IS NOT NULL),0)) > 0.005")
    elif f == "review":
        where.append("t.excluded = 0 AND t.reviewed = 0")
    elif f == "split":
        where.append("(SELECT COUNT(*) FROM splits s WHERE s.transaction_id = t.id) > 1")
    cat = request.args.get("category_id")
    if cat == "none":  # any part of the charge has no category
        where.append("t.excluded = 0 AND (NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id) "
                     "OR EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id AND s.category_id IS NULL))")
    elif cat:  # an envelope includes its subcategories
        where.append("EXISTS (SELECT 1 FROM splits s JOIN categories c ON c.id = s.category_id "
                     "WHERE s.transaction_id = t.id AND (c.id = ? OR c.parent_id = ?))")
        args += [int(cat), int(cat)]
    q = (request.args.get("q") or "").strip()
    if q:
        # Description, note, amount, the category (or its envelope) any part is filed in, and split notes.
        where.append("""(t.description LIKE ? OR t.merchant LIKE ? OR t.notes LIKE ? OR printf('%.2f', t.amount) LIKE ?
                         OR EXISTS (SELECT 1 FROM splits s LEFT JOIN categories c ON c.id = s.category_id
                                    LEFT JOIN categories p ON p.id = c.parent_id
                                    WHERE s.transaction_id = t.id AND (c.name LIKE ? OR p.name LIKE ? OR s.memo LIKE ?)))""")
        args += [f"%{q.lstrip('$')}%" if i == 3 else f"%{q}%" for i in range(7)]
    limit = min(int(request.args.get("limit") or 2000), 5000)
    return jsonify(_txn_rows(db, " AND ".join(where), args, limit=limit))


@bp.post("/transactions")
def create_transaction():
    db = get_db()
    b = _body()
    d = b.get("date") or date.today().isoformat()
    amount = round(float(b["amount"]), 2)
    desc = (b.get("description") or "").strip() or "Manual entry"
    cur = db.execute(
        """INSERT INTO transactions(date, description, merchant, amount, account, source, budget_month, reviewed, notes)
           VALUES (?, ?, ?, ?, ?, 'manual', ?, 1, ?)""",
        (d, desc, merchant_key(desc), amount, b.get("account") or "Cash", _month(b.get("budget_month") or d[:7]),
         b.get("notes")),
    )
    if b.get("category_id"):
        db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)",
                   (cur.lastrowid, int(b["category_id"]), amount))
    db.commit()
    return jsonify(id=cur.lastrowid), 201


def _similar_ids(db, txn):
    key = txn["merchant"] or merchant_key(txn["description"])
    return [r["id"] for r in db.execute(
        """SELECT id FROM transactions WHERE id != ? AND merchant = ? AND excluded = 0 AND reviewed = 0
             AND source != 'excel'""",
        (txn["id"], key),
    )]


@bp.patch("/transactions/<int:tid>")
def update_transaction(tid):
    db = get_db()
    b = _body()
    t = db.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not t:
        abort(404)
    result = {"ok": True}
    for field in ("description", "notes", "account", "date"):
        if field in b:
            db.execute(f"UPDATE transactions SET {field} = ? WHERE id = ?", (b[field], tid))
    if "budget_month" in b:
        db.execute("UPDATE transactions SET budget_month = ? WHERE id = ?", (_month(b["budget_month"]), tid))
    if "excluded" in b:
        db.execute("UPDATE transactions SET excluded = ? WHERE id = ?", (1 if b["excluded"] else 0, tid))
    if "amount" in b and t["source"] == "manual":
        amt = round(float(b["amount"]), 2)
        db.execute("UPDATE transactions SET amount = ? WHERE id = ?", (amt, tid))
        n = db.execute("SELECT COUNT(*) n FROM splits WHERE transaction_id = ?", (tid,)).fetchone()["n"]
        if n == 1:
            db.execute("UPDATE splits SET amount = ? WHERE transaction_id = ?", (amt, tid))
    if "reviewed" in b:
        db.execute("UPDATE transactions SET reviewed = ? WHERE id = ?", (1 if b["reviewed"] else 0, tid))
    if "category_id" in b:
        cid = b["category_id"] or None
        set_single_category(db, tid, cid, reviewed=True)
        if cid and b.get("remember"):
            pattern = (b.get("pattern") or t["merchant"] or merchant_key(t["description"])).strip()
            result["rule_id"] = upsert_rule(db, pattern, cid)
        if cid and b.get("apply_similar"):
            ids = _similar_ids(db, t)
            for i in ids:
                set_single_category(db, i, cid, reviewed=True)
            result["applied_to"] = len(ids)
    db.commit()
    return jsonify(result)


@bp.get("/transactions/<int:tid>/similar")
def similar(tid):
    db = get_db()
    t = db.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not t:
        abort(404)
    return jsonify(merchant=t["merchant"], count=len(_similar_ids(db, t)))


def _after_charges_removed(db, rids):
    """Receipts that lost a charge: point them at a remaining one, or back to reviewed."""
    for rid in rids:
        left = matching.links_of(db, rid)
        if left:
            db.execute("UPDATE receipts SET transaction_id = ? WHERE id = ?", (left[0], rid))
            for t in left:
                receipts.rebuild_splits(db, t)
        else:
            db.execute("UPDATE receipts SET transaction_id = NULL, status = 'reviewed' WHERE id = ? AND status = 'applied'", (rid,))


@bp.delete("/transactions/<int:tid>")
def delete_transaction(tid):
    db = get_db()
    hit = matching.receipts_of(db, tid)
    db.execute("UPDATE receipts SET transaction_id = NULL WHERE transaction_id = ?", (tid,))
    db.execute("DELETE FROM transactions WHERE id = ?", (tid,))
    _after_charges_removed(db, hit)
    db.commit()
    return jsonify(ok=True)


@bp.put("/transactions/<int:tid>/splits")
def put_splits(tid):
    db = get_db()
    t = db.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not t:
        abort(404)
    splits = _body().get("splits") or []
    total = round(sum(float(s.get("amount") or 0) for s in splits), 2)
    if abs(total - t["amount"]) > 0.005:
        raise ValueError(f"Splits add up to {total:.2f} but the transaction is {t['amount']:.2f}")
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (tid,))
    for s in splits:
        if abs(float(s.get("amount") or 0)) < 0.005:
            continue
        db.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) VALUES (?, ?, ?, ?)",
                   (tid, s.get("category_id") or None, round(float(s["amount"]), 2), s.get("memo")))
    db.execute("UPDATE transactions SET reviewed = 1 WHERE id = ?", (tid,))
    db.commit()
    return jsonify(ok=True)


@bp.post("/transactions/bulk")
def bulk_update():
    db = get_db()
    b = _body()
    ids = [int(i) for i in b.get("ids") or []]
    if "category_id" in b:
        for i in ids:
            set_single_category(db, i, b["category_id"] or None, reviewed=True)
    if b.get("reviewed"):
        db.executemany("UPDATE transactions SET reviewed = 1 WHERE id = ?", [(i,) for i in ids])
    if "excluded" in b:
        db.executemany("UPDATE transactions SET excluded = ? WHERE id = ?", [(1 if b["excluded"] else 0, i) for i in ids])
    if b.get("budget_month"):
        m = _month(b["budget_month"])
        db.executemany("UPDATE transactions SET budget_month = ? WHERE id = ?", [(m, i) for i in ids])
    db.commit()
    return jsonify(updated=len(ids))


@bp.post("/transactions/ai-categorize")
def ai_categorize():
    db = get_db()
    month = _month(_body().get("month"))
    txns = db.execute(
        """SELECT t.* FROM transactions t WHERE t.budget_month = ? AND t.excluded = 0 AND t.source != 'excel'
             AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)""",
        (month,),
    ).fetchall()
    if not txns:
        return jsonify(categorized=0, total=0)
    entries = [f"{t['description']} | ${t['amount']:.2f} | {t['date']} | card category: {t['statement_category'] or '-'}"
               for t in txns]
    try:
        res = ai.categorize(db, entries)
    except ai.AIError as e:
        return jsonify(error=str(e)), 502
    for idx, cid in res.items():
        set_single_category(db, txns[idx]["id"], cid, reviewed=False)
    db.commit()
    return jsonify(categorized=len(res), total=len(txns))


# ------------------------------------------------------------------ imports

@bp.post("/import/statement")
def import_statement():
    db = get_db()
    f = request.files.get("file")
    if not f:
        raise ValueError("Choose a CSV file")
    text = f.read().decode("utf-8-sig", errors="replace")
    account = (request.form.get("account") or "").strip() or None
    res = statement.import_statement(db, text, f.filename, account, request.form.get("budget_month") or None)
    if res.get("kind") == "card" and account:
        set_setting(db, "default_account", account)
    db.commit()
    return jsonify(res)


@bp.post("/import/statement/preview")
def preview_statement():
    f = request.files.get("file")
    if not f:
        raise ValueError("Choose a CSV file")
    db = get_db()
    p = statement.preview(db, f.read().decode("utf-8-sig", errors="replace"), f.filename)
    p["account"] = statement.guess_account(f.filename, p["kind"], get_setting(db, "default_account"))
    return jsonify(p)


@bp.get("/import/batches")
def list_batches():
    return jsonify(rows(get_db().execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 50")))


@bp.delete("/import/batches/<int:bid>")
def undo_batch(bid):
    db = get_db()
    hit = [r["receipt_id"] for r in db.execute(
        "SELECT DISTINCT receipt_id FROM receipt_links WHERE transaction_id IN (SELECT id FROM transactions WHERE batch_id = ?)", (bid,))]
    db.execute("UPDATE receipts SET transaction_id = NULL WHERE transaction_id IN "
               "(SELECT id FROM transactions WHERE batch_id = ?)", (bid,))
    db.execute("UPDATE transactions SET excluded = 0, replaced_by = NULL WHERE replaced_by = ?", (bid,))
    n = db.execute("DELETE FROM transactions WHERE batch_id = ?", (bid,)).rowcount
    _after_charges_removed(db, hit)
    n += db.execute("DELETE FROM income WHERE batch_id = ?", (bid,)).rowcount
    db.execute("DELETE FROM import_batches WHERE id = ?", (bid,))
    db.commit()
    return jsonify(deleted=n)


@bp.post("/import/excel")
def import_excel():
    db = get_db()
    f = request.files.get("file")
    if not f:
        raise ValueError("Choose your budget .xlsx file")
    path = os.path.join(current_app.config["DATA_DIR"], "import_" + secure_filename(f.filename or "budget.xlsx"))
    f.save(path)
    try:
        res = excel.import_workbook(db, path, replace=request.form.get("replace") == "1")
        db.commit()
    finally:
        os.remove(path)
    return jsonify(res)


@bp.post("/import/amazon")
def import_amazon():
    db = get_db()
    f = request.files.get("file")
    if not f:
        raise ValueError("Choose your Amazon order history file")
    since = request.form.get("since")
    since = date.fromisoformat(since) if since else None
    res = amazon.import_orders(db, f.filename or "orders.csv", f.read(), since=since)
    db.commit()
    return jsonify(res)


# ------------------------------------------------------------------ budget: allocations & income

@bp.put("/allocations")
def put_allocations():
    db = get_db()
    b = _body()
    month = _month(b.get("month"))
    for it in b.get("items") or []:
        db.execute(
            "INSERT INTO allocations(category_id, month, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(category_id, month) DO UPDATE SET amount = excluded.amount",
            (int(it["category_id"]), month, round(float(it.get("amount") or 0), 2)),
        )
    db.commit()
    return jsonify(ok=True)


@bp.post("/allocations/fill")
def fill_allocations():
    db = get_db()
    b = _body()
    month = _month(b.get("month"))
    mode = b.get("mode", "defaults")
    overwrite = bool(b.get("overwrite"))
    n = 0
    for c in db.execute("SELECT id, default_amount FROM categories WHERE parent_id IS NULL AND archived = 0").fetchall():
        if mode == "last_month":
            r = db.execute("SELECT amount FROM allocations WHERE category_id = ? AND month = ?",
                           (c["id"], month_add(month, -1))).fetchone()
            amt = r["amount"] if r else 0
        else:
            amt = c["default_amount"] or 0
        existing = db.execute("SELECT amount FROM allocations WHERE category_id = ? AND month = ?", (c["id"], month)).fetchone()
        if existing and existing["amount"] and not overwrite:
            continue
        db.execute(
            "INSERT INTO allocations(category_id, month, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(category_id, month) DO UPDATE SET amount = excluded.amount",
            (c["id"], month, amt),
        )
        n += 1
    db.commit()
    return jsonify(filled=n)


@bp.get("/income")
def list_income():
    return jsonify(rows(get_db().execute("SELECT * FROM income WHERE month = ? ORDER BY date, id", (_month(),))))


@bp.post("/income")
def add_income():
    db = get_db()
    b = _body()
    d = b.get("date") or date.today().isoformat()
    cur = db.execute("INSERT INTO income(month, date, amount, description) VALUES (?, ?, ?, ?)",
                     (_month(b.get("month") or d[:7]), d, round(float(b["amount"]), 2), b.get("description") or "Paycheck"))
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@bp.post("/transactions/<int:tid>/to-income")
def transaction_to_income(tid):
    """A deposit that is really income (e.g. a side job paid by Zelle): move it to the income list."""
    db = get_db()
    t = db.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not t:
        abort(404)
    if t["amount"] >= 0:
        raise ValueError("Only deposits/credits can be counted as income")
    month = _month(_body().get("month") or month_add(t["budget_month"], statement.income_offset(db)))
    db.execute("INSERT INTO income(month, date, amount, description, source, batch_id) VALUES (?, ?, ?, ?, 'statement', ?)",
               (month, t["date"], -t["amount"], t["description"], t["batch_id"]))
    # Keep the row (not budgeted) so re-importing the same statement doesn't bring the deposit back.
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (tid,))
    db.execute("UPDATE transactions SET excluded = 1, reviewed = 1, notes = ? WHERE id = ?",
               (f"Counted as income ({month}).", tid))
    db.commit()
    return jsonify(ok=True, month=month)


@bp.delete("/income/<int:iid>")
def delete_income(iid):
    db = get_db()
    db.execute("DELETE FROM income WHERE id = ?", (iid,))
    db.commit()
    return jsonify(ok=True)


# ------------------------------------------------------------------ rules & statement category mapping

@bp.get("/rules")
def list_rules():
    return jsonify(rows(get_db().execute(
        """SELECT r.*, c.name category, p.name parent FROM rules r JOIN categories c ON c.id = r.category_id
           LEFT JOIN categories p ON p.id = c.parent_id ORDER BY r.pattern""")))


@bp.post("/rules")
def create_rule():
    db = get_db()
    b = _body()
    if not b.get("pattern") or not b.get("category_id"):
        raise ValueError("Pattern and category are required")
    rid = upsert_rule(db, b["pattern"], int(b["category_id"]), b.get("match_type") or "contains")
    db.commit()
    return jsonify(id=rid), 201


@bp.patch("/rules/<int:rid>")
def update_rule(rid):
    db = get_db()
    b = _body()
    for field in ("pattern", "category_id", "match_type", "priority"):
        if field in b:
            db.execute(f"UPDATE rules SET {field} = ? WHERE id = ?", (b[field], rid))
    db.commit()
    return jsonify(ok=True)


@bp.delete("/rules/<int:rid>")
def delete_rule(rid):
    db = get_db()
    db.execute("DELETE FROM rules WHERE id = ?", (rid,))
    db.commit()
    return jsonify(ok=True)


@bp.post("/rules/apply")
def run_rules():
    db = get_db()
    n = apply_rules_to_uncategorized(db, _body().get("month"))
    db.commit()
    return jsonify(categorized=n)


@bp.get("/rules/test")
def test_rule():
    db = get_db()
    desc = request.args.get("description", "")
    cid, how = find_category(db, desc)
    return jsonify(category_id=cid, how=how, merchant=merchant_key(desc))


@bp.get("/statement-map")
def get_statement_map():
    db = get_db()
    seen = [r["c"] for r in db.execute(
        "SELECT DISTINCT statement_category c FROM transactions WHERE statement_category IS NOT NULL "
        "AND excluded = 0 ORDER BY c")]
    current = {r["statement_category"]: r["category_id"] for r in db.execute("SELECT * FROM statement_category_map")}
    return jsonify([{"statement_category": s, "category_id": current.get(s)} for s in sorted(set(seen) | set(current))])


@bp.put("/statement-map")
def put_statement_map():
    db = get_db()
    for it in _body().get("items") or []:
        if it.get("category_id"):
            db.execute("INSERT INTO statement_category_map VALUES (?, ?) ON CONFLICT(statement_category) "
                       "DO UPDATE SET category_id = excluded.category_id", (it["statement_category"], int(it["category_id"])))
        else:
            db.execute("DELETE FROM statement_category_map WHERE statement_category = ?", (it["statement_category"],))
    db.commit()
    return jsonify(ok=True)


# ------------------------------------------------------------------ recurring

@bp.get("/recurring")
def list_recurring():
    db = get_db()
    month = _month()
    out = rows(db.execute(
        """SELECT r.*, c.name category,
                  EXISTS(SELECT 1 FROM transactions t WHERE t.import_hash = 'recurring|' || r.id || '|' || ?) AS added
           FROM recurring r LEFT JOIN categories c ON c.id = r.category_id ORDER BY r.day_of_month, r.name""",
        (month,)))
    return jsonify(out)


@bp.get("/recurring/suggestions")
def recurring_suggestions():
    db = get_db()
    existing = {(r["category_id"], round(r["amount"], 2)) for r in db.execute("SELECT * FROM recurring")}
    return jsonify([s for s in excel.recurring_suggestions(db) if (s["category_id"], s["amount"]) not in existing])


@bp.post("/recurring")
def create_recurring():
    db = get_db()
    b = _body()
    cur = db.execute(
        "INSERT INTO recurring(name, category_id, amount, day_of_month, account) VALUES (?, ?, ?, ?, ?)",
        (b.get("name") or "Bill", b.get("category_id"), round(float(b["amount"]), 2), int(b.get("day_of_month") or 1),
         b.get("account") or "Checking"),
    )
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@bp.patch("/recurring/<int:rid>")
def update_recurring(rid):
    db = get_db()
    b = _body()
    for field in ("name", "category_id", "amount", "day_of_month", "account", "active"):
        if field in b:
            db.execute(f"UPDATE recurring SET {field} = ? WHERE id = ?", (b[field], rid))
    db.commit()
    return jsonify(ok=True)


@bp.delete("/recurring/<int:rid>")
def delete_recurring(rid):
    db = get_db()
    db.execute("DELETE FROM recurring WHERE id = ?", (rid,))
    db.commit()
    return jsonify(ok=True)


@bp.post("/recurring/add-month")
def add_recurring_month():
    db = get_db()
    b = _body()
    month = _month(b.get("month"))
    only = set(b.get("ids") or [])
    added = 0
    y, m = map(int, month.split("-"))
    for r in db.execute("SELECT * FROM recurring WHERE active = 1").fetchall():
        if only and r["id"] not in only:
            continue
        h = f"recurring|{r['id']}|{month}"
        if db.execute("SELECT 1 FROM transactions WHERE import_hash = ?", (h,)).fetchone():
            continue
        day = min(r["day_of_month"], 28)
        cur = db.execute(
            """INSERT INTO transactions(date, description, merchant, amount, account, source, budget_month, reviewed, import_hash)
               VALUES (?, ?, ?, ?, ?, 'recurring', ?, 1, ?)""",
            (f"{y:04d}-{m:02d}-{day:02d}", r["name"], r["name"].upper(), r["amount"], r["account"], month, h),
        )
        if r["category_id"]:
            db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)",
                       (cur.lastrowid, r["category_id"], r["amount"]))
        added += 1
    db.commit()
    return jsonify(added=added)


# ------------------------------------------------------------------ receipts

ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}


@bp.get("/receipts")
def list_receipts():
    db = get_db()
    if db.execute("SELECT 1 FROM receipts WHERE status = 'pending' LIMIT 1").fetchone():
        receipts.WORKER.kick(current_app.config["DATABASE"], current_app.config["RECEIPT_DIR"])
    where = "1=1"
    st = request.args.get("status")
    args = []
    if st == "open":
        where = "r.status NOT IN ('applied', 'reviewed')"
    q = f"""SELECT r.id, r.source, r.filename, r.merchant, r.purchase_date, r.total, r.tax, r.status, r.error,
                   r.transaction_id, r.created_at, t.description AS txn_description, t.amount AS txn_amount,
                   t.date AS txn_date,
                   (SELECT COUNT(*) FROM receipt_links l WHERE l.receipt_id = r.id) AS n_charges,
                   (SELECT COUNT(*) - 1 FROM receipt_links l WHERE l.transaction_id = r.transaction_id) AS n_shared,
                   (SELECT COUNT(*) FROM receipt_items i WHERE i.receipt_id = r.id) AS item_count,
                   (SELECT COUNT(*) FROM receipt_items i WHERE i.receipt_id = r.id AND i.category_id IS NULL) AS uncategorized
            FROM receipts r LEFT JOIN transactions t ON t.id = r.transaction_id
            WHERE {where} ORDER BY COALESCE(r.purchase_date, r.created_at) DESC, r.id DESC LIMIT 500"""
    return jsonify(rows(db.execute(q, args)))


def _store_upload(f):
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in ALLOWED:
        raise ValueError(f"Unsupported file type {ext or '(none)'} - use JPG, PNG, WEBP or PDF "
                         "(on iPhone, share/export the photo as JPG).")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    name = f"{stamp}_{secure_filename(f.filename) or 'receipt' + ext}"
    path = os.path.join(current_app.config["RECEIPT_DIR"], name)
    f.save(path)
    return name, path, ext


@bp.post("/receipts")
def upload_receipts():
    db = get_db()
    created = []
    text = (request.form.get("text") or "").strip()
    if text:
        cur = db.execute("INSERT INTO receipts(source, filename, status) VALUES ('text', 'Pasted text', 'pending')")
        rid = cur.lastrowid
        db.execute("UPDATE receipts SET raw_json = ? WHERE id = ?", (json.dumps({"text": text}), rid))
        created.append((rid, None, None, text))
    for f in request.files.getlist("files"):
        name, path, ext = _store_upload(f)
        cur = db.execute("INSERT INTO receipts(source, filename, stored_path, status) VALUES ('upload', ?, ?, 'pending')",
                         (f.filename, name))
        created.append((cur.lastrowid, path, ext, None))
    db.commit()
    # Read them in the background (a long receipt can take a minute with a local model).
    receipts.WORKER.kick(current_app.config["DATABASE"], current_app.config["RECEIPT_DIR"])
    return jsonify([receipts.get(db, rid) for rid, _, _, _ in created])


@bp.get("/receipts/<int:rid>")
def get_receipt(rid):
    db = get_db()
    r = receipts.get(db, rid)
    if not r:
        abort(404)
    r["candidates"] = receipts.candidates(db, rid)
    # a clear winner is pre-selected when nothing is applied or picked yet
    r["suggested"] = None
    if not r["links"] and not r["transaction_id"]:
        b = matching.best(db, rid)
        if b:
            key = lambda o: (sorted(t["id"] for t in o["transactions"]), sorted(w["id"] for w in o["with_receipts"]))
            r["suggested"] = next((i for i, o in enumerate(r["candidates"]) if key(o) == key(b)), None)
    return jsonify(r)


@bp.get("/receipts/<int:rid>/file")
def receipt_file(rid):
    db = get_db()
    r = db.execute("SELECT stored_path FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r or not r["stored_path"]:
        abort(404)
    path = os.path.join(current_app.config["RECEIPT_DIR"], os.path.basename(r["stored_path"]))
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@bp.patch("/receipts/<int:rid>")
def update_receipt(rid):
    db = get_db()
    b = _body()
    for field in ("merchant", "purchase_date", "subtotal", "tax", "total"):
        if field in b:
            db.execute(f"UPDATE receipts SET {field} = ? WHERE id = ?", (b[field], rid))
    learned = updated = 0
    if "items" in b:
        items = [dict(i, cat_source=None) for i in b["items"]]   # saved by you: your choices now
        receipts.save_items(db, rid, items)
        # Remember where you filed each item, and re-file the same items on receipts not reviewed yet.
        merchant = b.get("merchant") or (db.execute("SELECT merchant FROM receipts WHERE id = ?", (rid,)).fetchone() or {"merchant": None})["merchant"]
        keys = itemmemory.learn(db, items, merchant)
        learned, updated = len(keys), itemmemory.update_open_receipts(db, keys, exclude_receipt=rid)
    if "transaction_id" in b:
        db.execute("UPDATE receipts SET transaction_id = ? WHERE id = ?", (b["transaction_id"] or None, rid))
    if b.get("reviewed"):
        # Saved after looking it over: no longer "needs review" (an applied receipt stays applied).
        db.execute("UPDATE receipts SET status = 'reviewed', error = NULL WHERE id = ? AND status != 'applied'", (rid,))
    else:
        db.execute("UPDATE receipts SET status = CASE WHEN status IN ('pending','error') THEN 'parsed' ELSE status END "
                   "WHERE id = ?", (rid,))
    db.commit()
    out = receipts.get(db, rid)
    out["memory"] = {"learned": learned, "updated_other_receipts": updated}
    return jsonify(out)


@bp.post("/receipts/<int:rid>/parse")
def reparse_receipt(rid):
    db = get_db()
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r:
        abort(404)
    src = receipts.source_of(db, rid)
    data = mt = None
    if r["stored_path"]:
        path = os.path.join(current_app.config["RECEIPT_DIR"], os.path.basename(r["stored_path"]))
        ext = os.path.splitext(path)[1].lower()
        data = open(path, "rb").read()
        mt = "application/pdf" if ext == ".pdf" else mimetypes.guess_type(path)[0]
    if data is None and not src["text"]:
        raise ValueError("Nothing to re-read for this receipt")
    ok = receipts.run_parse(db, rid, data=data, media_type=mt, text=src["text"], email=src["email"])
    if ok and not r["transaction_id"]:
        m = receipts.best_match(db, rid)
        if m:
            db.execute("UPDATE receipts SET transaction_id = ? WHERE id = ?", (m["id"], rid))
    db.commit()
    return get_receipt(rid)


@bp.post("/receipts/<int:rid>/apply")
def apply_receipt(rid):
    db = get_db()
    b = _body()
    if b.get("create_new"):
        tid = receipts.create_transaction_from(db, rid, account=b.get("account") or "Cash",
                                               budget_month=b.get("budget_month"))
    else:
        # one charge or several (charged in parts); with_receipts: other receipts paid in the same charge
        ids = [int(x) for x in (b.get("transaction_ids") or [b.get("transaction_id")]) if x]
        if not ids:
            raise ValueError("Pick the card charge(s) this receipt belongs to")
        for other in b.get("with_receipts") or []:
            other = int(other)
            matching.set_links(db, other, list(dict.fromkeys(matching.links_of(db, other) + ids)))
        receipts.apply(db, rid, ids)
        tid = ids[0]
    db.commit()
    return jsonify(transaction_id=tid, receipt=receipts.get(db, rid))


@bp.post("/receipts/<int:rid>/unapply")
def unapply_receipt(rid):
    db = get_db()
    receipts.unapply(db, rid)
    db.commit()
    return jsonify(receipt=receipts.get(db, rid))


@bp.get("/receipts/<int:rid>/charges")
def search_charges(rid):
    return jsonify(matching.search(get_db(), request.args.get("q"), rid=rid))


@bp.delete("/receipts/<int:rid>")
def delete_receipt(rid):
    db = get_db()
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r:
        abort(404)
    if r["stored_path"]:
        p = os.path.join(current_app.config["RECEIPT_DIR"], os.path.basename(r["stored_path"]))
        if os.path.exists(p):
            os.remove(p)
    db.execute("DELETE FROM receipts WHERE id = ?", (rid,))
    db.commit()
    return jsonify(ok=True)


# ------------------------------------------------------------------ export

@bp.get("/export/year/<int:year>.xlsx")
def export_year(year):
    """Spreadsheet in the same shape as the original workbook (Spent / Total per month)."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    db = get_db()
    h = history(db, f"{year:04d}-01", f"{year:04d}-12")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = str(year)
    bold = Font(bold=True)
    ws.cell(1, 1, "Income").font = bold
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for i, n in enumerate(names):
        ws.cell(2, 2 + i * 2, n).font = bold
        ws.cell(3, 2 + i * 2, "Spent")
        ws.cell(3, 3 + i * 2, "Budgeted")
        ws.cell(1, 3 + i * 2, h["income"][i])
    ws.cell(2, 26, "Year").font = bold
    ws.cell(3, 26, "Spent")
    ws.cell(3, 27, "Budgeted")
    ws.cell(2, 28, "Available Dec").font = bold
    dec = month_summary(db, f"{year:04d}-12")
    avail = {e["id"]: e["available"] for e in dec["envelopes"]}
    r = 4
    fill = PatternFill("solid", fgColor="EEF2FF")
    for c in h["categories"]:
        ws.cell(r, 1, c["name"])
        for i in range(12):
            ws.cell(r, 2 + i * 2, c["spent"][i] or None)
            cell = ws.cell(r, 3 + i * 2, c["allocated"][i] or None)
            cell.fill = fill
        ws.cell(r, 26, round(sum(c["spent"]), 2))
        ws.cell(r, 27, round(sum(c["allocated"]), 2))
        ws.cell(r, 28, avail.get(c["id"]))
        r += 1
    ws.cell(r, 1, "Total").font = bold
    for i in range(12):
        ws.cell(r, 2 + i * 2, h["spent"][i]).font = bold
        ws.cell(r, 3 + i * 2, h["allocated"][i]).font = bold
    ws.column_dimensions["A"].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"budget_{year}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.get("/export/backup.db")
def backup():
    db = get_db()
    path = os.path.join(current_app.config["DATA_DIR"], "backup_export.db")
    if os.path.exists(path):
        os.remove(path)
    db.execute("VACUUM INTO ?", (path,))
    return send_file(path, as_attachment=True, download_name=f"budget_backup_{date.today().isoformat()}.db")


# ------------------------------------------------------------------ email receipts

def _default_since():
    first = date.today().replace(day=1)
    return month_add(first.strftime("%Y-%m"), -1) + "-01"


@bp.get("/email/accounts")
def email_accounts():
    db = get_db()
    return jsonify([emailscan.account_dict(db, a) for a in db.execute("SELECT * FROM email_accounts ORDER BY id")])


@bp.post("/email/accounts")
def add_email_account():
    db = get_db()
    b = _body()
    address = (b.get("address") or "").strip().lower()
    password = (b.get("password") or "").replace(" ", "") if b.get("provider") == "gmail" else (b.get("password") or "").strip()
    provider = b.get("provider") or ("gmail" if address.endswith(("@gmail.com", "@googlemail.com")) else "other")
    if "@" not in address or not password:
        raise ValueError("Enter the email address and its app password")
    if provider not in emailscan.PRESETS:
        raise ValueError("Unknown provider")
    preset = emailscan.PRESETS[provider]
    acct = {
        "address": address, "provider": provider,
        "host": (b.get("host") or preset["host"]).strip(), "port": int(b.get("port") or preset["port"]),
        "folder": (b.get("folder") or preset["folder"]).strip(),
        "search": (b.get("search") if b.get("search") is not None else preset["search"]) or None,
        "since": b.get("since") or _default_since(),
    }
    if not acct["host"]:
        raise ValueError("Enter the IMAP server name")
    date.fromisoformat(acct["since"])
    try:
        emailscan.login(acct, password).logout()   # make sure it works before saving anything
        emailscan.set_password(address, password)
    except emailscan.EmailError as e:
        return jsonify(error=str(e)), 400
    db.execute(
        """INSERT INTO email_accounts(address, provider, host, port, folder, search, since) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(address) DO UPDATE SET provider = excluded.provider, host = excluded.host, port = excluded.port,
               folder = excluded.folder, search = excluded.search, since = excluded.since, active = 1, last_error = NULL""",
        (acct["address"], acct["provider"], acct["host"], acct["port"], acct["folder"], acct["search"], acct["since"]),
    )
    db.commit()
    return email_accounts()


@bp.patch("/email/accounts/<int:aid>")
def update_email_account(aid):
    db = get_db()
    a = db.execute("SELECT * FROM email_accounts WHERE id = ?", (aid,)).fetchone()
    if not a:
        abort(404)
    b = _body()
    for k in ("folder", "search", "since", "active"):
        if k in b:
            if k == "since":
                date.fromisoformat(b[k])
            db.execute(f"UPDATE email_accounts SET {k} = ? WHERE id = ?", (b[k], aid))
    if b.get("password"):
        pw = b["password"].replace(" ", "") if a["provider"] == "gmail" else b["password"].strip()
        try:
            emailscan.login(dict(a), pw).logout()
            emailscan.set_password(a["address"], pw)
        except emailscan.EmailError as e:
            return jsonify(error=str(e)), 400
        db.execute("UPDATE email_accounts SET last_error = NULL WHERE id = ?", (aid,))
    db.commit()
    return email_accounts()


@bp.delete("/email/accounts/<int:aid>")
def delete_email_account(aid):
    db = get_db()
    a = db.execute("SELECT address FROM email_accounts WHERE id = ?", (aid,)).fetchone()
    if a:
        emailscan.delete_password(a["address"])
        db.execute("DELETE FROM email_accounts WHERE id = ?", (aid,))
        db.commit()
    return email_accounts()


@bp.get("/email/scan")
def email_scan_status():
    return jsonify(emailscan.JOB.status())


@bp.post("/email/scan")
def email_scan_start():
    db = get_db()
    if not ai.enabled(db):
        raise ValueError("Turn on AI first (Settings → AI & receipts) - it's what reads the emails.")
    if not db.execute("SELECT 1 FROM email_accounts WHERE active = 1").fetchone():
        raise ValueError("Add an email account first (Settings → Email).")
    ids = _body().get("account_ids")
    try:
        emailscan.JOB.start(current_app.config["DATABASE"], current_app.config["RECEIPT_DIR"], ids)
    except emailscan.EmailError as e:
        return jsonify(error=str(e)), 409
    return jsonify(emailscan.JOB.status())


@bp.post("/email/scan/cancel")
def email_scan_cancel():
    emailscan.JOB.cancel()
    return jsonify(emailscan.JOB.status())


@bp.get("/email/seen")
def email_seen():
    db = get_db()
    st = request.args.get("status")
    q = """SELECT s.*, a.address FROM email_seen s JOIN email_accounts a ON a.id = s.account_id"""
    args = []
    if st:
        q += " WHERE s.status = ?"
        args.append(st)
    q += " ORDER BY s.sent_at DESC, s.seen_at DESC LIMIT 300"
    return jsonify(rows(db.execute(q, args)))


@bp.post("/email/seen/retry")
def email_seen_retry():
    """Forget that an email was looked at, so the next scan reads it (even if the subject filter skipped it)."""
    db = get_db()
    b = _body()
    db.execute("UPDATE email_seen SET status = 'retry' WHERE account_id = ? AND message_id = ?",
               (b.get("account_id"), b.get("message_id")))
    db.commit()
    return jsonify(ok=True)


# ------------------------------------------------------------------ item memory

@bp.get("/item-memory")
def list_item_memory():
    db = get_db()
    return jsonify(rows(db.execute(
        """SELECT m.key, m.name, m.category_id, m.merchant, m.times, m.updated_at FROM item_memory m
           ORDER BY m.updated_at DESC, m.name""")))


@bp.patch("/item-memory")
def update_item_memory():
    db = get_db()
    b = _body()
    k, cid = b.get("key"), b.get("category_id")
    if not k or not cid or not db.execute("SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone():
        raise ValueError("key and a valid category_id are required")
    db.execute("UPDATE item_memory SET category_id = ?, updated_at = datetime('now') WHERE key = ?", (cid, k))
    n = itemmemory.update_open_receipts(db, [k])
    db.commit()
    return jsonify(ok=True, updated_other_receipts=n)


@bp.delete("/item-memory")
def delete_item_memory():
    db = get_db()
    db.execute("DELETE FROM item_memory WHERE key = ?", (_body().get("key") or request.args.get("key"),))
    db.commit()
    return jsonify(ok=True)
