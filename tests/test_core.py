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
        self.assertAlmostEqual(spent["Eating Out"], 5.25)  # Sep 30 charge counts toward the October statement
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
        self.assertEqual(s["uncategorized_count"], 6)
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
