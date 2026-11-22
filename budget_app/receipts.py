"""Receipts: store, parse, match to a card transaction, and split it by category."""
import json
import re
from collections import defaultdict
from datetime import date

from . import ai, itemmemory


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
    # the charges it's applied to, and any other receipts sharing them
    r["links"] = []
    for t in db.execute("""SELECT t.* FROM receipt_links l JOIN transactions t ON t.id = l.transaction_id
                           WHERE l.receipt_id = ? ORDER BY t.date, t.id""", (rid,)):
        t = dict(t)
        t["other_receipts"] = [dict(o) for o in db.execute(
            """SELECT r.id, r.merchant, r.purchase_date, r.total FROM receipt_links l JOIN receipts r ON r.id = l.receipt_id
               WHERE l.transaction_id = ? AND r.id != ?""", (t["id"], rid))]
        r["links"].append(t)
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
            "INSERT INTO receipt_items(receipt_id, name, quantity, amount, taxable, category_id, sort_order, cat_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, (it.get("name") or "Item").strip(), it.get("quantity"), round(float(it.get("amount") or 0), 2),
             1 if it.get("taxable", True) else 0, cid, i, it.get("cat_source") if cid else None),
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
    itemmemory.apply(db, res["items"])   # items you've filed before go where you put them
    db.execute(
        """UPDATE receipts SET merchant = ?, purchase_date = ?, subtotal = ?, tax = ?, total = ?,
               status = 'parsed', error = NULL, raw_json = ? WHERE id = ?""",
        (res["merchant"], purchase_date, res["subtotal"], res["tax"], res["total"], json.dumps(raw), rid),
    )
    save_items(db, rid, res["items"])


def categorize_items(db, items, store=""):
    """Fill in category_id for items read without AI: items you've filed before first (item memory),
    then one AI call for the rest. Returns an error message if the AI couldn't be reached."""
    itemmemory.apply(db, items)
    todo = [i for i in items if not i.get("category_id") and not i.get("coupon")]
    err = None
    if todo and ai.enabled(db):
        try:
            res = ai.categorize(db, [f"{i['name']} ({store}{', ' + i['department'].lower() + ' aisle' if i.get('department') else ''})"
                                     for i in todo], kind=f"{store} receipt items".strip())
            for idx, cid in res.items():
                if 0 <= idx < len(todo):
                    todo[idx]["category_id"] = cid
                    todo[idx]["cat_source"] = "ai"
        except ai.AIError as e:
            err = str(e)
    file_coupons(db, items)
    return err


def file_coupons(db, items):
    """A coupon on the whole order (not filed before) goes to the envelope most of the receipt was spent in."""
    coupons = [i for i in items if i.get("coupon") and not i.get("category_id")]
    if not coupons:
        return
    env = {r["id"]: r["parent_id"] or r["id"] for r in db.execute("SELECT id, parent_id FROM categories")}
    spent = {}
    for i in items:
        if i.get("category_id") and not i.get("coupon"):
            e = env.get(i["category_id"], i["category_id"])
            spent[e] = spent.get(e, 0) + (i.get("amount") or 0)
    if spent:
        top = max(spent, key=spent.get)
        for c in coupons:
            c["category_id"], c["cat_source"] = top, "ai"


def run_parse(db, rid, data=None, media_type=None, text=None, email=None):
    """Read a stored receipt and save the result. Digital receipts in a known layout (e.g. Meijer PDFs)
    are read exactly without AI; everything else goes to the AI."""
    from .importers import receipt_text
    plain = text or (receipt_text.pdf_text(data) if data is not None and media_type == "application/pdf" else "")
    res = receipt_text.parse(plain) if plain and not email else None
    if res:
        err = categorize_items(db, res["items"], res["merchant"])
        save_parsed(db, rid, res, text=text, email=email)
        if err:
            db.execute("UPDATE receipts SET error = ? WHERE id = ?",
                       (f"Read the receipt, but couldn't categorize the items: {err}", rid))
        return True
    if not ai.enabled(db):
        db.execute("UPDATE receipts SET status = 'parsed', error = ? WHERE id = ?",
                   ("Turn on AI in Settings for automatic reading - or enter the items by hand.", rid))
        return False
    try:
        res = ai.parse_receipt(db, data=data, media_type=media_type, text=text, email=email)
    except ai.AIError as e:
        db.execute("UPDATE receipts SET status = 'error', error = ? WHERE id = ?", (str(e), rid))
        return False
    save_parsed(db, rid, res, text=text, email=email)
    return True


def duplicate_of(db, rid):
    """Another receipt for the same purchase (same store, date and total), e.g. uploaded twice."""
    r = db.execute("SELECT merchant, purchase_date, total FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r or r["total"] is None or not r["purchase_date"]:
        return None
    store = (r["merchant"] or "").split()[:1]  # "MEIJER STORE" and "Meijer" are the same store
    for d in db.execute("""SELECT id, merchant FROM receipts WHERE id != ? AND status != 'duplicate' AND purchase_date = ?
                           AND ABS(total - ?) < 0.005 ORDER BY id""", (rid, r["purchase_date"], r["total"])):
        if [w.lower() for w in (d["merchant"] or "").split()[:1]] == [w.lower() for w in store]:
            return d["id"]
    return None


def process(db, rid, receipt_dir):
    """Read one stored receipt, check for duplicates, and match it to a card charge."""
    import mimetypes
    import os
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r:
        return
    src = source_of(db, rid)
    data = mt = None
    if r["stored_path"]:
        path = os.path.join(receipt_dir, os.path.basename(r["stored_path"]))
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            mt = "application/pdf" if path.lower().endswith(".pdf") else mimetypes.guess_type(path)[0]
    if data is None and not src["text"]:
        db.execute("UPDATE receipts SET status = 'error', error = 'The receipt file is missing.' WHERE id = ?", (rid,))
        return
    ok = run_parse(db, rid, data=data, media_type=mt, text=src["text"], email=src["email"])
    if not ok:
        return
    dup = duplicate_of(db, rid)
    if dup:
        db.execute("UPDATE receipts SET status = 'duplicate', error = ?, transaction_id = NULL WHERE id = ?",
                   (f"Same store, date and total as receipt #{dup} - probably uploaded twice. Delete this one.", rid))
        return
    if not r["transaction_id"]:
        m = best_match(db, rid)
        if m:
            db.execute("UPDATE receipts SET transaction_id = ? WHERE id = ?", (m["id"], rid))


class Worker:
    """Reads uploaded receipts one at a time in the background, so uploads return immediately and
    nothing is lost if the page is closed. Anything left 'pending' is picked up again on startup."""

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.thread = None
        self.current = None

    def kick(self, db_path, receipt_dir):
        import threading
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._run, args=(db_path, receipt_dir), daemon=True)
            self.thread.start()

    def _run(self, db_path, receipt_dir):
        from .db import connect
        db = connect(db_path)
        try:
            while True:
                r = db.execute("SELECT id FROM receipts WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
                if not r:
                    break
                self.current = r["id"]
                try:
                    process(db, r["id"], receipt_dir)
                except Exception as e:  # never leave it pending forever
                    db.execute("UPDATE receipts SET status = 'error', error = ? WHERE id = ?",
                               (f"{type(e).__name__}: {e}", r["id"]))
                db.commit()
        finally:
            self.current = None
            db.close()


WORKER = Worker()


def candidates(db, rid, limit=10):
    """Ways this receipt could be paid (see matching.candidates)."""
    from . import matching
    return matching.candidates(db, rid, limit)


def best_match(db, rid):
    """The charge to suggest for a receipt (the first one, if it's paid in parts), or None."""
    from . import matching
    o = matching.best(db, rid)
    return o["transactions"][0] if o else None


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


def apply(db, rid, txn_ids):
    """Apply a receipt to one charge or several (it was charged in parts), replacing what it was applied
    to before. Other receipts already applied to the same charge stay on it (several receipts, one
    charge). Each charge involved is re-split by category. Returns the splits of the first charge."""
    from . import matching
    if not db.execute("SELECT 1 FROM receipts WHERE id = ?", (rid,)).fetchone():
        raise ValueError("Receipt not found")
    ids = [txn_ids] if isinstance(txn_ids, int) else list(txn_ids)
    if not ids:
        raise ValueError("Pick the card charge(s) this receipt belongs to")
    matching.set_links(db, rid, ids)
    return [(s["category_id"], s["amount"]) for s in db.execute("SELECT * FROM splits WHERE transaction_id = ?", (ids[0],))]


def unapply(db, rid):
    """Take a receipt off its charge(s). Charges still holding other receipts are re-split; a charge left
    with none keeps its current categories."""
    from . import matching
    matching.set_links(db, rid, [])


def rebuild_splits(db, tid):
    """Split a charge by category from every receipt applied to it.

    Each receipt contributes its share of the charge (see matching._share), broken down by its items
    (tax spread over taxable items). If the receipts explain the charge to within 10 cents / 1% the
    difference is spread proportionally; if they explain clearly less, the rest stays uncategorized
    ("Not on the linked receipts") so you can see a receipt is missing; if more, they're scaled down."""
    from . import matching
    t = db.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    rids = matching.receipts_of(db, tid)
    if not t or not rids:
        return []
    amount = float(t["amount"])
    single = len(rids) == 1 and len(matching.links_of(db, rids[0])) == 1
    raw, counts, explained = defaultdict(float), defaultdict(int), 0.0
    for rid in rids:
        r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
        items = [dict(i) for i in db.execute("SELECT * FROM receipt_items WHERE receipt_id = ?", (rid,))]
        share = amount if single else matching._share(db, rid, tid)   # one receipt, one charge: fit it to the charge
        tax = 0 if r["source"] == "amazon" else (r["tax"] or 0)       # Amazon lines already include tax
        for cid, amt, n in compute_splits(items, tax, share):
            raw[cid] += amt
            counts[cid] += n
        explained += share
    rest = round(amount - explained, 2)
    if rest > max(0.10, amount * 0.01):
        raw[("rest",)] += rest
    else:
        factor = amount / explained if explained else 1
        for k in raw:
            raw[k] *= factor
    result = [[k, round(v, 2)] for k, v in raw.items()]
    residual = round(amount - sum(v for _, v in result), 2)
    if result and residual:
        max(result, key=lambda x: abs(x[1]))[1] = round(max(result, key=lambda x: abs(x[1]))[1] + residual, 2)
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (tid,))
    label = f"{len(rids)} receipts" if len(rids) > 1 else "Receipt"
    for k, v in result:
        if abs(v) < 0.005 and len(result) > 1:
            continue
        if k == ("rest",):
            db.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) VALUES (?, NULL, ?, ?)",
                       (tid, v, "Not on the linked receipts"))
        else:
            n = counts[k]
            db.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) VALUES (?, ?, ?, ?)",
                       (tid, k, v, f"{label}: {n} item{'s' if n != 1 else ''}"))
    db.execute("UPDATE transactions SET reviewed = 1 WHERE id = ?", (tid,))
    return result


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
