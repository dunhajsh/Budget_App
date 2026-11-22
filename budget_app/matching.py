"""Matching receipts to card charges, and splitting charges by the receipts linked to them.

Suggestions are only charges that match the receipt's exact amount or its store name - never just a
"similar" amount. Besides one receipt = one charge, two shapes are found automatically:

* several receipts paid in one charge - e.g. an Amazon order shipped in two boxes but charged once
  ($20.00 + $5.50 = one $25.50 charge), or two receipts from the same store on the same trip;
* one receipt charged in several parts - e.g. an order charged per shipment.

Links live in receipt_links (any receipt to any charges). A charge's category splits are rebuilt from
all its receipts: each receipt contributes its share (its whole total, or - if it was charged in parts -
the part on this charge), broken down by its items' categories with tax spread over taxable items.
"""
import re
from datetime import date, timedelta
from itertools import combinations

ALIASES = {"amzn": "amazon", "amazoncom": "amazon", "wm": "walmart", "wmt": "walmart", "wal": "walmart",
           "tgt": "target", "mjr": "meijer", "cvs": "cvs", "costco": "costco", "sams": "samsclub"}
NOISE = {"the", "inc", "com", "store", "stores", "llc", "co", "mktpl", "mktp", "mkt", "marketplace", "mark", "reta",
         "retail", "digital", "pos", "purchase", "debit", "card", "www", "online", "sq", "tst", "pp", "paypal",
         "supercenter", "super", "center", "market", "shop", "order", "payment", "and", "usa", "us"}
TOL = 0.005            # "exact" amount: to the cent


def store_words(s):
    """Distinctive words of a store / statement description ("AMAZON MKTPL*AB12CD34" -> {"amazon"})."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
        if any(ch.isdigit() for ch in w):
            continue
        w = ALIASES.get(w, w)
        if len(w) > 2 and w not in NOISE:
            out.add(w)
    return out


def same_store(a, b):
    return bool(store_words(a) & store_words(b))


def _d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _order(r):
    """Amazon order number from the receipt's external id (shipments of one order share it)."""
    m = re.match(r"amazon\|([^|]+)\|", r["external_id"] or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ links

def links_of(db, rid):
    return [r["transaction_id"] for r in db.execute(
        """SELECT l.transaction_id FROM receipt_links l JOIN transactions t ON t.id = l.transaction_id
           WHERE l.receipt_id = ? ORDER BY t.date, t.id""", (rid,))]


def receipts_of(db, tid):
    return [r["receipt_id"] for r in db.execute("SELECT receipt_id FROM receipt_links WHERE transaction_id = ? ORDER BY receipt_id", (tid,))]


def covered(db, tid, except_rid=None):
    """How much of a charge the receipts already applied to it account for."""
    total = 0.0
    for rid in receipts_of(db, tid):
        if rid != except_rid:
            total += _share(db, rid, tid)
    return round(total, 2)


def _receipt_total(db, r):
    if r["total"] is not None:
        return float(r["total"])
    s = db.execute("SELECT COALESCE(SUM(amount), 0) FROM receipt_items WHERE receipt_id = ?", (r["id"],)).fetchone()[0]
    return float(s) + (0 if r["source"] == "amazon" else float(r["tax"] or 0))


def _share(db, rid, tid):
    """The part of receipt rid paid in charge tid: all of it, or - if the receipt was charged in several
    parts - this charge's proportion of it."""
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    charges = db.execute("""SELECT t.id, t.amount FROM receipt_links l JOIN transactions t ON t.id = l.transaction_id
                            WHERE l.receipt_id = ?""", (rid,)).fetchall()
    total = _receipt_total(db, r)
    if len(charges) <= 1:
        return total
    whole = sum(c["amount"] for c in charges)
    mine = next((c["amount"] for c in charges if c["id"] == tid), 0)
    return total * mine / whole if whole else 0


def set_links(db, rid, txn_ids):
    """Apply receipt rid to exactly these charges (replacing what it was applied to before), then
    re-split every charge involved. A charge left with no receipts keeps its current categories."""
    from .receipts import rebuild_splits
    txn_ids = list(dict.fromkeys(int(t) for t in txn_ids))
    for tid in txn_ids:
        if not db.execute("SELECT 1 FROM transactions WHERE id = ?", (tid,)).fetchone():
            raise ValueError(f"Transaction {tid} not found")
    old = links_of(db, rid)
    db.execute("DELETE FROM receipt_links WHERE receipt_id = ?", (rid,))
    for tid in txn_ids:
        db.execute("INSERT INTO receipt_links(receipt_id, transaction_id) VALUES (?, ?)", (rid, tid))
    for tid in dict.fromkeys(old + txn_ids):
        if receipts_of(db, tid):
            rebuild_splits(db, tid)
    first = links_of(db, rid)[:1]
    if first:
        db.execute("UPDATE receipts SET transaction_id = ?, status = 'applied', error = NULL WHERE id = ?", (first[0], rid))
    else:
        db.execute("UPDATE receipts SET status = CASE WHEN status = 'applied' THEN 'reviewed' ELSE status END WHERE id = ?", (rid,))
    return txn_ids


# ------------------------------------------------------------------ suggestions

def _txn(t, **extra):
    d = {k: t[k] for k in ("id", "date", "description", "amount", "account", "budget_month", "reviewed")}
    d.update(extra)
    return d


def _date_points(days):
    return 15 if days <= 1 else 10 if days <= 3 else 5 if days <= 7 else 0


def candidates(db, rid, limit=10):
    """Ways this receipt could be paid, best first. Each option is
    {score, kind, why, transactions: [charge...], with_receipts: [other receipts paid in the same charge]}.
    Only charges matching the exact amount or the store name are suggested."""
    r = db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if not r:
        return []
    total = _receipt_total(db, r)
    rdate = _d(r["purchase_date"])
    store = r["merchant"] or ""
    if not total and not store:
        return []
    lo, hi = ((rdate - timedelta(days=21)).isoformat(), (rdate + timedelta(days=45)).isoformat()) if rdate else ("0000", "9999")
    pool = [dict(t) for t in db.execute(
        """SELECT * FROM transactions WHERE excluded = 0 AND source IN ('statement', 'manual') AND amount > 0
             AND date BETWEEN ? AND ?""", (lo, hi))]
    mine = set(links_of(db, rid))
    opts = []

    def days(t):
        td = _d(t["date"])
        return abs((td - rdate).days) if td and rdate else 99

    def taken(t):   # already fully explained by other receipts
        if t["id"] in mine:
            return 0
        return covered(db, t["id"], except_rid=rid)

    # 1. single charges: exact amount and/or same store
    for t in pool:
        exact = total and abs(t["amount"] - total) <= TOL
        named = same_store(store, t["description"]) or same_store(store, t["merchant"])
        if not exact and not (named and days(t) <= 14):
            continue
        score = (60 if exact else 0) + (30 if named else 0) + _date_points(days(t))
        cov = taken(t)
        if cov and cov >= t["amount"] - TOL:
            score -= 40
        why = ("Same amount and store" if exact and named else "Same amount" if exact else "Same store")
        opts.append({"score": score, "kind": "single", "why": why,
                     "transactions": [_txn(t, covered=cov, days=days(t))], "with_receipts": []})

    # 2. this receipt + other receipts from the same store = one charge (e.g. an order shipped in two boxes)
    if total:
        others = [dict(o) for o in db.execute(
            """SELECT * FROM receipts WHERE id != ? AND status IN ('parsed', 'reviewed', 'applied') AND total IS NOT NULL
                 AND purchase_date BETWEEN ? AND ?""",
            (rid, (rdate - timedelta(days=21)).isoformat() if rdate else "0000", (rdate + timedelta(days=21)).isoformat() if rdate else "9999"))]
        order = _order(r)
        # Amazon only charges shipments of the same order together; other stores: receipts from the same day or two
        others = [o for o in others if same_store(store, o["merchant"]) and not links_of(db, o["id"])
                  and (_order(o) == order if order else (rdate and _d(o["purchase_date"]) and abs((_d(o["purchase_date"]) - rdate).days) <= 2))][:30]
        bigger = [t for t in pool if t["amount"] > total + TOL and (same_store(store, t["description"]) or same_store(store, t["merchant"]))]
        for t in bigger:
            if taken(t):
                continue
            for n in (1, 2, 3):
                for combo in combinations(others, n):
                    if abs(total + sum(o["total"] for o in combo) - t["amount"]) <= TOL:
                        same_order = order and all(_order(o) == order for o in combo)
                        opts.append({"score": 92 + (6 if same_order else 0) + _date_points(days(t)) // 3, "kind": "together",
                                     "why": "Same Amazon order, charged together" if same_order else "Receipts add up to this charge",
                                     "transactions": [_txn(t, covered=0, days=days(t))],
                                     "with_receipts": [{"id": o["id"], "merchant": o["merchant"], "purchase_date": o["purchase_date"],
                                                        "total": o["total"], "status": o["status"]} for o in combo]})
                if len(others) > 12 and n == 2:
                    break

        # 3. this receipt charged in several parts (same store, adding up to the receipt)
        parts = [t for t in pool if t["amount"] < total - TOL and (same_store(store, t["description"]) or same_store(store, t["merchant"]))
                 and days(t) <= 14 and not taken(t)][:25]
        for n in (2, 3):
            for combo in combinations(parts, n):
                spread = max(_d(t["date"]) for t in combo) - min(_d(t["date"]) for t in combo)
                if spread.days <= 10 and abs(sum(t["amount"] for t in combo) - total) <= TOL:
                    opts.append({"score": 90 + max(_date_points(days(t)) for t in combo) // 3, "kind": "parts",
                                 "why": f"Charged in {n} parts", "transactions": [_txn(t, covered=0, days=days(t)) for t in combo],
                                 "with_receipts": []})

    # what it's applied to now always shows
    shown = {tuple(sorted(x["id"] for x in o["transactions"])) for o in opts}
    if mine and tuple(sorted(mine)) not in shown:
        ts = [dict(t) for t in db.execute(f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(mine))})", list(mine))]
        opts.append({"score": 200, "kind": "single" if len(ts) == 1 else "parts", "why": "Applied now",
                     "transactions": [_txn(t, covered=0, days=days(t)) for t in ts], "with_receipts": []})
    for o in opts:
        o["current"] = bool(mine) and {x["id"] for x in o["transactions"]} == mine
        if o["current"]:
            o["score"] = max(o["score"], 200)
    opts.sort(key=lambda o: (-o["score"], min(x["days"] for x in o["transactions"])))
    # with an exact match in hand, only a couple of "same store" fallbacks (Amazon has dozens of charges)
    if any(o["why"] != "Same store" for o in opts):
        keep, n = [], 0
        for o in opts:
            if o["why"] == "Same store":
                n += 1
                if n > 2:
                    continue
            keep.append(o)
        opts = keep
    return opts[:limit]


def best(db, rid):
    """A suggestion clear enough to pre-select: exact amount (plus same store, or a unique exact amount
    within a few days), or receipts that add up exactly to a same-store charge."""
    opts = [o for o in candidates(db, rid, limit=5) if not o.get("current")]
    if not opts:
        return None
    top = opts[0]
    runner = opts[1]["score"] if len(opts) > 1 else 0
    if top["score"] >= 90 and top["score"] - runner >= 10:
        return top
    return None


def search(db, q, rid=None, limit=40):
    """Find any charge by text or amount (for when the suggestion isn't there)."""
    q = (q or "").strip()
    where, args = ["excluded = 0", "source IN ('statement', 'manual')"], []
    num = re.sub(r"[$,\s]", "", q)
    if re.fullmatch(r"-?\d+(\.\d+)?", num):
        where.append("ABS(amount - ?) < 0.015")
        args.append(float(num))
    elif q:
        where.append("(description LIKE ? OR merchant LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    rdate = None
    if rid:
        r = db.execute("SELECT purchase_date FROM receipts WHERE id = ?", (rid,)).fetchone()
        rdate = r and r["purchase_date"]
    order = "ABS(julianday(date) - julianday(?))" if rdate else "date DESC"
    if rdate:
        args.append(rdate)
    out = []
    for t in db.execute(f"SELECT * FROM transactions WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {int(limit)}", args):
        out.append(_txn(t, covered=covered(db, t["id"], except_rid=rid)))
    return out
