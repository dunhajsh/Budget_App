"""Import credit card / bank statement CSVs.

Works with SoFi's export out of the box and tries to auto-detect the columns of
other banks (Chase, Capital One, Discover, Amex, Citi...).
"""
import csv
import hashlib
import io
import re
from collections import Counter
from datetime import datetime

from ..categorize import find_category, load_rules, merchant_key

DATE_COLS = ["transaction date", "trans. date", "trans date", "date", "posted date", "post date"]
POST_COLS = ["post date", "posted date", "posting date"]
DESC_COLS = ["description", "merchant", "payee", "name", "details", "memo"]
AMOUNT_COLS = ["amount", "transaction amount"]
DEBIT_COLS = ["debit", "withdrawal", "charges"]
CREDIT_COLS = ["credit", "deposit", "payments"]
CAT_COLS = ["category"]
TYPE_COLS = ["type", "transaction type"]

PAYMENT_PAT = re.compile(
    r"AUTO-?PAY|PAYMENT\s*(-\s*)?THANK|THANK YOU.*PAYMENT|ONLINE PAYMENT|AUTOMATIC PAYMENT|MOBILE PAYMENT", re.I
)


def _find(headers, options):
    low = {h.lower().strip(): h for h in headers}
    for o in options:
        if o in low:
            return low[o]
    return None


def _date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {s!r}")


def _money(s):
    s = (s or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


def parse_csv(text):
    """Return a list of normalized transaction dicts (amount > 0 = spend)."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    c_date = _find(headers, DATE_COLS)
    c_post = _find(headers, POST_COLS)
    c_desc = _find(headers, DESC_COLS)
    c_amt = _find(headers, AMOUNT_COLS)
    c_deb = _find(headers, DEBIT_COLS)
    c_cred = _find(headers, CREDIT_COLS)
    c_cat = _find(headers, CAT_COLS)
    c_type = _find(headers, TYPE_COLS)
    if not c_date or not c_desc or not (c_amt or c_deb):
        raise ValueError(
            "Couldn't find date / description / amount columns. Found: " + ", ".join(headers)
        )

    out = []
    for row in reader:
        if not (row.get(c_date) or "").strip():
            continue
        if c_amt:
            amt = _money(row.get(c_amt))
        else:
            deb, cred = _money(row.get(c_deb)) or 0.0, _money(row.get(c_cred)) or 0.0
            amt = deb - cred
        if amt is None:
            continue
        out.append(
            {
                "date": _date(row[c_date]),
                "post_date": _date(row[c_post]) if c_post and row.get(c_post) and c_post != c_date else None,
                "description": row[c_desc].strip(),
                "amount": amt,
                "statement_category": (row.get(c_cat) or "").strip() or None if c_cat else None,
                "txn_type": (row.get(c_type) or "").strip() or None if c_type else None,
            }
        )

    # Some banks export purchases as negative numbers. Spending should be positive here.
    if c_amt and out:
        sales = [t for t in out if (t["txn_type"] or "").lower() in ("sale", "purchase", "debit")]
        sample = sales or [t for t in out if not PAYMENT_PAT.search(t["description"])]
        if sample and sum(1 for t in sample if t["amount"] < 0) > len(sample) / 2:
            for t in out:
                t["amount"] = -t["amount"]

    for t in out:
        t["excluded"] = bool(
            (t["txn_type"] or "").lower() == "payment"
            or (t["statement_category"] or "").lower() == "payment"
            or PAYMENT_PAT.search(t["description"])
        )
        t["amount"] = round(t["amount"], 2)
    return out


def guess_budget_month(txns, filename=""):
    """Statement month: prefer the dates in the filename, else the most common month."""
    dates = re.findall(r"(\d{1,2})[_\-](\d{1,2})[_\-](\d{4})", filename or "")
    if len(dates) >= 2:
        m, _, y = dates[-1]
        return f"{int(y):04d}-{int(m):02d}"
    c = Counter(t["date"][:7] for t in txns)
    return c.most_common(1)[0][0] if c else datetime.now().strftime("%Y-%m")


def import_statement(db, text, filename, account, budget_month=None):
    txns = parse_csv(text)
    if not txns:
        return {"imported": 0, "duplicates": 0, "auto_categorized": 0, "budget_month": budget_month}
    budget_month = budget_month or guess_budget_month(txns, filename)
    cur = db.execute(
        "INSERT INTO import_batches(kind, filename, budget_month, row_count) VALUES ('statement', ?, ?, ?)",
        (filename, budget_month, len(txns)),
    )
    batch_id = cur.lastrowid
    rules = load_rules(db)
    stmt_map = {r["statement_category"]: r["category_id"] for r in db.execute("SELECT * FROM statement_category_map")}

    seen = Counter()
    imported = dupes = auto = 0
    for t in txns:
        key = f"{account}|{t['date']}|{t['description']}|{t['amount']:.2f}"
        seen[key] += 1
        h = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
        if db.execute("SELECT 1 FROM transactions WHERE import_hash = ?", (h,)).fetchone():
            dupes += 1
            continue
        cur = db.execute(
            """INSERT INTO transactions(date, post_date, description, merchant, amount, account, source,
                   budget_month, statement_category, txn_type, excluded, reviewed, import_hash, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, 'statement', ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["date"], t["post_date"], t["description"], merchant_key(t["description"]), t["amount"],
                account, budget_month, t["statement_category"], t["txn_type"], int(t["excluded"]),
                int(t["excluded"]), h, batch_id,
            ),
        )
        imported += 1
        if not t["excluded"]:
            cid, how = find_category(db, t["description"], t["statement_category"], rules=rules, stmt_map=stmt_map)
            if cid:
                db.execute(
                    "INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)",
                    (cur.lastrowid, cid, t["amount"]),
                )
                if how.startswith("rule:"):
                    db.execute("UPDATE rules SET hits = hits + 1 WHERE id = ?", (int(how[5:]),))
                auto += 1
    db.execute("UPDATE import_batches SET row_count = ? WHERE id = ?", (imported, batch_id))
    return {
        "batch_id": batch_id,
        "imported": imported,
        "duplicates": dupes,
        "auto_categorized": auto,
        "budget_month": budget_month,
        "statement_categories": sorted({t["statement_category"] for t in txns if t["statement_category"] and not t["excluded"]}),
    }
