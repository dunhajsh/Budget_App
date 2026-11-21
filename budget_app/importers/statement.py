"""Import credit card and bank account CSVs.

Works with SoFi's exports out of the box and tries to auto-detect the columns of
other banks (Chase, Capital One, Discover, Amex, Citi...).

Card statements: every row counts toward one budget month (the statement month).
Bank accounts: each row counts toward the month it happened in, and rows are sorted
into spending, income (paychecks, interest; can count toward the next month's budget),
credits (refunds/reimbursements to categorize), transfers and card payments (left out).

Months already covered by the imported spreadsheet are never added twice. Instead
their rows are matched to the spreadsheet entries (same month, same amount) to give
those entries real merchant names and dates, and to learn merchant -> envelope rules.
"""
import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from datetime import datetime

from ..budget import month_add
from ..categorize import find_category, load_rules, merchant_key, upsert_rule
from ..db import get_setting

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
# Bank-account rows
BANK_TYPES = {"direct_pay", "direct_deposit", "deposit", "withdrawal", "zelle", "interest_earned", "check_deposit",
              "atm", "ach", "transfer", "debit_card", "pos", "credit_card_direct_payment", "check", "fee", "other"}
TRANSFER_PAT = re.compile(r"^(FROM|TO)\s+(SAVINGS|CHECKING|SHARE|MONEY MARKET|VAULT)|\bTRANSFER\b|\bXFER\b", re.I)
CARD_PAY_PAT = re.compile(r"\bCC PAY|CREDIT CARD|CRD PMT|CARD PAYMENT|CARDMEMBER SERV|PAYMENT TO .*CARD", re.I)
INCOME_TYPES = {"DIRECT_DEPOSIT", "PAYROLL", "INTEREST_EARNED", "INTEREST", "DIVIDEND"}
INCOME_PAT = re.compile(r"PAYROLL|DIR DEP|DIRECT DEP|\bINTEREST\b|DIVIDEND|IRS\s+TREAS|TAX REF|^STATE OF |"
                        r"REWARDS? REDEMPTION|CASH ?BACK REDEMPTION", re.I)

# Merchant keys too generic to become rules on their own.
GENERIC_KEYS = {"LLC", "INC", "PAYPAL", "VENMO", "SQUARE", "AMAZON", "AMAZON MARK", "AMAZON MKTPL", "AMAZON RETA",
                "WITHDRAWAL", "DEPOSIT", "CHECK", "ZELLE", "ATM"}


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


def classify_bank(description, txn_type, raw_amount):
    """flow for a bank row: spend | credit | income | transfer | card_payment (raw_amount < 0 = money out)."""
    ty = (txn_type or "").upper()
    if ty == "TRANSFER" or TRANSFER_PAT.search(description):
        return "transfer"
    if raw_amount < 0:
        if ty == "CREDIT_CARD_DIRECT_PAYMENT" or CARD_PAY_PAT.search(description) or PAYMENT_PAT.search(description):
            return "card_payment"
        return "spend"
    if ty in INCOME_TYPES or INCOME_PAT.search(description):
        return "income"
    return "credit"


def parse_csv(text):
    """Return normalized rows. amount > 0 = money spent (or income received, for flow 'income');
    amount < 0 = a refund/credit. Each row has 'flow' and 'kind' ('card' or 'bank')."""
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
    has_balance = any("balance" in (h or "").lower() for h in headers)
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
            amt = cred - deb  # bank-style: negative = money out
        if amt is None:
            continue
        out.append(
            {
                "date": _date(row[c_date]),
                "post_date": _date(row[c_post]) if c_post and row.get(c_post) and c_post != c_date else None,
                "description": row[c_desc].strip(),
                "raw": amt,
                "statement_category": (row.get(c_cat) or "").strip() or None if c_cat else None,
                "txn_type": (row.get(c_type) or "").strip() or None if c_type else None,
            }
        )
    if not out:
        return out

    types = [(t["txn_type"] or "").lower() for t in out]
    is_bank = has_balance or (not c_amt) or sum(1 for ty in types if ty in BANK_TYPES) > len(out) / 2

    if is_bank:
        for t in out:
            t["kind"] = "bank"
            t["flow"] = classify_bank(t["description"], t["txn_type"], t["raw"])
            # Spending positive, credits negative, income positive.
            t["amount"] = abs(t["raw"]) if t["flow"] in ("income", "transfer", "card_payment") else -t["raw"]
    else:
        # Card statements: most exports list purchases as positive; some as negative.
        sales = [t for t in out if (t["txn_type"] or "").lower() in ("sale", "purchase", "debit")]
        sample = sales or [t for t in out if not PAYMENT_PAT.search(t["description"])]
        flip = bool(sample) and sum(1 for t in sample if t["raw"] < 0) > len(sample) / 2
        for t in out:
            t["kind"] = "card"
            t["amount"] = -t["raw"] if flip else t["raw"]
            payment = ((t["txn_type"] or "").lower() == "payment" or (t["statement_category"] or "").lower() == "payment"
                       or bool(PAYMENT_PAT.search(t["description"])))
            t["flow"] = "card_payment" if payment else ("spend" if t["amount"] >= 0 else "credit")

    for t in out:
        t["excluded"] = t["flow"] in ("transfer", "card_payment")
        t["amount"] = round(t["amount"], 2)
        del t["raw"]
    return out


def guess_budget_month(txns, filename=""):
    """Statement month: prefer the dates in the filename, else the most common month."""
    dates = re.findall(r"(\d{1,2})[_\-](\d{1,2})[_\-](\d{4})", filename or "")
    if len(dates) >= 2:
        m, _, y = dates[-1]
        return f"{int(y):04d}-{int(m):02d}"
    c = Counter(t["date"][:7] for t in txns)
    return c.most_common(1)[0][0] if c else datetime.now().strftime("%Y-%m")


def guess_account(filename, kind, default=None):
    f = (filename or "").lower()
    if kind == "bank":
        base = "Savings" if "saving" in f else "Checking" if "checking" in f else "Bank account"
        return ("Joint " + base) if "joint" in f else base
    return default or "Credit Card"


def history_months(db):
    """Months whose spending came from the imported spreadsheet."""
    return {r[0] for r in db.execute("SELECT DISTINCT budget_month FROM transactions WHERE source = 'excel'")}


def income_offset(db):
    try:
        return int(get_setting(db, "income_month_offset") or 0)
    except ValueError:
        return 0


def preview(db, text, filename):
    txns = parse_csv(text)
    kind = txns[0]["kind"] if txns else "card"
    hist = history_months(db)
    last_hist = max(hist) if hist else ""
    offset = income_offset(db)
    flows = Counter(t["flow"] for t in txns)
    month = guess_budget_month(txns, filename)
    in_hist = sum(1 for t in txns if (t["date"][:7] if kind == "bank" else month) <= last_hist and t["flow"] != "income")
    return {
        "kind": kind,
        "count": len(txns),
        "budget_month": month,
        "first": min((t["date"] for t in txns), default=None),
        "last": max((t["date"] for t in txns), default=None),
        "total_spent": round(sum(t["amount"] for t in txns if t["flow"] in ("spend", "credit")), 2),
        "payments": flows["card_payment"],
        "transfers": flows["transfer"],
        "income": flows["income"],
        "credits": flows["credit"],
        "in_history": in_hist,
        "history_through": last_hist or None,
        "income_offset": offset,
    }


# ---------------------------------------------------------------- matching to spreadsheet history

def _enrich_txn(db, month, t, account, rules, stmt_map):
    """Give a spreadsheet entry with the same month and amount this row's merchant and date.
    If entries with that amount sit in different envelopes (e.g. two $150 bills), use an existing
    rule or the card's own category to pick one; with no clue, leave it alone rather than guess."""
    cands = db.execute(
        """SELECT t.id, s.category_id FROM transactions t LEFT JOIN splits s ON s.transaction_id = t.id
           WHERE t.source = 'excel' AND t.budget_month = ? AND ABS(t.amount - ?) < 0.005
               AND t.description LIKE '%(spreadsheet)' ORDER BY t.id""",
        (month, t["amount"]),
    ).fetchall()
    if not cands:
        return False
    pick = cands[0]
    if len({_envelope(db, c["category_id"]) for c in cands}) > 1:
        hint, _ = find_category(db, t["description"], t["statement_category"], rules=rules, stmt_map=stmt_map)
        hint_env = _envelope(db, hint) if hint else None
        same = [c for c in cands if hint_env and _envelope(db, c["category_id"]) == hint_env]
        if not same:
            return False
        pick = same[0]
    db.execute("UPDATE transactions SET description = ?, merchant = ?, date = ?, account = ? WHERE id = ?",
               (t["description"], merchant_key(t["description"]), t["date"], account, pick["id"]))
    return True


def _enrich_income(db, month, t):
    r = db.execute(
        """SELECT id FROM income WHERE source = 'excel' AND month = ? AND ABS(amount - ?) < 0.005
               AND description LIKE '%(spreadsheet)' ORDER BY id LIMIT 1""",
        (month, t["amount"]),
    ).fetchone()
    if not r:
        return False
    db.execute("UPDATE income SET description = ?, date = ? WHERE id = ?", (t["description"], t["date"], r["id"]))
    return True


def _envelope(db, cid):
    r = db.execute("SELECT id, parent_id FROM categories WHERE id = ?", (cid,)).fetchone()
    return (r["parent_id"] or r["id"]) if r else None


def learn_rules(db, min_count=2, min_share=0.8):
    """Create merchant rules from spreadsheet entries that have merchant names (see _enrich_txn).
    Only merchants that went to the same envelope consistently. Existing rules are never changed.
    Returns ([(pattern, category name)], [(pattern, existing category, history category)])."""
    counts = defaultdict(Counter)
    for r in db.execute(
        """SELECT t.merchant, s.category_id FROM transactions t JOIN splits s ON s.transaction_id = t.id
           WHERE t.source = 'excel' AND t.merchant IS NOT NULL AND t.amount > 0
               AND t.description NOT LIKE '%(spreadsheet)'"""
    ):
        counts[r["merchant"]][r["category_id"]] += 1
    rules = load_rules(db)
    learned, conflicts = [], []
    for key, c in counts.items():
        cid, n = c.most_common(1)[0]
        total = sum(c.values())
        key = key.strip()
        if n < min_count or n / total < min_share or len(key) < 4 or key.upper() in GENERIC_KEYS:
            continue
        existing, how = find_category(db, key, rules=rules, stmt_map={})
        if existing:
            if _envelope(db, existing) != _envelope(db, cid):
                conflicts.append((key, existing, cid))
            continue
        upsert_rule(db, key, cid)
        name = db.execute("SELECT name FROM categories WHERE id = ?", (cid,)).fetchone()["name"]
        learned.append((key, name))
        rules = load_rules(db)
    return learned, conflicts


def recategorize_unreviewed(db):
    """Re-run rules on imported rows nobody has reviewed yet (e.g. after new rules were learned)."""
    rules = load_rules(db)
    stmt_map = {r["statement_category"]: r["category_id"] for r in db.execute("SELECT * FROM statement_category_map")}
    changed = 0
    for t in db.execute(
        """SELECT t.id, t.description, t.statement_category, t.amount,
                  (SELECT COUNT(*) FROM splits s WHERE s.transaction_id = t.id) AS n,
                  (SELECT category_id FROM splits s WHERE s.transaction_id = t.id LIMIT 1) AS cid
           FROM transactions t WHERE t.source = 'statement' AND t.reviewed = 0 AND t.excluded = 0""").fetchall():
        if t["n"] > 1:
            continue
        cid, _ = find_category(db, t["description"], t["statement_category"], rules=rules, stmt_map=stmt_map)
        if cid and cid != t["cid"]:
            db.execute("DELETE FROM splits WHERE transaction_id = ?", (t["id"],))
            db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)", (t["id"], cid, t["amount"]))
            changed += 1
    return changed


# ---------------------------------------------------------------- import

def import_statement(db, text, filename, account=None, budget_month=None):
    txns = parse_csv(text)
    empty = {"imported": 0, "duplicates": 0, "auto_categorized": 0, "budget_month": budget_month}
    if not txns:
        return empty
    is_bank = txns[0]["kind"] == "bank"
    account = account or guess_account(filename, txns[0]["kind"])
    if not is_bank:
        budget_month = budget_month or guess_budget_month(txns, filename)
    hist = history_months(db)
    last_hist = max(hist) if hist else ""
    offset = income_offset(db)

    cur = db.execute(
        "INSERT INTO import_batches(kind, filename, budget_month, row_count) VALUES (?, ?, ?, ?)",
        ("bank" if is_bank else "statement", filename, None if is_bank else budget_month, len(txns)),
    )
    batch_id = cur.lastrowid
    rules = load_rules(db)
    stmt_map = {r["statement_category"]: r["category_id"] for r in db.execute("SELECT * FROM statement_category_map")}

    seen = Counter()
    res = Counter()
    months = Counter()
    income_added = []
    for t in txns:
        month = t["date"][:7] if is_bank else budget_month
        key = f"{account}|{t['date']}|{t['description']}|{t['amount']:.2f}"
        seen[key] += 1
        h = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()

        if t["flow"] == "income":
            inc_month = month_add(month, offset)
            if _enrich_income(db, inc_month, t):
                res["history_matched"] += 1
            elif inc_month <= last_hist:
                res["history_skipped"] += 1
            elif db.execute("SELECT 1 FROM income WHERE date = ? AND ABS(amount - ?) < 0.005 AND description = ?",
                            (t["date"], t["amount"], t["description"])).fetchone():
                res["duplicates"] += 1
            else:
                db.execute("INSERT INTO income(month, date, amount, description, source, batch_id) "
                           "VALUES (?, ?, ?, ?, 'statement', ?)", (inc_month, t["date"], t["amount"], t["description"], batch_id))
                income_added.append({"month": inc_month, "date": t["date"], "amount": t["amount"], "description": t["description"]})
            continue

        if month <= last_hist:
            if not t["excluded"] and _enrich_txn(db, month, t, account, rules, stmt_map):
                res["history_matched"] += 1
            else:
                res["history_skipped"] += 1
            continue

        if db.execute("SELECT 1 FROM transactions WHERE import_hash = ?", (h,)).fetchone():
            res["duplicates"] += 1
            continue
        cur = db.execute(
            """INSERT INTO transactions(date, post_date, description, merchant, amount, account, source,
                   budget_month, statement_category, txn_type, excluded, reviewed, import_hash, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, 'statement', ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["date"], t["post_date"], t["description"], merchant_key(t["description"]), t["amount"],
                account, month, t["statement_category"], t["txn_type"], int(t["excluded"]),
                int(t["excluded"]), h, batch_id,
            ),
        )
        res["imported"] += 1
        months[month] += 1
        if t["excluded"]:
            res["left_out"] += 1
            continue
        cid, how = find_category(db, t["description"], t["statement_category"], rules=rules, stmt_map=stmt_map)
        if cid:
            db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)",
                       (cur.lastrowid, cid, t["amount"]))
            if how.startswith("rule:"):
                db.execute("UPDATE rules SET hits = hits + 1 WHERE id = ?", (int(how[5:]),))
            res["auto_categorized"] += 1

    learned, conflicts = ([], [])
    recat = 0
    if res["history_matched"]:
        learned, conflicts = learn_rules(db)
        if learned:
            recat = recategorize_unreviewed(db)
    db.execute("UPDATE import_batches SET row_count = ? WHERE id = ?", (res["imported"] + len(income_added), batch_id))
    return {
        "batch_id": batch_id,
        "kind": "bank" if is_bank else "card",
        "account": account,
        "imported": res["imported"],
        "duplicates": res["duplicates"],
        "auto_categorized": res["auto_categorized"],
        "left_out": res["left_out"],
        "history_matched": res["history_matched"],
        "history_skipped": res["history_skipped"],
        "income_added": income_added,
        "rules_learned": [{"pattern": p, "category": c} for p, c in learned],
        "recategorized": recat,
        "budget_month": (months.most_common(1)[0][0] if months else None) if is_bank else budget_month,
        "months": dict(months),
        "statement_categories": sorted({t["statement_category"] for t in txns
                                        if t["statement_category"] and not t["excluded"]}),
    }
