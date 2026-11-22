"""Import Amazon order history so each Amazon card charge can be split by item.

Get the file from amazon.com -> Account -> "Request Your Data" -> "Your Orders".
Amazon emails a zip; upload the zip itself or the Retail.OrderHistory.*.csv inside it.
Third-party exports (e.g. the "Amazon Order History Reporter" browser extension)
also work as long as they have an order id, a date, an item name and an amount.

Amazon charges the card once per *shipment*, so items are grouped by
(order id, ship date) and each group is matched to a card charge of the same amount.
"""
import csv
import io
import zipfile
from collections import OrderedDict
from datetime import date, datetime, timedelta

from .. import ai, receipts


def _find(headers, *options):
    low = {h.lower().strip(): h for h in headers}
    for o in options:
        if o in low:
            return low[o]
    for o in options:  # loose contains-match as a fallback
        for k, h in low.items():
            if o in k:
                return h
    return None


def _find_exact(headers, *options):
    low = {h.lower().strip(): h for h in headers}
    return next((low[o] for o in options if o in low), None)


def _money(s):
    s = (str(s or "")).replace("$", "").replace(",", "").replace("'", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _date(s):
    s = (s or "").strip()
    if not s or s.lower().startswith("not"):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def read_files(filename, data):
    """Return list of CSV texts from an upload (zip or csv)."""
    if filename.lower().endswith(".zip"):
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                key = n.lower().replace(".", "").replace("_", "").replace(" ", "")
                # "Retail.OrderHistory.1.csv" (older exports) or "Your Amazon Orders/Order History.csv" (newer)
                if n.lower().endswith(".csv") and "orderhistory" in key and "digital" not in key:
                    out.append(z.read(n).decode("utf-8-sig", errors="replace"))
        if not out:
            raise ValueError("No order history CSV found in that zip (looked for 'Order History.csv' / 'Retail.OrderHistory').")
        return out
    return [data.decode("utf-8-sig", errors="replace")]


def parse_shipments(texts, since=None):
    shipments = OrderedDict()
    for text in texts:
        reader = csv.DictReader(io.StringIO(text))
        h = reader.fieldnames or []
        c_order = _find(h, "order id", "order number")
        c_odate = _find(h, "order date", "date")
        c_sdate = _find(h, "ship date", "shipment date")
        c_name = _find(h, "product name", "title", "description", "item")
        # What was actually charged per item (tax and discounts included). Never the "Shipment Item
        # Subtotal" column: that's the pre-tax subtotal of the whole shipment, repeated on every item row.
        c_amt = _find_exact(h, "total owed", "total amount", "item total") or _find(
            [x for x in h if not any(w in x.lower() for w in ("subtotal", "tax", "discount", "shipping"))],
            "total", "amount", "price")
        c_qty = _find(h, "quantity", "qty")
        c_status = _find(h, "order status", "status")
        c_track = _find_exact(h, "carrier name & tracking number", "tracking number")
        if not (c_order and c_odate and c_name and c_amt):
            raise ValueError("Couldn't recognize the Amazon export columns: " + ", ".join(h))
        for row in reader:
            status = (row.get(c_status) or "").lower() if c_status else ""
            if "cancel" in status:
                continue
            odate = _date(row.get(c_odate))
            sdate = _date(row.get(c_sdate)) if c_sdate else None
            d = sdate or odate
            if not d or (since and d < since):
                continue
            amt = _money(row.get(c_amt))
            if amt is None:
                continue
            # One card charge per shipment: group by tracking number when the export has it.
            track = (row.get(c_track) or "").strip() if c_track else ""
            track = "" if track.lower().startswith("not ") else track
            key = (row[c_order].strip(), d.isoformat(), track)
            s = shipments.setdefault(key, {"order_id": key[0], "date": d, "order_date": odate, "items": [], "tracking": track})
            qty = _money(row.get(c_qty)) if c_qty else None
            s["items"].append({"name": row[c_name].strip()[:200], "quantity": qty or 1, "amount": amt})
    for s in shipments.values():
        s["total"] = round(sum(i["amount"] for i in s["items"]), 2)
    return [s for s in shipments.values() if s["total"] > 0]


def import_orders(db, filename, data, since=None, use_ai=True):
    shipments = parse_shipments(read_files(filename, data), since=since)
    created, skipped, rids = 0, 0, []
    for s in shipments:
        ext = f"amazon|{s['order_id']}|{s['date'].isoformat()}" + (f"|{s['tracking']}" if s.get("tracking") else "")
        if db.execute("SELECT 1 FROM receipts WHERE external_id = ?", (ext,)).fetchone():
            skipped += 1
            continue
        cur = db.execute(
            """INSERT INTO receipts(source, external_id, filename, merchant, purchase_date, subtotal, tax, total, status)
               VALUES ('amazon', ?, ?, 'Amazon', ?, ?, 0, ?, 'parsed')""",
            (ext, f"Order {s['order_id']}", s["date"].isoformat(), s["total"], s["total"]),
        )
        rid = cur.lastrowid
        receipts.save_items(db, rid, [dict(i, taxable=False) for i in s["items"]])
        rids.append(rid)
        created += 1

    # Categorize all new items in one pass
    categorized = 0
    ai_error = None
    if rids and use_ai and ai.enabled(db):
        items = db.execute(
            f"SELECT id, name, amount FROM receipt_items WHERE receipt_id IN ({','.join('?' * len(rids))})", rids
        ).fetchall()
        try:
            res = ai.categorize(db, [f"{i['name']} (${i['amount']:.2f})" for i in items], kind="Amazon order items")
            for idx, cid in res.items():
                db.execute("UPDATE receipt_items SET category_id = ? WHERE id = ?", (cid, items[idx]["id"]))
                categorized += 1
        except ai.AIError as e:
            ai_error = str(e)

    matched = match_amazon(db, rids)
    return {"shipments": created, "already_imported": skipped, "matched": matched,
            "items_categorized": categorized, "ai_error": ai_error}


def match_amazon(db, rids=None):
    """Pair Amazon receipts with Amazon card charges (exact amount, nearby date)."""
    q = "SELECT * FROM receipts WHERE source = 'amazon' AND transaction_id IS NULL"
    args = []
    if rids:
        q += f" AND id IN ({','.join('?' * len(rids))})"
        args = rids
    pending = [dict(r) for r in db.execute(q, args)]
    used = {r["transaction_id"] for r in db.execute("SELECT transaction_id FROM receipts WHERE transaction_id IS NOT NULL")}
    last_sheet = db.execute("SELECT MAX(budget_month) FROM transactions WHERE source = 'excel'").fetchone()[0]
    matched = 0
    for r in sorted(pending, key=lambda r: r["purchase_date"]):
        d = date.fromisoformat(r["purchase_date"])
        cands = db.execute(
            """SELECT * FROM transactions WHERE excluded = 0 AND source = 'statement'
                 AND (UPPER(description) LIKE '%AMAZON%' OR UPPER(description) LIKE '%AMZN%')
                 AND ABS(amount - ?) < 0.015 AND date BETWEEN ? AND ?
               ORDER BY ABS(julianday(date) - julianday(?))""",
            (r["total"], (d - timedelta(days=3)).isoformat(), (d + timedelta(days=10)).isoformat(), d.isoformat()),
        ).fetchall()
        cands = [c for c in cands if c["id"] not in used]
        if not cands:
            continue
        t = cands[0]
        used.add(t["id"])
        items = db.execute("SELECT category_id FROM receipt_items WHERE receipt_id = ?", (r["id"],)).fetchall()
        in_sheet_month = t["budget_month"] <= (last_sheet or "")
        # Split the charge automatically only if you haven't reviewed it yet, and never in spreadsheet months
        # (those must keep matching the sheet). Otherwise just link it so you can apply it from Receipts.
        if items and all(i["category_id"] for i in items) and not in_sheet_month and not t["reviewed"]:
            receipts.apply(db, r["id"], t["id"])
        else:
            db.execute("UPDATE receipts SET transaction_id = ? WHERE id = ?", (t["id"], r["id"]))
        matched += 1
    return matched
