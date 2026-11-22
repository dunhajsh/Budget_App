"""Run with:  python -m unittest discover -s tests -v"""
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile

import openpyxl

from budget_app import create_app
from budget_app.budget import month_summary
from budget_app.categorize import merchant_key
from budget_app.db import connect, init_db
from budget_app.importers import amazon, excel, statement
from budget_app.receipts import compute_splits

SOFI = """Transaction Date,Post Date,Description,Category,Type,Amount
2026-10-30,2026-10-30,MEIJER STORE #330,Groceries,Sale,47.04
2026-10-29,2026-10-29,AMAZON MKTPL*VH9KI4UJ3,Shopping,Sale,54.21
2026-10-26,2026-10-26,DEPOP* P478869118251,Shopping,Return Credit,-24.11
2026-10-14,2026-10-14,AUTO-PAY THANK YOU,Payment,Payment,-500.00
2026-10-10,2026-10-10,MEIJER STORE #330,Groceries,Sale,12.00
2026-10-10,2026-10-10,MEIJER STORE #330,Groceries,Sale,12.00
2026-09-30,2026-09-30,COFFEE SHOP,Dining & Drinks,Sale,5.25
"""

CHASE_STYLE = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
10/05/2026,10/06/2026,GROCERY OUTLET,Groceries,Sale,-30.00,
10/07/2026,10/07/2026,PAYMENT THANK YOU-MOBILE,,Payment,200.00,
10/08/2026,10/08/2026,SHOE STORE,Shopping,Return,15.00,
"""


def make_db(tmp):
    path = os.path.join(tmp, "t.db")
    init_db(path)
    return connect(path)


def make_workbook(path):
    """Two-year workbook in the legacy layout."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for year, prev in ((2024, {"Food": 100, "Fun": 0}), (2025, None)):
        ws = wb.create_sheet(str(year))
        ws["A1"] = "Pay Check"
        for i, m in enumerate(months):
            ws.cell(2, 3 + i * 2, m)
            ws.cell(3, 3 + i * 2, "Spent")
            ws.cell(3, 4 + i * 2, "Total")
        ws.cell(2, 27, "Previous Year")
        ws.cell(2, 30, "Remainder")
        ws.cell(1, 4, "=1000+500")  # Jan income
        cats = ["Food", "Fun"] if year == 2024 else ["Food", "Fun", "Gifts"]
        for r, name in enumerate(cats, start=4):
            ws.cell(r, 1, name)
            ws.cell(r, 2, 200 if name == "Food" else 50)
            ws.cell(r, 4, 200 if name == "Food" else 50)        # Jan allocated
            ws.cell(r, 3, "=10.5+20-5" if name == "Food" else 12)  # Jan spent
        ws.cell(4 + len(cats), 1, "Total")
        # Cached "Previous Year" values (openpyxl writes no cached formula results, so use plain numbers)
        if prev:
            for r, name in enumerate(cats, start=4):
                ws.cell(r, 27, prev.get(name, 0))
        else:
            # 2024 end: Food 100+200-25.5 = 274.5 ; Fun 0+50-12 = 38. Pretend the sheet carried Fun as 40.
            ws.cell(4, 27, 274.5)
            ws.cell(5, 27, 40)
            ws.cell(6, 27, 0)
    wb.save(path)


class MerchantKeyTests(unittest.TestCase):
    def test_cleanup(self):
        cases = {
            "MEIJER STORE #330": "MEIJER STORE",
            "AMAZON MKTPL*VH9KI4UJ3": "AMAZON MKTPL",
            "SQ *8 SISTERS BAKERY,": "8 SISTERS BAKERY",
            "DEPOP* P478869118251 BROO": "DEPOP",
            "TST* CARIBOU COFFEE -": "CARIBOU COFFEE",
            "636 TAKE 5 CAR WASH": "TAKE 5 CAR WASH",
            "GAS STOP 585 SPRINGFIELD": "GAS STOP",
            "NETFLIX.COM": "NETFLIX.COM",
        }
        for raw, want in cases.items():
            self.assertEqual(merchant_key(raw), want, raw)


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = make_db(self.tmp)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_parse_sofi(self):
        txns = statement.parse_csv(SOFI)
        self.assertEqual(len(txns), 7)
        pay = [t for t in txns if t["excluded"]]
        self.assertEqual(len(pay), 1)
        self.assertEqual(txns[2]["amount"], -24.11)  # return stays negative
        self.assertEqual(statement.guess_budget_month(txns, "Card-10_01_2026-10_31_2026.csv"), "2026-10")

    def test_negative_purchases_are_flipped(self):
        txns = statement.parse_csv(CHASE_STYLE)
        self.assertEqual(txns[0]["amount"], 30.0)
        self.assertTrue(txns[1]["excluded"])
        self.assertEqual(txns[2]["amount"], -15.0)

    def test_import_dedupes_and_keeps_same_day_duplicates(self):
        r1 = statement.import_statement(self.db, SOFI, "s.csv", "Card")
        self.assertEqual(r1["imported"], 7)
        r2 = statement.import_statement(self.db, SOFI, "s.csv", "Card")
        self.assertEqual(r2["imported"], 0)
        self.assertEqual(r2["duplicates"], 7)
        n = self.db.execute("SELECT COUNT(*) FROM transactions WHERE amount = 12").fetchone()[0]
        self.assertEqual(n, 2)

    def test_rules_and_statement_map(self):
        food = self.db.execute("INSERT INTO categories(name) VALUES ('Food')").lastrowid
        eat = self.db.execute("INSERT INTO categories(name) VALUES ('Eating Out')").lastrowid
        self.db.execute("INSERT INTO rules(pattern, category_id) VALUES ('MEIJER STORE', ?)", (food,))
        self.db.execute("INSERT INTO statement_category_map VALUES ('Dining & Drinks', ?)", (eat,))
        r = statement.import_statement(self.db, SOFI, "s.csv", "Card")
        self.assertEqual(r["auto_categorized"], 4)  # 3 Meijer + 1 coffee
        s = month_summary(self.db, "2026-10")
        spent = {e["name"]: e["spent"] for e in s["envelopes"]}
        self.assertAlmostEqual(spent["Food"], 71.04)
        self.assertAlmostEqual(spent["Eating Out"], 0)  # the Sep 30 charge counts in September, when it happened
        sep = {e["name"]: e["spent"] for e in month_summary(self.db, "2026-09")["envelopes"]}
        self.assertAlmostEqual(sep["Eating Out"], 5.25)
        self.assertEqual(s["uncategorized_count"], 2)


class ExcelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = make_db(self.tmp)
        self.path = os.path.join(self.tmp, "b.xlsx")
        make_workbook(self.path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_formula_terms(self):
        self.assertEqual(excel.formula_terms("=54.47+252.96-10", None), [54.47, 252.96, -10.0])
        self.assertEqual(excel.formula_terms("=-700+23.38", None), [-700.0, 23.38])
        self.assertEqual(excel.formula_terms("=3402*0.4", 1360.8), [1360.8])
        self.assertEqual(excel.formula_terms(None, None), [])
        self.assertEqual(excel.formula_terms(42, 42), [42.0])

    def test_import_matches_spreadsheet_balances(self):
        res = excel.import_workbook(self.db, self.path)
        self.assertEqual(len(res["years"]), 2)
        jan24 = month_summary(self.db, "2024-01")
        food = next(e for e in jan24["envelopes"] if e["name"] == "Food")
        self.assertEqual(food["carry_in"], 100)
        self.assertAlmostEqual(food["spent"], 25.5)
        self.assertEqual(jan24["income"], 1500)
        # The 2025 sheet says Fun carried 40 (not the computed 38): the app must agree with the sheet.
        jan25 = month_summary(self.db, "2025-01")
        fun = next(e for e in jan25["envelopes"] if e["name"] == "Fun")
        self.assertAlmostEqual(fun["carry_in"], 40)
        # Spent cell formula split into individual entries
        n = self.db.execute("SELECT COUNT(*) FROM transactions WHERE source='excel' AND budget_month='2024-01' "
                            "AND description LIKE 'Food%'").fetchone()[0]
        self.assertEqual(n, 3)

    def test_reimport_replace_is_idempotent(self):
        excel.import_workbook(self.db, self.path)
        a = month_summary(self.db, "2025-12")["total_available"]
        excel.import_workbook(self.db, self.path, replace=True)
        b = month_summary(self.db, "2025-12")["total_available"]
        self.assertAlmostEqual(a, b)


class ReceiptSplitTests(unittest.TestCase):
    def test_tax_goes_to_taxable_items_only(self):
        items = [
            {"category_id": 1, "amount": 20.00, "taxable": False},  # food
            {"category_id": 2, "amount": 10.00, "taxable": True},   # soap
        ]
        out = {c: a for c, a, _ in compute_splits(items, tax=0.70, target_total=30.70)}
        self.assertEqual(out, {1: 20.00, 2: 10.70})

    def test_scales_to_card_amount_and_rounds_exactly(self):
        items = [{"category_id": i % 3, "amount": 3.33, "taxable": True} for i in range(7)]
        out = compute_splits(items, tax=1.11, target_total=25.00)
        self.assertAlmostEqual(sum(a for _, a, _ in out), 25.00, places=2)

    def test_discount_lines(self):
        items = [{"category_id": 1, "amount": 10.0, "taxable": False}, {"category_id": 1, "amount": -2.0, "taxable": False}]
        self.assertEqual(compute_splits(items, 0, 8.0), [(1, 8.0, 2)])


class AmazonTests(unittest.TestCase):
    CSV = ('"Website","Order ID","Order Date","Ship Date","Product Name","Quantity","Total Owed","Order Status"\n'
           '"Amazon.com","111-1","2026-10-27T10:00:00Z","2026-10-28T10:00:00Z","Dish soap","1","10.00","Closed"\n'
           '"Amazon.com","111-1","2026-10-27T10:00:00Z","2026-10-28T10:00:00Z","Phone case","1","44.21","Closed"\n'
           '"Amazon.com","111-1","2026-10-27T10:00:00Z","2026-10-30T10:00:00Z","Batteries","1","13.79","Closed"\n'
           '"Amazon.com","222-2","2026-10-01T10:00:00Z","Not Available","Cancelled thing","1","99.00","Cancelled"\n')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = make_db(self.tmp)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_shipments_grouped_and_matched(self):
        statement.import_statement(self.db, SOFI + "2026-10-31,2026-10-31,AMAZON RETA* KY9RQ3373,Shopping,Sale,13.79\n", "s.csv", "Card")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Retail.OrderHistory.1/Retail.OrderHistory.1.csv", self.CSV)
        res = amazon.import_orders(self.db, "orders.zip", buf.getvalue(), use_ai=False)
        self.assertEqual(res["shipments"], 2)       # cancelled order skipped
        self.assertEqual(res["matched"], 2)         # 54.21 and 13.79 charges
        again = amazon.import_orders(self.db, "orders.zip", buf.getvalue(), use_ai=False)
        self.assertEqual(again["already_imported"], 2)


BANK_CSV = """Date,Description,Type,Amount,Current balance,Status
2026-03-14,ACME CORP PAYROLL,DIRECT_DEPOSIT,2500.00,5000.00,Posted
2026-03-10,MORTGAGE CO,DIRECT_PAY,-1500.00,2500.00,Posted
2026-03-05,CITY WATER,DIRECT_PAY,-60.00,4000.00,Posted
2026-03-02,CITY WATER,DIRECT_PAY,-55.00,4060.00,Posted
2026-02-28,Interest earned,INTEREST_EARNED,4.10,4115.00,Posted
2026-02-20,ACME CORP PAYROLL,DIRECT_DEPOSIT,2500.00,4110.90,Posted
2026-02-16,BIG BANK CC PAY,DIRECT_PAY,-900.00,1610.90,Posted
2026-02-11,CITY WATER,DIRECT_PAY,-58.00,2510.90,Posted
2026-02-10,MORTGAGE CO,DIRECT_PAY,-1500.00,2568.90,Posted
2026-02-03,To Checking - 1234,WITHDRAWAL,-100.00,4068.90,Posted
2026-02-02,FRIEND REIMBURSED,ZELLE,45.00,4168.90,Posted
2026-01-10,MORTGAGE CO,DIRECT_PAY,-1500.00,4123.90,Posted
2026-01-20,ACME CORP PAYROLL,DIRECT_DEPOSIT,2500.00,6623.90,Posted
"""


class BankAccountTests(unittest.TestCase):
    """Bank exports: flows, months, income offset, and learning from spreadsheet history."""

    def setUp(self):
        from budget_app.db import set_setting
        self.tmp = tempfile.mkdtemp()
        self.db = make_db(self.tmp)
        db = self.db
        db.execute("INSERT INTO categories(id, name) VALUES (1, 'House'), (2, 'Utilities'), (3, 'Misc')")
        # Spreadsheet history covers Jan-Feb 2026: one entry per amount, as the Excel importer creates them.
        for month, amt, cid in [("2026-01", 1500.0, 1), ("2026-02", 1500.0, 1), ("2026-02", 58.0, 2), ("2026-02", 12.0, 3)]:
            cur = db.execute("INSERT INTO transactions(date, description, amount, source, budget_month) "
                             "VALUES (?, ?, ?, 'excel', ?)", (month + "-01", f"x (spreadsheet)", amt, month))
            db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)", (cur.lastrowid, cid, amt))
        db.execute("INSERT INTO income(month, date, amount, description, source) VALUES "
                   "('2026-02', '2026-02-01', 2500.0, 'Paycheck (spreadsheet)', 'excel')")
        set_setting(db, "income_month_offset", "1")   # paychecks fund next month's budget
        db.commit()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_classification(self):
        rows = statement.parse_csv(BANK_CSV)
        flows = {(r["date"], r["description"]): (r["flow"], r["amount"]) for r in rows}
        self.assertTrue(all(r["kind"] == "bank" for r in rows))
        self.assertEqual(flows[("2026-03-14", "ACME CORP PAYROLL")], ("income", 2500.0))
        self.assertEqual(flows[("2026-03-10", "MORTGAGE CO")], ("spend", 1500.0))
        self.assertEqual(flows[("2026-02-16", "BIG BANK CC PAY")][0], "card_payment")
        self.assertEqual(flows[("2026-02-03", "To Checking - 1234")][0], "transfer")
        self.assertEqual(flows[("2026-02-02", "FRIEND REIMBURSED")], ("credit", -45.0))
        self.assertEqual(flows[("2026-02-28", "Interest earned")][0], "income")

    def test_import_matches_history_learns_rules_and_adds_new_months(self):
        res = statement.import_statement(self.db, BANK_CSV, "Joint-Savings.csv")
        self.assertEqual(res["account"], "Joint Savings")
        # Jan + Feb mortgage, Feb water, Jan paycheck (-> Feb income) matched to the spreadsheet.
        self.assertEqual(res["history_matched"], 4)
        self.assertEqual(res["months"], {"2026-03": 3})        # only March rows are new
        # A rule is learned only when a merchant consistently went to one envelope (2+ times).
        self.assertEqual(res["rules_learned"], [{"pattern": "MORTGAGE CO", "category": "House"}])
        mort = self.db.execute("SELECT * FROM transactions WHERE source = 'statement' AND description = 'MORTGAGE CO'").fetchone()
        self.assertEqual(mort["budget_month"], "2026-03")
        self.assertEqual(self.db.execute("SELECT category_id FROM splits WHERE transaction_id = ?", (mort["id"],)).fetchone()[0], 1)
        # The statement line replaced the spreadsheet entry (kept, excluded, for undo); same envelope and total.
        h = self.db.execute("SELECT * FROM transactions WHERE source = 'excel' AND budget_month = '2026-01'").fetchone()
        self.assertEqual((h["excluded"], h["replaced_by"]), (1, res["batch_id"]))
        jan = self.db.execute("""SELECT t.*, s.category_id FROM transactions t JOIN splits s ON s.transaction_id = t.id
                                 WHERE t.source = 'statement' AND t.budget_month = '2026-01' AND t.excluded = 0""").fetchone()
        self.assertEqual((jan["description"], jan["date"], jan["amount"], jan["category_id"]), ("MORTGAGE CO", "2026-01-10", 1500.0, 1))
        self.assertAlmostEqual(month_summary(self.db, "2026-02")["spent"], 1500 + 58 + 12)   # unchanged
        # Lines that aren't in the spreadsheet are recorded but not budgeted.
        friend = self.db.execute("SELECT * FROM transactions WHERE description = 'FRIEND REIMBURSED'").fetchone()
        self.assertEqual((friend["excluded"], friend["budget_month"]), (1, "2026-02"))
        # Income: Feb paycheck (in Feb history) -> skipped/matched; Feb interest -> March income; March paycheck -> April.
        inc = {(r["month"], r["amount"]) for r in self.db.execute("SELECT * FROM income WHERE source = 'statement'")}
        self.assertEqual(inc, {("2026-03", 2500.0), ("2026-03", 4.1), ("2026-04", 2500.0)})
        self.assertEqual(self.db.execute("SELECT description FROM income WHERE source = 'excel'").fetchone()[0], "ACME CORP PAYROLL")

        # Re-import: nothing new.
        again = statement.import_statement(self.db, BANK_CSV, "Joint-Savings.csv")
        self.assertEqual((again["imported"], len(again["income_added"])), (0, 0))
        self.assertEqual(again["history_skipped"], res["history_skipped"])   # earlier matches count as duplicates

    def test_same_amount_in_two_envelopes_is_not_guessed(self):
        db = self.db
        db.execute("INSERT INTO categories(id, name) VALUES (4, 'Church'), (5, 'Loans')")
        for cid in (4, 5):   # two $150 entries in different envelopes, same month
            cur = db.execute("INSERT INTO transactions(date, description, amount, source, budget_month) "
                             "VALUES ('2026-02-01', 'x (spreadsheet)', 150.0, 'excel', '2026-02')")
            db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, 150.0)", (cur.lastrowid, cid))
        csv_text = ("Date,Description,Type,Amount,Current balance,Status\n"
                    "2026-02-05,GIVING CO,DIRECT_PAY,-150.00,100,Posted\n2026-02-15,LOAN SERVICER,DIRECT_PAY,-150.00,100,Posted\n")
        res = statement.import_statement(db, csv_text, "checking.csv")
        self.assertEqual(res["history_matched"], 0)          # no clue which is which -> left alone
        db.execute("DELETE FROM transactions WHERE batch_id = ?", (res["batch_id"],))
        from budget_app.categorize import upsert_rule
        upsert_rule(db, "LOAN SERVICER", 5)                     # with a clue, the right one is picked...
        res = statement.import_statement(db, csv_text, "checking.csv")
        self.assertEqual(res["history_matched"], 2)          # ...and that settles the other one too
        cats = dict(db.execute("""SELECT t.description, s.category_id FROM transactions t JOIN splits s ON s.transaction_id = t.id
                                  WHERE t.source = 'statement'""").fetchall())
        self.assertEqual(cats, {"LOAN SERVICER": 5, "GIVING CO": 4})

    def test_undo_removes_income(self):
        tmp = tempfile.mkdtemp()
        try:
            app = create_app(data_dir=tmp, testing=True)
            with app.test_client() as c:
                r = c.post("/api/import/statement", data={"file": (io.BytesIO(BANK_CSV.encode()), "Joint-Savings.csv")})
                res = r.get_json()
                self.assertEqual(res["kind"], "bank")
                # No spreadsheet here and the default offset (0): every row is new, paychecks count the same month.
                self.assertEqual(len(c.get("/api/income?month=2026-03").get_json()), 1)
                self.assertGreater(res["imported"], 0)
                c.delete(f"/api/import/batches/{res['batch_id']}")
                self.assertEqual(c.get("/api/income?month=2026-03").get_json(), [])
            db = connect(os.path.join(tmp, "budget.db"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReconcileTests(unittest.TestCase):
    """Statement lines replacing spreadsheet history: every case, totals unchanged, undo restores."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app(data_dir=self.tmp, testing=True)
        self.db = connect(os.path.join(self.tmp, "budget.db"))
        db = self.db
        db.execute("INSERT INTO categories(id, name) VALUES (1, 'Clothes'), (2, 'Groceries'), (3, 'House'), (4, 'Misc')")
        db.execute("INSERT INTO rules(pattern, category_id) VALUES ('MORTGAGE CO', 3)")
        sheet = [("2026-05", 141.08, 1), ("2026-05", 6.50, 2),     # one store charge split over two envelopes
                 ("2026-05", 303.00, 2),                           # a May 31 purchase on the June statement
                 ("2026-05", 1500.17, 3),                          # mortgage typed with a typo
                 ("2026-05", 30.00, 4),                            # two coffee charges entered as one
                 ("2026-05", 40.00, 4)]                            # cash - no statement line
        for m, a, c in sheet:
            cur = db.execute("INSERT INTO transactions(date, description, amount, source, budget_month, reviewed) "
                             "VALUES (?, 'x (spreadsheet)', ?, 'excel', ?, 1)", (m + "-01", a, m))
            db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)", (cur.lastrowid, c, a))
        db.commit()
        self.csv = """Transaction Date,Post Date,Description,Category,Type,Amount
2026-05-31,2026-05-31,BIG GROCER #12,Groceries,Sale,303.00
2026-05-20,2026-05-20,CLOTHING BARN #9,Shopping,Sale,147.58
2026-05-18,2026-05-18,MORTGAGE CO,Home,Sale,1500.79
2026-05-12,2026-05-12,BEAN CAFE,Dining,Sale,12.00
2026-05-10,2026-05-10,BEAN CAFE,Dining,Sale,18.00
2026-05-08,2026-05-08,SHOE PLACE #3,Shopping,Sale,84.10
2026-05-09,2026-05-09,SHOE PLACE #3 TOWN,Shopping,Return,-84.10
2026-05-05,2026-05-05,SURPRISE SHOP,Shopping,Sale,9.99
"""

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reconcile_keeps_totals_and_undo_restores(self):
        before = {e["name"]: e["spent"] for e in month_summary(self.db, "2026-05")["envelopes"]}
        res = statement.import_statement(self.db, self.csv, "card.csv", "Card")
        self.db.commit()
        h = res["history"]
        self.assertEqual((h["exact"], h["near"], h["split"], h["combined"], h["pairs"], h["not_in_sheet"]), (1, 1, 1, 2, 2, 1))
        after = {e["name"]: e["spent"] for e in month_summary(self.db, "2026-05")["envelopes"]}
        self.assertEqual(before, after)                                     # every envelope still matches
        q = lambda d: self.db.execute("""SELECT t.budget_month, t.excluded, GROUP_CONCAT(s.category_id || ':' || s.amount)
                                         FROM transactions t LEFT JOIN splits s ON s.transaction_id = t.id
                                         WHERE t.source = 'statement' AND t.description = ? GROUP BY t.id""", (d,)).fetchone()
        self.assertEqual(q("BIG GROCER #12")[0], "2026-05")
        self.assertEqual(sorted(q("CLOTHING BARN #9")[2].split(",")), ["1:141.08", "2:6.5"])
        self.assertEqual(q("SURPRISE SHOP")[1], 1)                         # not in the sheet -> not budgeted
        diff = self.db.execute("SELECT amount FROM transactions WHERE description = 'Spreadsheet difference'").fetchone()[0]
        self.assertAlmostEqual(diff, -0.62)
        # Cash entry is still there as spreadsheet-only; replaced ones are hidden from the list.
        with self.app.test_client() as c:
            rows = c.get("/api/transactions?month=2026-05").get_json()
            self.assertEqual(sum(1 for r in rows if r["source"] == "excel" and r["description"] == "x (spreadsheet)"), 1)
            c.delete(f"/api/import/batches/{res['batch_id']}")
        self.assertEqual({e["name"]: e["spent"] for e in month_summary(self.db, "2026-05")["envelopes"]}, before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM transactions WHERE source = 'excel' AND excluded = 0").fetchone()[0], 6)


MEIJER_TEXT = """          11/18/26      Fastlane100
    MEIJER SAVINGS
    SPECIALS                      11.39
SAVINGS TOTAL   11.39
GENERAL MERCHANDISE
5209243396     CAKE PAN           4.29  CT
DRUGSTORE
81002713166    BANDAGES           7.99 + T
71928311084    DIAPER PASTE       4.49 +
GROCERY
7490404011     BANANAS            1.28  F
     2.66 lb @   1 lb /     .48
76101004770    AVOCADOS
     2  @  2.59                   5.18  F
70882097452    GR BEEF
     2  @  6.79                  13.58  F
 => 50% off 6.79                 -3.40  F
*71373356251   PUMPKIN
     was      2.19       now      1.49  F
*71575610001   RASPBERRIES
     3  @ 3 / 5.00
     was     11.07       now      5.00  F
71928314559    ROTSSERIE CHCK     6.99
 => 15.00 off                    -5.00
            mPerks # -- ********27
TOTAL
             OH 6.50% Sales Tax    .80
             TOTAL TAX             .80
             TOTAL               42.69
PAYMENTS
  CREDIT CARDS         TENDER    42.69
"""

AMAZON_NEW = """Order ID,Order Date,Ship Date,Product Name,Shipment Item Subtotal,Shipment Item Subtotal Tax,Total Amount,Unit Price,Original Quantity,Order Status,Carrier Name & Tracking Number
111-1,2026-10-29T15:36:18Z,2026-10-30T00:27:33Z,Vitamins,50.90,3.31,27.64,25.95,1,Closed,AMZN_US(TBA1)
111-1,2026-10-29T15:36:18Z,2026-10-30T00:27:33Z,Washer cleaner,50.90,3.31,26.57,24.95,1,Closed,AMZN_US(TBA1)
222-2,2026-09-30T02:34:51Z,2026-10-01T04:39:14Z,Iron,30.99,2.01,17.04,16.00,1,Closed,AMZN_US(TBA2)
222-2,2026-09-30T02:34:51Z,2026-10-01T09:24:23Z,Lotion,9.99,0.65,10.64,9.99,1,Closed,AMZN_US(TBA3)
"""


class ReceiptReadingTests(unittest.TestCase):
    def test_meijer_text_is_read_exactly(self):
        from budget_app.importers.receipt_text import parse
        r = parse(MEIJER_TEXT)
        self.assertIsNotNone(r)
        self.assertEqual((r["purchase_date"], r["tax"], r["total"]), ("2026-11-18", 0.80, 42.69))
        amounts = {i["name"]: (i["amount"], i["taxable"]) for i in r["items"]}
        self.assertEqual(amounts["Gr Beef"], (10.18, False))       # 2 @ 6.79, minus the 50% off line
        self.assertEqual(amounts["Raspberries"], (5.00, False))    # was/now line, unit-price note ignored
        self.assertEqual(amounts["Bandages"], (7.99, True))        # "+ T" health item, taxed
        self.assertEqual(amounts["Rotsserie Chck"], (1.99, False))  # no flag; coupon line folded in
        self.assertEqual(len(r["items"]), 9)
        self.assertIsNone(parse(MEIJER_TEXT.replace("42.69", "50.00")))   # doesn't add up -> AI fallback

    def test_amazon_new_export_uses_charged_amounts_per_shipment(self):
        ships = amazon.parse_shipments([AMAZON_NEW])
        totals = sorted((s["order_id"], s["total"]) for s in ships)
        self.assertEqual(totals, [("111-1", 54.21), ("222-2", 10.64), ("222-2", 17.04)])

    def test_duplicate_upload_is_flagged(self):
        tmp = tempfile.mkdtemp()
        try:
            db = make_db(tmp)
            from budget_app import receipts
            for m in ("MEIJER STORE", "Meijer"):
                db.execute("INSERT INTO receipts(source, merchant, purchase_date, total, status) VALUES ('upload', ?, '2026-10-15', 205.24, 'parsed')", (m,))
            self.assertEqual(receipts.duplicate_of(db, 2), 1)
            db.close()
        finally:
            shutil.rmtree(tmp)


class LocalModelTests(unittest.TestCase):
    """The Ollama path, with the HTTP call replaced by a fake."""

    def setUp(self):
        from budget_app import ai
        from budget_app.db import set_setting
        self.ai = ai
        self.tmp = tempfile.mkdtemp()
        self.db = make_db(self.tmp)
        self.db.execute("INSERT INTO categories(id, name) VALUES (1, 'Groceries'), (2, 'Toiletries')")
        set_setting(self.db, "ai_provider", "ollama")
        set_setting(self.db, "ollama_model", "test-vision")
        self.calls = []
        self.replies = []
        self._orig = ai._ollama_request

        def fake(db, path, body=None, timeout=0):
            self.calls.append(body)
            return {"message": {"content": json.dumps(self.replies.pop(0))}, "done_reason": "stop"}
        ai._ollama_request = fake

    def tearDown(self):
        self.ai._ollama_request = self._orig
        self.db.close()
        shutil.rmtree(self.tmp)

    def receipt(self, items, subtotal):
        return {"merchant": "Store", "purchase_date": "2026-11-14T10:00:00", "subtotal": subtotal, "tax": 0.5,
                "total": subtotal + 0.5, "card_last4": "", "items": [
                    {"name": n, "quantity": 1, "amount": a, "taxable": True, "category_id": 1} for n, a in items]}

    def image(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 80), "white").save(buf, "PNG")
        return buf.getvalue()

    def test_enabled_and_request_shape(self):
        self.assertTrue(self.ai.enabled(self.db))
        self.replies = [self.receipt([("Milk", 3.0), ("Soap", 2.0)], 5.0)]
        res = self.ai.parse_receipt(self.db, data=self.image(), media_type="image/png")
        self.assertEqual(res["purchase_date"], "2026-11-14")  # time stripped
        self.assertEqual(len(self.calls), 1)  # added up, so no careful re-read
        body = self.calls[0]
        self.assertEqual(body["model"], "test-vision")
        self.assertFalse(body["think"])
        self.assertEqual(len(body["messages"][1]["images"]), 1)
        self.assertIn("1: Groceries", body["messages"][1]["content"])
        self.assertEqual(body["format"], self.ai.RECEIPT_SCHEMA)

    def test_rereads_carefully_when_items_dont_add_up(self):
        self.replies = [self.receipt([("Milk", 3.0)], 5.0), self.receipt([("Milk", 3.0), ("Soap", 2.0)], 5.0)]
        res = self.ai.parse_receipt(self.db, text="MILK 3.00\nSOAP 2.00\nSUBTOTAL 5.00")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1]["think"])
        self.assertEqual(len(res["items"]), 2)

    def test_text_pdf_is_sent_as_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import NameObject
        # Minimal one-page PDF with a text layer.
        w = PdfWriter()
        page = w.add_blank_page(300, 300)
        from pypdf.generic import DecodedStreamObject, DictionaryObject
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 200 Td (MEIJER  MILK 3.00  SOAP 2.00  SUBTOTAL 5.00) Tj ET")
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        page[NameObject("/Contents")] = w._add_object(stream)
        buf = io.BytesIO()
        w.write(buf)
        self.replies = [self.receipt([("Milk", 3.0), ("Soap", 2.0)], 5.0)]
        self.ai.parse_receipt(self.db, data=buf.getvalue(), media_type="application/pdf")
        msg = self.calls[0]["messages"][1]
        self.assertNotIn("images", msg)
        self.assertIn("SUBTOTAL 5.00", msg["content"])


def make_email(subject, html, msg_id, sent="Mon, 13 Oct 2026 10:00:00 -0400", sender="Store <orders@example.com>"):
    from email.message import EmailMessage
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"], m["Message-ID"] = subject, sender, "me@example.com", sent, msg_id
    m.set_content("See the HTML version.")
    m.add_alternative(html, subtype="html")
    return m.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for the scanner."""

    def __init__(self, messages):
        self.messages = messages  # {uid: raw bytes}
        self.fetched_bodies = []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, name, readonly=False):
        assert readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        uids, spec = args
        out = []
        for u in uids.split(","):
            raw = self.messages[int(u)]
            if "HEADER.FIELDS" in spec:
                raw = raw.split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0]
            else:
                self.fetched_bodies.append(int(u))
            out += [(f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", out


class EmailScanTests(unittest.TestCase):
    def setUp(self):
        from budget_app import emailscan
        self.es = emailscan
        self.tmp = tempfile.mkdtemp()
        self.db = make_db(self.tmp)
        self.db.execute("INSERT INTO categories(id, name) VALUES (1, 'Groceries'), (2, 'Household')")
        self.db.execute("INSERT INTO email_accounts(id, address, provider, host, search, since) "
                        "VALUES (1, 'me@example.com', 'gmail', 'imap.example.com', 'category:purchases', '2026-10-01')")
        self.db.execute("INSERT INTO transactions(date, description, amount, source, budget_month) "
                        "VALUES ('2026-10-14', 'FRESH MART #12', 25.00, 'statement', '2026-10')")
        self.db.commit()
        self.acct = dict(self.db.execute("SELECT * FROM email_accounts").fetchone())
        self.ai_calls = []
        self._orig = emailscan.ai.parse_receipt

        def fake_parse(db, text=None, email=None, attachments=(), **kw):
            self.ai_calls.append(email["subject"])
            if "Newsletter" in email["subject"]:
                return {"is_receipt": False, "order_number": "", "merchant": "", "purchase_date": "", "subtotal": 0,
                        "tax": 0, "total": 0, "card_last4": "", "items": []}
            assert "Bananas" in text and "25.00" in text
            return {"is_receipt": True, "order_number": "A-12345", "merchant": "Fresh Mart", "purchase_date": "2026-10-13",
                    "subtotal": 24.0, "tax": 1.0, "total": 25.0, "card_last4": "",
                    "items": [{"name": "Bananas", "quantity": 1, "amount": 4.0, "taxable": False, "category_id": 1},
                              {"name": "Dish soap", "quantity": 1, "amount": 20.0, "taxable": True, "category_id": 2}]}
        emailscan.ai.parse_receipt = fake_parse
        receipt_html = ("<html><head><style>.x{}</style></head><body><h1>Thanks for your order A-12345</h1><table>"
                        "<tr><td>Bananas</td><td>$4.00</td></tr><tr><td>Dish soap</td><td>$20.00</td></tr>"
                        "<tr><td>Total</td><td>$25.00</td></tr></table></body></html>")
        self.imap = FakeIMAP({
            1: make_email("Your Fresh Mart order A-12345", receipt_html, "<a@x>"),
            2: make_email("Your package has shipped!", "<p>On its way. $25.00</p>", "<b@x>"),
            3: make_email("Weekly Newsletter: deals", "<p>Steaks $9.99/lb</p>", "<c@x>"),
            4: make_email("Receipt for order A-12345", receipt_html, "<d@x>"),
        })

    def tearDown(self):
        self.es.ai.parse_receipt = self._orig
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_html_to_text(self):
        t = self.es.html_to_text("<style>p{color:red}</style><table><tr><td>Milk</td><td>$3.49</td></tr>"
                                 "<tr><td>Eggs &amp; ham</td><td>$5.00</td></tr></table>")
        self.assertNotIn("color", t)
        self.assertEqual(t.splitlines(), ["Milk | $3.49", "Eggs & ham | $5.00"])

    def test_scan_finds_receipt_matches_charge_and_skips_the_rest(self):
        c = self.es.scan_account(self.db, self.acct, "pw", self.tmp, imap=self.imap)
        self.assertEqual(c["receipts"], 1)
        self.assertEqual(c["duplicates"], 1)   # second email for the same order
        self.assertEqual(c["skipped"], 2)      # shipped (by subject, no AI) + newsletter (AI said no)
        self.assertNotIn("Your package has shipped!", self.ai_calls)
        self.assertNotIn(2, self.imap.fetched_bodies)
        r = self.db.execute("SELECT * FROM receipts").fetchone()
        self.assertEqual((r["source"], r["merchant"], r["total"]), ("email", "Fresh Mart", 25.0))
        t = self.db.execute("SELECT id FROM transactions").fetchone()
        self.assertEqual(r["transaction_id"], t["id"])   # matched to the card charge
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM receipt_items").fetchone()[0], 2)
        # The original email text is kept so the receipt can be re-read.
        self.assertIn("Bananas", json.loads(r["raw_json"])["text"])

        # A second scan reads nothing again.
        n = len(self.ai_calls)
        c2 = self.es.scan_account(self.db, self.acct, "pw", self.tmp, imap=self.imap)
        self.assertEqual(len(self.ai_calls), n)
        self.assertEqual(c2["receipts"] + c2["skipped"] + c2["duplicates"], 0)

    def test_read_again_bypasses_subject_filter(self):
        self.es.scan_account(self.db, self.acct, "pw", self.tmp, imap=self.imap)
        self.db.execute("UPDATE email_seen SET status = 'retry' WHERE message_id = '<b@x>'")
        self.es.scan_account(self.db, self.acct, "pw", self.tmp, imap=self.imap)
        self.assertIn(2, self.imap.fetched_bodies)

    def test_non_gmail_requires_purchase_subject(self):
        acct = dict(self.acct, provider="outlook", search=None)
        self.assertEqual(self.es.screen(acct, {"subject": "Lunch on Friday?"}), "Subject doesn't look like a purchase")
        self.assertIsNone(self.es.screen(acct, {"subject": "Your Target.com order #1234"}))
        self.assertIsNone(self.es.screen(self.acct, {"subject": "Lunch on Friday?"}))  # Gmail search already filtered


class ApiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.app = create_app(data_dir=self.tmp, testing=True)
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_end_to_end(self):
        path = os.path.join(self.tmp, "b.xlsx")
        make_workbook(path)
        with open(path, "rb") as f:
            r = self.c.post("/api/import/excel", data={"file": (f, "b.xlsx")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.c.post("/api/import/statement", data={"file": (io.BytesIO(SOFI.encode()), "s.csv"), "account": "Card"},
                        content_type="multipart/form-data")
        self.assertEqual(r.get_json()["imported"], 7)
        self.assertEqual(self.c.get("/").status_code, 200)
        s = self.c.get("/api/summary?month=2026-10").get_json()
        self.assertEqual(s["uncategorized_count"], 5)   # the Sep 30 coffee counts in September
        txns = self.c.get("/api/transactions?month=2026-10&filter=uncategorized").get_json()
        meijer = next(t for t in txns if "MEIJER" in t["description"])
        food = next(c for c in self.c.get("/api/categories").get_json() if c["name"] == "Food")
        r = self.c.patch(f"/api/transactions/{meijer['id']}",
                         json={"category_id": food["id"], "remember": True, "apply_similar": True})
        self.assertEqual(r.get_json()["applied_to"], 2)
        # split a transaction
        amz = next(t for t in txns if "AMAZON" in t["description"])
        fun = next(c for c in self.c.get("/api/categories").get_json() if c["name"] == "Fun")
        r = self.c.put(f"/api/transactions/{amz['id']}/splits", json={"splits": [
            {"category_id": food["id"], "amount": 4.21}, {"category_id": fun["id"], "amount": 50}]})
        self.assertEqual(r.status_code, 200)
        bad = self.c.put(f"/api/transactions/{amz['id']}/splits", json={"splits": [{"category_id": fun["id"], "amount": 1}]})
        self.assertEqual(bad.status_code, 400)
        # manual receipt (no AI): upload, enter items, apply
        r = self.c.post("/api/receipts", data={"files": (io.BytesIO(b"\x89PNG fake"), "r.png")}, content_type="multipart/form-data")
        rid = r.get_json()[0]["id"]
        self.c.patch(f"/api/receipts/{rid}", json={"merchant": "Meijer", "purchase_date": "2026-10-30", "total": 47.04, "tax": 0.5,
                                                   "items": [{"name": "Apples", "amount": 40, "taxable": False, "category_id": food["id"]},
                                                             {"name": "Soap", "amount": 6.54, "taxable": True, "category_id": fun["id"]}]})
        cands = self.c.get(f"/api/receipts/{rid}").get_json()["candidates"]
        self.assertTrue(cands and cands[0]["amount"] == 47.04)
        r = self.c.post(f"/api/receipts/{rid}/apply", json={"transaction_id": cands[0]["id"]})
        self.assertEqual(r.status_code, 200)
        t = self.c.get("/api/transactions?month=2026-10&filter=split").get_json()
        self.assertEqual(len(t), 2)
        # budget endpoints
        self.c.post("/api/allocations/fill", json={"month": "2026-10", "mode": "defaults"})
        self.c.post("/api/income", json={"month": "2026-10", "amount": 2000})
        s = self.c.get("/api/summary?month=2026-10").get_json()
        self.assertEqual(s["income"], 2000)
        self.assertEqual(s["allocated"], 300)  # Food 200 + Fun 50 + Gifts 50
        rid2 = self.c.post("/api/recurring", json={"name": "Mortgage", "amount": 1000, "category_id": food["id"]}).get_json()["id"]
        self.assertEqual(self.c.post("/api/recurring/add-month", json={"month": "2026-10"}).get_json()["added"], 1)
        self.assertEqual(self.c.post("/api/recurring/add-month", json={"month": "2026-10"}).get_json()["added"], 0)
        self.assertTrue(rid2)
        for url in ("/api/history?end=2026-10", "/api/reports/merchants?end=2026-10", f"/api/reports/category/{food['id']}",
                    "/api/rules", "/api/statement-map", "/api/recurring/suggestions", "/api/settings", "/api/status",
                    "/api/receipts", "/api/import/batches"):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        x = self.c.get("/api/export/year/2026.xlsx")
        self.assertEqual(x.status_code, 200)
        self.assertGreater(len(x.data), 1000)


if __name__ == "__main__":
    unittest.main()
