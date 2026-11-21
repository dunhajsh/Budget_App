"""Envelope math.

For every top-level category ("envelope") and month M:
    carry_in(M)  = sum over earlier months of (allocated - spent) + adjustments up to M
    available(M) = carry_in(M) + allocated(M) - spent(M)

This is the same thing the spreadsheet did with
"Previous Year + Total - Spent = Remainder", just tracked month by month.
Spending in a subcategory counts against its parent envelope.
"""
from collections import defaultdict


def month_add(month, n):
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_range(start, end):
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = month_add(cur, 1)
    return out


def categories_tree(db, include_archived=False):
    q = "SELECT * FROM categories"
    if not include_archived:
        q += " WHERE archived = 0"
    q += " ORDER BY sort_order, name"
    cats = [dict(r) for r in db.execute(q)]
    parents = [c for c in cats if c["parent_id"] is None]
    by_parent = defaultdict(list)
    for c in cats:
        if c["parent_id"] is not None:
            by_parent[c["parent_id"]].append(c)
    for p in parents:
        p["children"] = by_parent.get(p["id"], [])
    return parents


def envelope_of(db):
    """Map every category id to its top-level envelope id."""
    m = {}
    for r in db.execute("SELECT id, parent_id FROM categories"):
        m[r["id"]] = r["parent_id"] if r["parent_id"] is not None else r["id"]
    return m


def _monthly(db, through_month):
    """Return dicts keyed by (envelope_id, month) for spent, allocated, adjusted."""
    env = envelope_of(db)
    spent, alloc, adj = defaultdict(float), defaultdict(float), defaultdict(float)
    for r in db.execute(
        """SELECT s.category_id, t.budget_month AS month, SUM(s.amount) AS amt
           FROM splits s JOIN transactions t ON t.id = s.transaction_id
           WHERE t.excluded = 0 AND s.category_id IS NOT NULL AND t.budget_month <= ?
           GROUP BY s.category_id, t.budget_month""",
        (through_month,),
    ):
        spent[(env.get(r["category_id"], r["category_id"]), r["month"])] += r["amt"]
    for r in db.execute(
        "SELECT category_id, month, amount FROM allocations WHERE month <= ?", (through_month,)
    ):
        alloc[(env.get(r["category_id"], r["category_id"]), r["month"])] += r["amount"]
    for r in db.execute(
        "SELECT category_id, month, SUM(amount) AS amt FROM adjustments WHERE month <= ? "
        "GROUP BY category_id, month",
        (through_month,),
    ):
        adj[(env.get(r["category_id"], r["category_id"]), r["month"])] += r["amt"]
    return spent, alloc, adj


def month_summary(db, month):
    """Everything the dashboard / budget screens need for one month."""
    spent, alloc, adj = _monthly(db, month)
    # Adjustments are carry-over corrections (opening balances etc.), so the current month's count as carried in.
    carry = defaultdict(float)
    for (cid, m), v in alloc.items():
        if m < month:
            carry[cid] += v
    for (cid, m), v in adj.items():
        if m <= month:
            carry[cid] += v
    for (cid, m), v in spent.items():
        if m < month:
            carry[cid] -= v

    # Subcategory spending this month (for breakdowns)
    sub_spent = defaultdict(float)
    for r in db.execute(
        """SELECT s.category_id, SUM(s.amount) AS amt
           FROM splits s JOIN transactions t ON t.id = s.transaction_id
           WHERE t.excluded = 0 AND t.budget_month = ? AND s.category_id IS NOT NULL
           GROUP BY s.category_id""",
        (month,),
    ):
        sub_spent[r["category_id"]] = r["amt"]

    envelopes = []
    active_ids = set()
    for p in categories_tree(db, include_archived=True):
        cid = p["id"]
        row = {
            "id": cid,
            "name": p["name"],
            "group": p["grp"],
            "default_amount": p["default_amount"],
            "archived": bool(p["archived"]),
            "carry_in": round(carry[cid], 2),
            "allocated": round(alloc[(cid, month)], 2),
            "spent": round(spent[(cid, month)], 2),
            "children": [
                {"id": c["id"], "name": c["name"], "spent": round(sub_spent.get(c["id"], 0), 2)}
                for c in p["children"]
            ],
            "direct_spent": round(sub_spent.get(cid, 0), 2),
        }
        row["available"] = round(row["carry_in"] + row["allocated"] - row["spent"], 2)
        # Hide archived envelopes that are empty and untouched
        if row["archived"] and abs(row["available"]) < 0.005 and not row["spent"] and not row["allocated"]:
            continue
        active_ids.add(cid)
        envelopes.append(row)

    income = db.execute("SELECT COALESCE(SUM(amount),0) AS v FROM income WHERE month = ?", (month,)).fetchone()["v"]
    uncategorized = db.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(t.amount - COALESCE(x.amt,0)),0) AS amt
           FROM transactions t
           LEFT JOIN (SELECT transaction_id, SUM(amount) amt FROM splits
                      WHERE category_id IS NOT NULL GROUP BY transaction_id) x
             ON x.transaction_id = t.id
           WHERE t.excluded = 0 AND t.budget_month = ?
             AND ABS(t.amount - COALESCE(x.amt,0)) > 0.005""",
        (month,),
    ).fetchone()
    to_review = db.execute(
        "SELECT COUNT(*) AS n FROM transactions t WHERE excluded = 0 AND reviewed = 0 AND budget_month = ? "
        "AND source != 'excel' AND EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)",
        (month,),
    ).fetchone()["n"]

    tot_alloc = sum(e["allocated"] for e in envelopes)
    tot_spent = sum(e["spent"] for e in envelopes)
    return {
        "month": month,
        "income": round(income, 2),
        "allocated": round(tot_alloc, 2),
        "spent": round(tot_spent, 2),
        "left_to_allocate": round(income - tot_alloc, 2),
        "total_available": round(sum(e["available"] for e in envelopes), 2),
        "uncategorized_count": uncategorized["n"],
        "uncategorized_amount": round(uncategorized["amt"], 2),
        "to_review": to_review,
        "envelopes": envelopes,
    }


def history(db, start, end):
    """Per-month totals and per-envelope spending between start and end (inclusive)."""
    months = month_range(start, end)
    spent, alloc, _ = _monthly(db, end)
    names = {c["id"]: c["name"] for c in categories_tree(db, include_archived=True)}
    income = defaultdict(float)
    for r in db.execute(
        "SELECT month, SUM(amount) AS v FROM income WHERE month BETWEEN ? AND ? GROUP BY month", (start, end)
    ):
        income[r["month"]] = r["v"]
    by_cat = defaultdict(lambda: [0.0] * len(months))
    alloc_by_cat = defaultdict(lambda: [0.0] * len(months))
    idx = {m: i for i, m in enumerate(months)}
    for (cid, m), v in spent.items():
        if m in idx:
            by_cat[cid][idx[m]] += v
    for (cid, m), v in alloc.items():
        if m in idx:
            alloc_by_cat[cid][idx[m]] += v
    return {
        "months": months,
        "income": [round(income[m], 2) for m in months],
        "spent": [round(sum(by_cat[c][i] for c in by_cat), 2) for i in range(len(months))],
        "allocated": [round(sum(alloc_by_cat[c][i] for c in alloc_by_cat), 2) for i in range(len(months))],
        "categories": [
            {
                "id": cid,
                "name": names.get(cid, f"#{cid}"),
                "spent": [round(v, 2) for v in by_cat[cid]],
                "allocated": [round(v, 2) for v in alloc_by_cat[cid]],
            }
            for cid in names
            if cid in by_cat or cid in alloc_by_cat
        ],
    }
