"""Remember where you put receipt items, and file them the same way next time.

Learning happens only from your own choices: whenever you save a receipt, each item's
category is remembered under a normalized version of its name. AI guesses are never
remembered. When a new receipt comes in (Meijer PDF, photo, Amazon order, email), items
you've categorized before get your category - it wins over any AI guess - and only the
rest are left to the AI.

Names are normalized so the same product matches across receipts: case, punctuation,
sizes and counts are ignored ("BANANAS 2.3 LB" = "Bananas"), and long product titles
(Amazon) are matched on their first words.
"""
import re

UNITS = r"(oz|fl|lb|lbs|ct|count|pk|pack|pc|pcs|g|kg|ml|l|ltr|gal|qt|pt|in|ft|inch|x)"
MAX_WORDS = 8


def key(name):
    s = (name or "").lower()
    s = re.sub(r"[®™©]", "", s)
    s = re.sub(rf"\b\d+([.,/]\d+)?\s*{UNITS}\b", " ", s)   # sizes: 2.3 lb, 42ct, 6 pack
    s = re.sub(r"\b\d+([.,/]\d+)?\b", " ", s)               # other numbers
    s = re.sub(r"[^a-z& ]+", " ", s)
    words = [w for w in s.split() if len(w) > 1 or w == "&"]
    return " ".join(words[:MAX_WORDS])


def lookup(db, names):
    """{name: category_id} for names you've categorized before."""
    out = {}
    for n in set(names):
        k = key(n)
        if not k:
            continue
        r = db.execute("""SELECT m.category_id FROM item_memory m JOIN categories c ON c.id = m.category_id
                          WHERE m.key = ? AND c.archived = 0""", (k,)).fetchone()
        if r:
            out[n] = r["category_id"]
    return out


def apply(db, items):
    """Give items their remembered category (overriding AI guesses). Marks where each category came from:
    item["cat_source"] = "memory" | "ai" | None. Returns how many came from memory."""
    known = lookup(db, [i.get("name") or "" for i in items])
    n = 0
    for i in items:
        cid = known.get(i.get("name") or "")
        if cid:
            i["category_id"] = cid
            i["cat_source"] = "memory"
            n += 1
        elif i.get("category_id") and not i.get("cat_source"):
            i["cat_source"] = "ai"
    return n


def learn(db, items, merchant=None):
    """Remember the categories of items you've saved. Returns the keys that were new or changed."""
    changed = []
    for i in items:
        cid, k = i.get("category_id"), key(i.get("name"))
        if not cid or not k:
            continue
        old = db.execute("SELECT category_id FROM item_memory WHERE key = ?", (k,)).fetchone()
        if old and old["category_id"] == cid:
            db.execute("UPDATE item_memory SET times = times + 1, updated_at = datetime('now') WHERE key = ?", (k,))
            continue
        db.execute(
            """INSERT INTO item_memory(key, name, category_id, merchant, times) VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(key) DO UPDATE SET name = excluded.name, category_id = excluded.category_id,
                   merchant = COALESCE(excluded.merchant, item_memory.merchant), times = item_memory.times + 1,
                   updated_at = datetime('now')""",
            (k, (i.get("name") or "").strip()[:200], cid, merchant))
        changed.append(k)
    return changed


def update_open_receipts(db, keys, exclude_receipt=None):
    """After you teach an item, re-file the same item on receipts you haven't reviewed yet."""
    if not keys:
        return 0
    keys = set(keys)
    n = 0
    for it in db.execute(
        """SELECT i.id, i.name, i.category_id FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id
           WHERE r.status IN ('parsed', 'error', 'duplicate') AND r.id != ?""", (exclude_receipt or -1,)).fetchall():
        if key(it["name"]) in keys:
            cid = db.execute("SELECT category_id FROM item_memory WHERE key = ?", (key(it["name"]),)).fetchone()["category_id"]
            if cid != it["category_id"]:
                db.execute("UPDATE receipt_items SET category_id = ?, cat_source = 'memory' WHERE id = ?", (cid, it["id"]))
                n += 1
    return n


def seed_from_reviewed(db):
    """Start the memory from receipts you've already saved or applied."""
    for r in db.execute("SELECT id, merchant FROM receipts WHERE status IN ('reviewed', 'applied')").fetchall():
        items = [dict(i) for i in db.execute("SELECT name, category_id FROM receipt_items WHERE receipt_id = ?", (r["id"],))]
        learn(db, items, r["merchant"])
