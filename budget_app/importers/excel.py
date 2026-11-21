"""Import the legacy yearly budget workbook.

Expected layout per sheet (one sheet per year, named e.g. "2024"):
    Row 1  : "Pay Check" with income in each month's "Total" column
    Row 2  : month names (Jan .. Dec) above each Spent/Total column pair,
             plus "Previous Year" (carry-over) and "Remainder"
    Row 3  : "Spent" / "Total" headers, column B = planned amount
    Row 4+ : one row per category until a row named "Total"

Spent cells are usually formulas like "=54.47+252.96-10" - each term becomes
its own historical transaction so nothing is lost.
"""
import re

import openpyxl

from ..budget import month_add

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_SUM_ONLY = re.compile(r"^=?\s*[+-]?\s*\d*\.?\d+(\s*[+-]\s*\d*\.?\d+)*\s*$")
_TERM = re.compile(r"[+-]?\s*\d*\.?\d+")

# Generic keyword -> display group used to organize envelopes on screen.
GROUP_KEYWORDS = [
    ("Home", ["house", "home", "hoa", "property", "rent", "mortgage", "internet", "utilit"]),
    ("Giving & Taxes", ["tax", "church", "tithe", "donat", "charity"]),
    ("Food", ["grocer", "food", "eating", "dining", "restaurant"]),
    ("Transportation", ["car", "gas", "license", "auto", "vehicle"]),
    ("Subscriptions & Phone", ["netflix", "hbo", "spotify", "apple", "amazon", "phone", "gym", "music", "subscri"]),
    ("Personal", ["clothes", "beauty", "no ?", "hair", "personal", "toiletr"]),
    ("Health", ["medical", "health", "doctor", "dental"]),
    ("Gifts & Fun", ["gift", "christmas", "vacation", "birthday", "fun"]),
    ("Debt", ["loan", "debt"]),
    ("Work", ["work", "business"]),
]

# Starter subcategories for common envelopes (only added if the envelope has none yet)
SUBCATEGORY_TEMPLATES = [
    (lambda n: "grocer" in n, ["Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery & Bread", "Pantry & Dry Goods",
                               "Snacks & Sweets", "Beverages", "Frozen", "Deli & Prepared"]),
    (lambda n: n in ("house", "home", "housing"), ["Mortgage/Rent", "Utilities", "Household Supplies",
                                                   "Home Improvement", "Furniture & Decor", "Kitchen", "Lawn & Garden"]),
    (lambda n: n in ("car", "auto", "vehicle"), ["Payment", "Maintenance & Repairs", "Car Wash", "Parts & Accessories"]),
    (lambda n: "eating out" in n or "dining" in n, ["Restaurants", "Coffee", "Fast Food", "Drinks & Bars"]),
    (lambda n: "miscellaneous" in n, ["Entertainment", "Hobbies", "Electronics", "Pets", "Fees", "Other"]),
]


def group_for(name):
    n = name.lower()
    for grp, words in GROUP_KEYWORDS:
        if any(w in n for w in words):
            return grp
    return "Other"


def normalize_name(name):
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    n = re.sub(r"(?i)liscense", "License", n)
    return n


def formula_terms(formula_cell, value_cell):
    """Split '=1.5+2-3' into [1.5, 2.0, -3.0]; fall back to the computed value."""
    f = formula_cell
    if isinstance(f, (int, float)):
        return [float(f)] if f else []
    if isinstance(f, str) and _SUM_ONLY.match(f.strip()):
        terms = [float(t.replace(" ", "")) for t in _TERM.findall(f.strip().lstrip("="))]
        return [t for t in terms if t != 0]
    if isinstance(value_cell, (int, float)) and value_cell:
        return [float(value_cell)]
    return []


def _num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


def _layout(ws):
    """Find month columns: {month_index: (spent_col, total_col)} plus special columns."""
    months, prev_col = {}, None
    for cell in ws[2]:
        if not isinstance(cell.value, str):
            continue
        label = cell.value.strip().lower()
        if label[:3] in MONTHS:
            months[MONTHS.index(label[:3]) + 1] = (cell.column, cell.column + 1)
        elif label.startswith("previous"):
            prev_col = cell.column
    return months, prev_col


def year_sheets(wb):
    return sorted((int(ws.title), ws.title) for ws in wb.worksheets if re.fullmatch(r"\d{4}", ws.title.strip()))


def preview(path):
    wbf = openpyxl.load_workbook(path, data_only=False)
    out = []
    for year, title in year_sheets(wbf):
        ws = wbf[title]
        cats = []
        for r in range(4, ws.max_row + 1):
            name = ws.cell(r, 1).value
            if name is None:
                continue
            if str(name).strip().lower() == "total":
                break
            cats.append(normalize_name(name))
        out.append({"year": year, "categories": cats})
    return out


def import_workbook(db, path, replace=False):
    """Import every year sheet. Returns a summary dict."""
    wbf = openpyxl.load_workbook(path, data_only=False)
    wbv = openpyxl.load_workbook(path, data_only=True)
    years = year_sheets(wbf)
    if not years:
        raise ValueError("No sheets named like a year (e.g. '2024') were found.")

    if replace:
        db.execute("DELETE FROM transactions WHERE source = 'excel'")
        db.execute("DELETE FROM income WHERE source = 'excel'")
        db.execute("DELETE FROM adjustments WHERE note LIKE 'Spreadsheet%'")
        first = f"{years[0][0]:04d}-01"
        last = f"{years[-1][0]:04d}-12"
        db.execute("DELETE FROM allocations WHERE month BETWEEN ? AND ?", (first, last))

    cat_ids = {}  # lower(name) -> id

    def category(name, order):
        key = name.lower()
        if key in cat_ids:
            return cat_ids[key]
        r = db.execute(
            "SELECT id FROM categories WHERE LOWER(name) = ? AND parent_id IS NULL", (key,)
        ).fetchone()
        if r:
            cat_ids[key] = r["id"]
        else:
            cur = db.execute(
                "INSERT INTO categories(name, grp, sort_order) VALUES (?, ?, ?)", (name, group_for(name), order)
            )
            cat_ids[key] = cur.lastrowid
        return cat_ids[key]

    summary = {"years": [], "transactions": 0, "income_entries": 0, "adjustments": 0}
    latest_year = years[-1][0]
    names_by_year = {}

    for year, title in years:
        wsf, wsv = wbf[title], wbv[title]
        months, prev_col = _layout(wsf)
        n_txn = 0

        # Income (row 1, "Total" columns)
        for mi, (_s, tcol) in months.items():
            for amt in formula_terms(wsf.cell(1, tcol).value, wsv.cell(1, tcol).value):
                db.execute(
                    "INSERT INTO income(month, date, amount, description, source) VALUES (?, ?, ?, ?, 'excel')",
                    (f"{year:04d}-{mi:02d}", f"{year:04d}-{mi:02d}-01", amt, "Paycheck (spreadsheet)"),
                )
                summary["income_entries"] += 1

        names_by_year[year] = []
        carry_in_excel = {}
        for r in range(4, wsf.max_row + 1):
            raw = wsf.cell(r, 1).value
            if raw is None:
                continue
            name = normalize_name(raw)
            if name.lower() == "total":
                break
            cid = category(name, r)
            names_by_year[year].append(name.lower())
            db.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (r, cid))
            if year == latest_year:
                db.execute("UPDATE categories SET default_amount = ? WHERE id = ?", (_num(wsv.cell(r, 2).value), cid))
            if prev_col:
                carry_in_excel[cid] = _num(wsv.cell(r, prev_col).value)

            for mi, (scol, tcol) in months.items():
                month = f"{year:04d}-{mi:02d}"
                alloc = _num(wsv.cell(r, tcol).value)
                if alloc:
                    db.execute(
                        "INSERT INTO allocations(category_id, month, amount) VALUES (?, ?, ?) "
                        "ON CONFLICT(category_id, month) DO UPDATE SET amount = excluded.amount",
                        (cid, month, alloc),
                    )
                for i, amt in enumerate(formula_terms(wsf.cell(r, scol).value, wsv.cell(r, scol).value)):
                    cur = db.execute(
                        """INSERT INTO transactions(date, description, merchant, amount, account, source,
                               budget_month, reviewed, import_hash)
                           VALUES (?, ?, ?, ?, 'Spreadsheet', 'excel', ?, 1, ?)""",
                        (f"{month}-01", f"{name} (spreadsheet)", name, round(amt, 2), month,
                         f"excel|{title}|{r}|{scol}|{i}"),
                    )
                    db.execute(
                        "INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)",
                        (cur.lastrowid, cid, round(amt, 2)),
                    )
                    n_txn += 1

        # Make the app's carry-over into this year equal the spreadsheet's "Previous Year" column.
        start = f"{year:04d}-01"
        for cid, excel_carry in carry_in_excel.items():
            app_carry = _balance_before(db, cid, start)
            diff = round(excel_carry - app_carry, 2)
            if abs(diff) >= 0.01:
                note = "Spreadsheet opening balance" if year == years[0][0] else "Spreadsheet carry-over reconciliation"
                db.execute(
                    "INSERT INTO adjustments(category_id, month, amount, note) VALUES (?, ?, ?, ?)",
                    (cid, start, diff, note),
                )
                summary["adjustments"] += 1
        # Envelopes that were dropped from this year's sheet: the spreadsheet let their balance vanish.
        for key, cid in cat_ids.items():
            if cid in carry_in_excel:
                continue
            bal = round(_balance_before(db, cid, start), 2)
            if abs(bal) >= 0.01:
                db.execute(
                    "INSERT INTO adjustments(category_id, month, amount, note) VALUES (?, ?, ?, ?)",
                    (cid, start, -bal, "Spreadsheet: envelope retired"),
                )
                summary["adjustments"] += 1
        summary["transactions"] += n_txn
        summary["years"].append({"year": year, "transactions": n_txn, "categories": len(names_by_year[year])})

    # Archive envelopes that no longer appear in the newest year's sheet
    current = set(names_by_year[latest_year])
    for key, cid in cat_ids.items():
        db.execute("UPDATE categories SET archived = ? WHERE id = ?", (0 if key in current else 1, cid))

    add_starter_subcategories(db)
    return summary


def _balance_before(db, cid, month):
    a = db.execute("SELECT COALESCE(SUM(amount),0) v FROM allocations WHERE category_id=? AND month<?", (cid, month)).fetchone()["v"]
    j = db.execute("SELECT COALESCE(SUM(amount),0) v FROM adjustments WHERE category_id=? AND month<?", (cid, month)).fetchone()["v"]
    s = db.execute(
        """SELECT COALESCE(SUM(s.amount),0) v FROM splits s JOIN transactions t ON t.id=s.transaction_id
           WHERE s.category_id=? AND t.budget_month<? AND t.excluded=0""",
        (cid, month),
    ).fetchone()["v"]
    return a + j - s


def add_starter_subcategories(db):
    parents = db.execute("SELECT id, name FROM categories WHERE parent_id IS NULL AND archived = 0").fetchall()
    for p in parents:
        if db.execute("SELECT 1 FROM categories WHERE parent_id = ?", (p["id"],)).fetchone():
            continue
        n = p["name"].strip().lower()
        for test, subs in SUBCATEGORY_TEMPLATES:
            if test(n):
                for i, s in enumerate(subs):
                    db.execute(
                        "INSERT INTO categories(name, parent_id, grp, sort_order) VALUES (?, ?, '', ?)", (s, p["id"], i)
                    )
                break


def recurring_suggestions(db, months_back=12, min_months=4):
    """Amounts that showed up in the same envelope most months -> likely recurring bills."""
    last = db.execute("SELECT MAX(budget_month) m FROM transactions WHERE source='excel'").fetchone()["m"]
    if not last:
        return []
    first = month_add(last, -(months_back - 1))
    q = """SELECT s.category_id, c.name AS category, ROUND(s.amount,2) AS amount,
                  COUNT(DISTINCT t.budget_month) AS months
           FROM splits s JOIN transactions t ON t.id = s.transaction_id
           JOIN categories c ON c.id = s.category_id
           WHERE t.source = 'excel' AND t.budget_month BETWEEN ? AND ? AND s.amount >= 5
           GROUP BY s.category_id, ROUND(s.amount,2)
           HAVING months >= ?
           ORDER BY months DESC, amount DESC"""
    return [dict(r) for r in db.execute(q, (first, last, min_months))]
