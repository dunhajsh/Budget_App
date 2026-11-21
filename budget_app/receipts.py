"""Receipts: store, parse, match to a card transaction, and split it by category."""
import json
import re
from collections import defaultdict
from datetime import date

from . import ai


def _d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2 and t not in {"the", "inc", "com", "store", "llc"}}


def get(db, rid):
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r:
        return None
    r = dict(r)
    r["items"] = [dict(i) for i in db.execute("SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY sort_order, id", (rid,))]
    r["items_total"] = round(sum(i["amount"] for i in r["items"]), 2)
    if r["transaction_id"]:
        t = db.execute("SELECT * FROM transactions WHERE id = ?", (r["transaction_id"],)).fetchone()
        r["transaction"] = dict(t) if t else None
    raw = json.loads(r.pop("raw_json", None) or "{}")
    r["email"] = raw.get("email")
    if r["source"] == "email":
        r["source_text"] = raw.get("text")
    return r


def save_items(db, rid, items):
    db.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (rid,))
    for i, it in enumerate(items):
        cid = it.get("category_id") or None
        if cid and not db.execute("SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone():
            cid = None
        db.execute(
            "INSERT INTO receipt_items(receipt_id, name, quantity, amount, taxable, category_id, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, (it.get("name") or "Item").strip(), it.get("quantity"), round(float(it.get("amount") or 0), 2),
             1 if it.get("taxable", True) else 0, cid, i),
        )


def source_of(db, rid):
    """The text/email details a receipt was read from (kept so it can be re-read)."""
    r = db.execute("SELECT raw_json FROM receipts WHERE id = ?", (rid,)).fetchone()
    raw = json.loads(r["raw_json"] or "{}") if r else {}
    return {"text": raw.get("text"), "email": raw.get("email")}


def save_parsed(db, rid, res, text=None, email=None):
    """Store a parsed receipt. The original text/email details are kept alongside the result."""
    raw = dict(res)
    if text:
        raw["text"] = text
    if email:
        raw["email"] = email
    purchase_date = res.get("purchase_date") or ((email or {}).get("date") or "")[:10] or None
    db.execute(
        """UPDATE receipts SET merchant = ?, purchase_date = ?, subtotal = ?, tax = ?, total = ?,
               status = 'parsed', error = NULL, raw_json = ? WHERE id = ?""",
        (res["merchant"], purchase_date, res["subtotal"], res["tax"], res["total"], json.dumps(raw), rid),
    )
    save_items(db, rid, res["items"])


def run_parse(db, rid, data=None, media_type=None, text=None, email=None):
    """Read a stored receipt with AI and save the result."""
    try:
        res = ai.parse_receipt(db, data=data, media_type=media_type, text=text, email=email)
    except ai.AIError as e:
        db.execute("UPDATE receipts SET status = 'error', error = ? WHERE id = ?", (str(e), rid))
        return False
    save_parsed(db, rid, res, text=text, email=email)
    return True


def candidates(db, rid, limit=8):
    """Card transactions that could be this receipt, best first."""
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r or r["total"] is None:
        return []
    total, rdate = float(r["total"]), _d(r["purchase_date"])
    rtok = _tokens(r["merchant"])
    q = """SELECT t.* FROM transactions t
           WHERE t.excluded = 0 AND t.source IN ('statement', 'manual') AND t.amount > 0
             AND t.amount BETWEEN ? AND ?
             AND t.id NOT IN (SELECT transaction_id FROM receipts WHERE transaction_id IS NOT NULL AND id != ?)"""
    args = [total * 0.75 - 0.02, total * 1.35 + 0.02, rid]
    if rdate:
        q += " AND t.date BETWEEN date(?, '-10 day') AND date(?, '+10 day')"
        args += [rdate.isoformat(), rdate.isoformat()]
    out = []
    for t in db.execute(q, args):
        t = dict(t)
        diff = abs(t["amount"] - total)
        score = 60 if diff <= 0.011 else 35 if diff <= total * 0.02 else 12
        td = _d(t["date"])
        if rdate and td:
            days = abs((td - rdate).days)
            score += 25 if days <= 1 else 18 if days <= 3 else 8 if days <= 7 else 0
        else:
            score += 8
        overlap = rtok & _tokens(t["description"])
        if overlap:
            score += 15
        elif rtok and r["merchant"]:
            score -= 10
        t["score"] = score
        out.append(t)
    out.sort(key=lambda t: (-t["score"], t["date"]))
    return out[:limit]


def best_match(db, rid):
    c = candidates(db, rid, limit=3)
    if not c:
        return None
    if c[0]["score"] >= 85 and (len(c) == 1 or c[1]["score"] <= c[0]["score"] - 15):
        return c[0]
    return None


def compute_splits(items, tax, target_total):
    """Spread tax over taxable items, then scale so splits sum exactly to target_total.

    Returns list of (category_id, amount, item_count)."""
    items = [i for i in items if abs(i["amount"]) > 0]
    if not items:
        return [(None, round(target_total, 2), 0)]
    taxable = [i for i in items if i.get("taxable")] or items
    taxable_ids = {id(i) for i in taxable}
    tax_base = sum(i["amount"] for i in taxable)
    raw = []
    for i in items:
        share = (i["amount"] / tax_base * (tax or 0)) if (id(i) in taxable_ids and tax_base) else 0
        raw.append((i.get("category_id"), i["amount"] + share))
    total_raw = sum(a for _, a in raw)
    factor = (target_total / total_raw) if total_raw else 1
    by_cat, counts = defaultdict(float), defaultdict(int)
    for cid, a in raw:
        by_cat[cid] += a * factor
        counts[cid] += 1
    result = [[cid, round(a, 2), counts[cid]] for cid, a in by_cat.items()]
    residual = round(target_total - sum(a for _, a, _ in result), 2)
    if result and residual:
        biggest = max(result, key=lambda x: abs(x[1]))
        biggest[1] = round(biggest[1] + residual, 2)
    return [tuple(x) for x in result if abs(x[1]) >= 0.005 or len(result) == 1]


def apply(db, rid, txn_id):
    """Link receipt to transaction and replace its splits with the receipt's category breakdown."""
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    t = db.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not r or not t:
        raise ValueError("Receipt or transaction not found")
    items = [dict(i) for i in db.execute("SELECT * FROM receipt_items WHERE receipt_id = ?", (rid,))]
    # Amazon exports already include tax in each line
    tax = 0 if r["source"] == "amazon" else (r["tax"] or 0)
    splits = compute_splits(items, tax, t["amount"])
    db.execute("UPDATE receipts SET transaction_id = NULL WHERE transaction_id = ? AND id != ?", (txn_id, rid))
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
    for cid, amt, n in splits:
        db.execute(
            "INSERT INTO splits(transaction_id, category_id, amount, memo) VALUES (?, ?, ?, ?)",
            (txn_id, cid, amt, f"Receipt: {n} item{'s' if n != 1 else ''}"),
        )
    db.execute("UPDATE transactions SET reviewed = 1 WHERE id = ?", (txn_id,))
    db.execute("UPDATE receipts SET transaction_id = ?, status = 'applied' WHERE id = ?", (txn_id, rid))
    return splits


def create_transaction_from(db, rid, account="Cash", budget_month=None):
    """For cash/debit purchases with no card line: make a transaction from the receipt."""
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    d = (r["purchase_date"] or date.today().isoformat())[:10]
    cur = db.execute(
        """INSERT INTO transactions(date, description, merchant, amount, account, source, budget_month, reviewed)
           VALUES (?, ?, ?, ?, ?, 'manual', ?, 1)""",
        (d, r["merchant"] or "Receipt", (r["merchant"] or "").upper(), r["total"], account, budget_month or d[:7]),
    )
    apply(db, rid, cur.lastrowid)
    return cur.lastrowid
