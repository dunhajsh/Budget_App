"""Read digital receipts that come as text (PDF text layer), without AI.

Store receipts in a known layout are read exactly and instantly; only categorizing the
items is left to the AI (or to item names categorized before). Anything that doesn't add
up returns None, and the caller falls back to AI reading.

Supported: Meijer digital receipts (mPerks / meijer.com PDFs), including Meijer gas.
"""
import io
import re
from datetime import datetime

# price, then the tax flag (F food, T taxed, FT, CT, N...), sometimes after "+" (health item)
PRICE_END = re.compile(r"\s(-?\d[\d,]*\.\d\d|-?\.\d\d)(?:\s+\+?\s*([A-Z]{1,2})?)?\s*$")
MONEY = re.compile(r"-?\d[\d,]*\.\d\d|-?\.\d\d")
ITEM = re.compile(r"^[*+]*\s*(\d{4,14})\s+(.+?)\s*$")
DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
DEPARTMENTS = {"GROCERY", "PRODUCE", "DRUGSTORE", "GENERAL MERCHANDISE", "FUEL", "PHARMACY", "BAKERY", "DELI",
               "MEAT", "DAIRY", "FROZEN", "PET", "HOME", "APPAREL", "BEAUTY", "HEALTH", "BABY", "SEASONAL", "FLORAL"}


def pdf_text(data):
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:10])
    except Exception:
        return ""


def _money(s):
    s = s.replace(",", "")
    return float(s if not s.startswith(("-.", ".")) else s.replace(".", "0.", 1))


def parse(text):
    """Return a receipt dict (same shape as the AI's) or None if the layout isn't recognized."""
    if "MEIJER" in text.upper() or "MPERKS" in text.upper():
        return parse_meijer(text)
    return None


def parse_meijer(text):
    lines = [ln.rstrip() for ln in text.splitlines()]
    items, dept, pending, started = [], None, None, False
    tax = total = None
    purchase_date = ""
    m = DATE.search(text)
    if m:
        mo, d, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        try:
            purchase_date = datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass

    def add(name, amount, flag):
        items.append({"name": _nice(name), "quantity": 1, "amount": round(amount, 2),
                      "taxable": "T" in flag, "category_id": 0, "department": dept})

    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        up = s.upper()
        if up in DEPARTMENTS or (up.isalpha() and up in DEPARTMENTS):
            dept, started = up, True
            continue
        if not started:
            continue  # savings summary at the top
        if re.match(r"^TOTAL\s+TAX\s", up):
            tax = _money(s.split()[-1])
            continue
        mt = re.match(r"^TOTAL\s+(-?[\d,]*\.\d\d)$", up)
        if mt:
            total = _money(mt.group(1))
            break
        if up == "TOTAL" or up.startswith(("PAYMENTS", "NUMBER OF ITEMS")):
            continue
        ms = re.match(r"^YOUR TOTAL SAVINGS\s+(-[\d,]*\.\d\d)$", up)
        if ms and items:  # e.g. fuel rewards taken off the gas purchase
            items[-1]["amount"] = round(items[-1]["amount"] + _money(ms.group(1)), 2)
            continue
        pf = PRICE_END.search(raw)
        flag = (pf.group(2) or "") if pf else ""
        it = ITEM.match(s)
        if s.startswith("=>"):  # discount or coupon on the previous item
            if pf and items:
                items[-1]["amount"] = round(items[-1]["amount"] + _money(pf.group(1)), 2)
            continue
        if it and not re.match(r"^\d+(\.\d+)?\s*(LB\s*)?@", up):
            name = it.group(2)
            if pf and re.search(r"\S\s{2,}" + re.escape(pf.group(1).strip()), name + " " * 2 + pf.group(1)):
                name = re.sub(r"\s{2,}(-?[\d,]*\.\d\d).*$", "", name).strip()
                add(name, _money(pf.group(1)), flag)
                pending = None
            else:
                pending = name
            continue
        if "@" in up:
            # "2 @ 2.59   5.18 F" carries the line total; "0.70 lb @ 1 lb / 2.99" and "1 @ 3 / 5.00"
            # are unit-price notes; "2 @ .79" alone is a note too (the total follows on a was/now line)
            nums = MONEY.findall(s)
            if dept == "FUEL" and re.search(r"\bG\s*@", up) and pf:
                add("Gasoline", _money(pf.group(1)), flag)
            elif "/" not in s and len(nums) >= 2 and pending:
                add(pending, _money(nums[-1]), flag)
                pending = None
            continue
        if pf and pending:  # "was 2.19  now 1.49 F" or a plain price line finishing the item above
            add(pending, _money(pf.group(1)), flag)
            pending = None
            continue
        if dept == "FUEL" and re.fullmatch(r"[A-Z ]{2,12}", up):
            pending = None  # grade name (UNL, PREM); the "G @" line carries the price
            continue

    if total is None or not items:
        return None
    tax = tax or 0.0
    subtotal = round(sum(i["amount"] for i in items), 2)
    if abs(subtotal + tax - total) > 0.02:
        return None  # didn't read it fully: let the AI try
    return {"merchant": "Meijer", "purchase_date": purchase_date, "subtotal": subtotal, "tax": round(tax, 2),
            "total": round(total, 2), "card_last4": "", "items": items, "reader": "meijer"}


def _nice(name):
    name = re.sub(r"\s{2,}", " ", name).strip(" *+")
    return name.title() if name.isupper() else name
