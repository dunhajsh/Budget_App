"""Rule-based auto-categorization of card transactions."""
import re

_PREFIXES = re.compile(r"^(TST\*|SQ \*|SQ\*|SP \*|SP\*|PP\*|PAYPAL \*|ACI\*|DD \*|IC\*|WPY\*|PY \*)\s*", re.I)
_STORE_NO = re.compile(r"\s*#\s*\d+.*$")
_TRAILING_CODE = re.compile(r"[\*\s]+[A-Z0-9]*\d[A-Z0-9]{4,}\b.*$", re.I)
_LEADING_NUM = re.compile(r"^\d{3,}\s+")
_TRAILING_NUM = re.compile(r"\s+\d{3,}\b.*$")
_PUNCT = re.compile(r"[\s,\-\.\*]+$")


def merchant_key(description):
    """Reduce a raw card descriptor to a stable merchant name.

    'MEIJER STORE #330' -> 'MEIJER STORE'
    'AMAZON MKTPL*VH9KI4UJ3' -> 'AMAZON MKTPL'
    'SQ *8 SISTERS BAKERY,' -> '8 SISTERS BAKERY'
    'DEPOP* P478869118251 BROO' -> 'DEPOP'
    """
    s = (description or "").upper().strip()
    s = _PREFIXES.sub("", s)
    s = _STORE_NO.sub("", s)
    s = _TRAILING_CODE.sub("", s)
    s = _LEADING_NUM.sub("", s)
    s = _TRAILING_NUM.sub("", s)
    s = _PUNCT.sub("", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip() or (description or "").upper().strip()


def _matches(rule, text):
    pat = rule["pattern"].upper()
    mt = rule["match_type"]
    if mt == "exact":
        return merchant_key(text) == pat
    if mt == "regex":
        try:
            return re.search(rule["pattern"], text, re.I) is not None
        except re.error:
            return False
    return pat in text.upper()


def load_rules(db):
    # Higher priority first, then longer (more specific) patterns first
    return [
        dict(r)
        for r in db.execute(
            "SELECT * FROM rules ORDER BY priority DESC, LENGTH(pattern) DESC, id"
        )
    ]


def find_category(db, description, statement_category=None, rules=None, stmt_map=None):
    """Return (category_id, how) or (None, None)."""
    rules = load_rules(db) if rules is None else rules
    for r in rules:
        if _matches(r, description):
            return r["category_id"], f"rule:{r['id']}"
    if statement_category:
        if stmt_map is None:
            stmt_map = {
                row["statement_category"]: row["category_id"]
                for row in db.execute("SELECT * FROM statement_category_map")
            }
        cid = stmt_map.get(statement_category)
        if cid:
            return cid, "statement_category"
    return None, None


def upsert_rule(db, pattern, category_id, match_type="contains"):
    pattern = pattern.strip().upper()
    existing = db.execute(
        "SELECT id FROM rules WHERE UPPER(pattern) = ? AND match_type = ?", (pattern, match_type)
    ).fetchone()
    if existing:
        db.execute("UPDATE rules SET category_id = ? WHERE id = ?", (category_id, existing["id"]))
        return existing["id"]
    cur = db.execute(
        "INSERT INTO rules(pattern, match_type, category_id) VALUES (?, ?, ?)",
        (pattern, match_type, category_id),
    )
    return cur.lastrowid


def set_single_category(db, txn_id, category_id, reviewed=True):
    """Replace a transaction's splits with one split for the full amount."""
    t = db.execute("SELECT amount FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
    if category_id:
        db.execute(
            "INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)",
            (txn_id, category_id, t["amount"]),
        )
    db.execute("UPDATE transactions SET reviewed = ? WHERE id = ?", (1 if reviewed else 0, txn_id))


def apply_rules_to_uncategorized(db, month=None):
    """Re-run rules over uncategorized, unreviewed transactions. Returns count categorized."""
    rules = load_rules(db)
    q = """SELECT t.id, t.description, t.statement_category FROM transactions t
           WHERE t.excluded = 0 AND t.reviewed = 0 AND t.source != 'excel'
             AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)"""
    args = []
    if month:
        q += " AND t.budget_month = ?"
        args.append(month)
    n = 0
    for t in db.execute(q, args).fetchall():
        cid, _ = find_category(db, t["description"], t["statement_category"], rules=rules)
        if cid:
            set_single_category(db, t["id"], cid, reviewed=False)
            n += 1
    return n
