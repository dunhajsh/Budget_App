"""Spending broken down by envelope and subcategory, and what makes up each subcategory.

Amounts here are the same numbers the budget uses: split lines of transactions that count
(excluded = 0), in their budget month. Spending filed on an envelope itself (not on one of its
subcategories, e.g. spreadsheet months that were only tracked per envelope) is reported as the
envelope's "not broken down" part, so subcategories always add up to the envelope.
"""
from collections import defaultdict

from . import itemmemory
from .budget import categories_tree, month_range

RECEIPT_OK = ("parsed", "reviewed", "applied")   # read receipts (not pending, failed or duplicates)


def overview(db, start, end):
    months = month_range(start, end)
    idx = {m: i for i, m in enumerate(months)}
    tree = categories_tree(db, include_archived=True)
    spent = defaultdict(lambda: [0.0] * len(months))
    alltime = defaultdict(float)
    for r in db.execute(
        """SELECT s.category_id cid, t.budget_month m, SUM(s.amount) a FROM splits s
           JOIN transactions t ON t.id = s.transaction_id
           WHERE t.excluded = 0 AND s.category_id IS NOT NULL GROUP BY 1, 2"""):
        alltime[r["cid"]] += r["a"]
        if r["m"] in idx:
            spent[r["cid"]][idx[r["m"]]] += r["a"]
    alloc = defaultdict(lambda: [0.0] * len(months))
    env = {c["id"]: p["id"] for p in tree for c in [p, *p["children"]]}
    for r in db.execute("SELECT category_id cid, month m, amount a FROM allocations WHERE month BETWEEN ? AND ?", (start, end)):
        alloc[env.get(r["cid"], r["cid"])][idx[r["m"]]] += r["a"]
    used = set(spent) | set(alloc)
    out = []
    for p in tree:
        kids = [c for c in p["children"] if not c["archived"] or c["id"] in used]
        if p["archived"] and p["id"] not in used and not any(c["id"] in used for c in kids):
            continue
        out.append({
            "id": p["id"], "name": p["name"], "grp": p["grp"], "archived": p["archived"],
            "own": [round(v, 2) for v in spent.get(p["id"], [0.0] * len(months))],
            "allocated": [round(v, 2) for v in alloc.get(p["id"], [0.0] * len(months))],
            "alltime_own": round(alltime.get(p["id"], 0), 2),
            "children": [{"id": c["id"], "name": c["name"], "archived": c["archived"],
                          "spent": [round(v, 2) for v in spent.get(c["id"], [0.0] * len(months))],
                          "alltime": round(alltime.get(c["id"], 0), 2)} for c in kids],
        })
    return {"months": months, "envelopes": out}


def detail(db, cid, start, end, only_own=False, only_ids=None):
    """Transactions, merchants and receipt items behind a subcategory (or a whole envelope) in a period.
    only_own: an envelope's spending that isn't on any of its subcategories.
    only_ids: just these subcategories of the envelope (e.g. the small ones grouped as "Other")."""
    cat = db.execute("SELECT id, name, parent_id FROM categories WHERE id = ?", (cid,)).fetchone()
    if not cat:
        raise ValueError("Category not found")
    ids = [cid]
    if cat["parent_id"] is None and not only_own:
        kids = [r["id"] for r in db.execute("SELECT id FROM categories WHERE parent_id = ?", (cid,))]
        ids = [i for i in kids if i in set(only_ids)] if only_ids else ids + kids
        if not ids:
            raise ValueError("No such subcategories")
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM categories")}
    marks = ",".join("?" * len(ids))

    txns = []
    for r in db.execute(
        f"""SELECT t.id, t.date, t.description, t.merchant, t.source, t.budget_month, t.account, t.amount AS txn_amount,
                   s.amount, s.category_id, s.memo,
                   (SELECT l.receipt_id FROM receipt_links l WHERE l.transaction_id = t.id ORDER BY l.receipt_id LIMIT 1) AS receipt_id
            FROM splits s JOIN transactions t ON t.id = s.transaction_id
            WHERE t.excluded = 0 AND s.category_id IN ({marks}) AND t.budget_month BETWEEN ? AND ?
            ORDER BY t.date DESC, t.id DESC""", (*ids, start, end)):
        d = dict(r)
        d["category"] = names.get(d["category_id"])
        d["who"] = _merchant(d)
        txns.append(d)

    merchants = defaultdict(lambda: {"total": 0.0, "n": 0, "by_month": defaultdict(float)})
    for t in txns:
        m = merchants[t["who"]]
        m["total"] += t["amount"]
        m["n"] += 1
        m["by_month"][t["budget_month"]] += t["amount"]

    items = defaultdict(lambda: {"names": defaultdict(int), "buys": []})
    for r in db.execute(
        f"""SELECT ri.name, ri.amount, ri.quantity, ri.category_id, ri.cat_source, r.id AS receipt_id, r.merchant,
                   r.purchase_date, r.status, t.budget_month
            FROM receipt_items ri JOIN receipts r ON r.id = ri.receipt_id
            LEFT JOIN transactions t ON t.id = r.transaction_id
            WHERE ri.category_id IN ({marks}) AND r.status IN ({",".join("?" * len(RECEIPT_OK))})
              AND COALESCE(t.budget_month, substr(r.purchase_date, 1, 7)) BETWEEN ? AND ?""",
            (*ids, *RECEIPT_OK, start, end)):
        k = itemmemory.key(r["name"]) or r["name"].lower()
        g = items[k]
        g["names"][r["name"]] += 1
        g["buys"].append({"date": r["purchase_date"], "amount": r["amount"], "quantity": r["quantity"],
                          "store": r["merchant"], "receipt_id": r["receipt_id"], "category": names.get(r["category_id"]),
                          "month": r["budget_month"] or (r["purchase_date"] or "")[:7],
                          "checked": r["status"] != "parsed"})
    item_rows = []
    for k, g in items.items():
        buys = sorted(g["buys"], key=lambda b: b["date"] or "")
        amounts = [b["amount"] for b in buys]
        by_month = defaultdict(float)
        for b in buys:
            by_month[b["month"]] += b["amount"]
        item_rows.append({
            "key": k, "name": max(g["names"], key=g["names"].get), "n": len(buys),
            "total": round(sum(amounts), 2), "avg": round(sum(amounts) / len(amounts), 2),
            "low": min(amounts), "high": max(amounts), "first": amounts[0], "last": amounts[-1],
            "last_date": buys[-1]["date"], "stores": sorted({b["store"] for b in buys if b["store"]}),
            "categories": sorted({b["category"] for b in buys if b["category"]}),
            "unchecked": sum(1 for b in buys if not b["checked"]),
            "by_month": {m: round(v, 2) for m, v in by_month.items()}, "buys": buys,
        })
    item_rows.sort(key=lambda x: -x["total"])

    return {
        "category": {"id": cat["id"], "name": cat["name"], "parent_id": cat["parent_id"],
                     "parent": names.get(cat["parent_id"]), "only_own": bool(only_own and cat["parent_id"] is None)},
        "start": start, "end": end,
        "total": round(sum(t["amount"] for t in txns), 2),
        "transactions": txns,
        "merchants": sorted(({"name": k, "total": round(v["total"], 2), "n": v["n"],
                              "by_month": {m: round(a, 2) for m, a in v["by_month"].items()}}
                             for k, v in merchants.items()), key=lambda x: -x["total"]),
        "items": item_rows,
        "item_total": round(sum(i["total"] for i in item_rows), 2),
    }


def _merchant(t):
    if t["source"] == "excel":
        return (t["description"] or "Spreadsheet entry").strip()
    m = (t["merchant"] or "").strip()
    return m.title() if m and m.isupper() else (m or t["description"] or "?")
