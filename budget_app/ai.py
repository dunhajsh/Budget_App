"""AI helpers: read receipts, and suggest categories.

Two providers are supported:
- "anthropic": Claude via the Anthropic API (most accurate; needs an API key).
- "ollama": a vision model running locally through Ollama (free; nothing leaves the computer).

Everything here is optional. Without AI the app still works; you just enter
receipt line items and categories yourself.
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.request

from .db import get_setting

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_IMAGE_BYTES = 4_500_000  # API limit is 5 MB per image (base64 adds ~33%, so stay below)


class AIError(Exception):
    pass


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def api_key(db):
    return get_setting(db, "anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")


def provider(db):
    return get_setting(db, "ai_provider") or "anthropic"


def ollama_url(db):
    return (get_setting(db, "ollama_url") or DEFAULT_OLLAMA_URL).rstrip("/")


def ollama_model(db):
    return get_setting(db, "ollama_model") or ""


def enabled(db):
    if provider(db) == "ollama":
        return bool(ollama_model(db))
    return bool(api_key(db))


def describe(db):
    """Short label for the UI, e.g. 'Claude' or 'qwen3.5:4b (local)'."""
    if not enabled(db):
        return None
    return f"{ollama_model(db)} (local)" if provider(db) == "ollama" else "Claude"


def _client(db):
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise AIError("The 'anthropic' package is not installed. Run: pip install -r requirements.txt") from e
    key = api_key(db)
    if not key:
        raise AIError("No Anthropic API key configured. Add one under Settings.")
    return anthropic.Anthropic(api_key=key)


def category_lines(db):
    """'id: Envelope > Subcategory' lines for prompts."""
    rows = db.execute(
        """SELECT c.id, c.name, p.name AS parent FROM categories c
           LEFT JOIN categories p ON p.id = c.parent_id
           WHERE c.archived = 0 AND (p.id IS NULL OR p.archived = 0)
           ORDER BY COALESCE(p.sort_order, c.sort_order), c.parent_id IS NOT NULL, c.sort_order"""
    ).fetchall()
    return "\n".join(f"{r['id']}: {r['parent'] + ' > ' if r['parent'] else ''}{r['name']}" for r in rows)


def _guidance(db):
    notes = (get_setting(db, "categorization_notes") or "").strip()
    rules = db.execute(
        """SELECT r.pattern, c.name, p.name AS parent FROM rules r JOIN categories c ON c.id = r.category_id
           LEFT JOIN categories p ON p.id = c.parent_id ORDER BY r.hits DESC LIMIT 60"""
    ).fetchall()
    parts = []
    if notes:
        parts.append("Household notes about how we budget:\n" + notes)
    if rules:
        parts.append(
            "Merchants we have already categorized (merchant -> category):\n"
            + "\n".join(f"- {r['pattern']} -> {(r['parent'] + ' > ') if r['parent'] else ''}{r['name']}" for r in rules)
        )
    return "\n\n".join(parts)


def _call(db, content, schema, system, effort="medium", max_tokens=16000, think=False):
    if provider(db) == "ollama":
        return _call_ollama(db, content, schema, system, think=think)
    return _call_anthropic(db, content, schema, system, effort, max_tokens)


def _call_anthropic(db, content, schema, system, effort, max_tokens):
    import anthropic

    client = _client(db)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=system,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise AIError("The Anthropic API key was rejected. Check it under Settings.") from e
    except anthropic.PermissionDeniedError as e:
        raise AIError("This API key doesn't have access to the model.") from e
    except anthropic.RateLimitError as e:
        raise AIError("Rate limited by the Anthropic API - wait a minute and try again.") from e
    except anthropic.BadRequestError as e:
        raise AIError(f"The API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic API error ({e.status_code}). Try again shortly.") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Couldn't reach the Anthropic API - check your internet connection.") from e

    if resp.stop_reason == "refusal":
        raise AIError("The model declined to process this file.")
    if resp.stop_reason == "max_tokens":
        raise AIError("The response was cut off (very long receipt). Try splitting it into two photos.")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise AIError("Empty response from the model.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("The model returned malformed JSON.") from e


# ---------------------------------------------------------------- ollama (local)

def _ollama_request(db, path, body=None, timeout=900):
    url = ollama_url(db) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            pass
        if e.code == 404 and "not found" in detail:
            raise AIError(f"Ollama doesn't have the model '{ollama_model(db)}'. Pick another under Settings, "
                          f"or run: ollama pull {ollama_model(db)}") from e
        raise AIError(f"Ollama error ({e.code}): {detail or e.reason}") from e
    except TimeoutError as e:
        raise AIError("The local model took too long. Try a smaller photo or a smaller model.") from e
    except (urllib.error.URLError, ConnectionError) as e:
        raise AIError(f"Couldn't reach Ollama at {ollama_url(db)} - is it running?") from e


def ollama_models(db):
    """Installed models, with whether each can read images."""
    tags = _ollama_request(db, "/api/tags", timeout=10).get("models", [])
    out = []
    for m in tags:
        caps = []
        try:
            caps = _ollama_request(db, "/api/show", {"model": m["name"]}, timeout=10).get("capabilities", [])
        except AIError:
            pass
        out.append({"name": m["name"], "size": m.get("size"), "vision": "vision" in caps})
    return out


def _call_ollama(db, content, schema, system, think=False):
    model = ollama_model(db)
    if not model:
        raise AIError("No local model selected. Pick one under Settings.")
    texts, images = [], []
    for b in content:
        if b["type"] == "text":
            texts.append(b["text"])
        elif b["type"] == "image":
            images.append(b["source"]["data"])
        else:  # PDFs are converted to text/images before they get here
            raise AIError("The local model can't read this file type.")
    msg = {"role": "user", "content": "\n\n".join(texts)}
    if images:
        msg["images"] = images
    body = {
        "model": model,
        "stream": False,
        "format": schema,
        "think": bool(think),
        "options": {"temperature": 0, "num_ctx": 16384},
        "messages": [{"role": "system", "content": system}, msg],
    }
    try:
        res = _ollama_request(db, "/api/chat", body)
    except AIError as e:
        if think and "think" in str(e).lower():  # model has no thinking mode
            body["think"] = False
            res = _ollama_request(db, "/api/chat", body)
        else:
            raise
    if res.get("done_reason") == "length":
        raise AIError("The local model's answer was cut off. Try splitting the receipt into two photos.")
    text = (res.get("message") or {}).get("content", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("The local model returned malformed JSON. Try Re-read.") from e


def _pdf_for_local(data):
    """Ollama can't read PDFs: use the PDF's text if it has some (emailed receipts),
    otherwise the scanned page images inside it."""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise AIError("Reading PDFs with a local model needs pypdf. Run: pip install -r requirements.txt") from e
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:10]).strip()
    if len(text) >= 40:
        return [{"type": "text", "text": "Receipt text:\n\n" + text}]
    blocks = []
    for page in reader.pages[:4]:
        for im in page.images[:2]:
            blocks.append(_image_block(im.data, None, max_side=2048))
    if not blocks:
        raise AIError("Couldn't find text or images in this PDF. Try a screenshot instead.")
    return blocks


# ---------------------------------------------------------------- receipts

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "purchase_date": {"type": "string", "description": "YYYY-MM-DD, or empty string if not shown"},
        "subtotal": {"type": "number"},
        "tax": {"type": "number"},
        "total": {"type": "number", "description": "Amount actually charged, including tax and tip"},
        "card_last4": {"type": "string", "description": "Last 4 digits of the card used, or empty"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Readable product name, expanded from receipt abbreviations"},
                    "quantity": {"type": "number"},
                    "amount": {"type": "number", "description": "Line total after item discounts, before sales tax. Negative for coupons/discounts."},
                    "taxable": {"type": "boolean"},
                    "category_id": {"type": "integer", "description": "Best category id from the list, or 0 if unsure"},
                },
                "required": ["name", "quantity", "amount", "taxable", "category_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["merchant", "purchase_date", "subtotal", "tax", "total", "card_last4", "items"],
    "additionalProperties": False,
}

RECEIPT_SYSTEM = """You read shopping receipts for a household budgeting app and turn them into line items.

Rules:
- One entry per purchased line. Expand cryptic abbreviations into readable product names (e.g. "GV 2% MLK GAL" -> "Great Value 2% milk, gallon").
- `amount` is the price paid for that line BEFORE sales tax, after any discount printed directly under that item. Fold item-level discounts into the item.
- Order-level coupons, mPerks/rewards savings or discounts not tied to one item become their own line with a NEGATIVE amount.
- Bottle deposits, bag fees, delivery fees, service fees and tips are their own lines.
- Do NOT include subtotal, tax, total, payment, change, or balance lines as items.
- `taxable`: true if the receipt marks the line as taxed (often a T/X/TX flag) - when the receipt has no tax flags, mark food as false and non-food as true.
- `total` is what was charged to the card (after tax, tip, and coupons).
- Pick `category_id` from the provided list. Prefer the most specific subcategory that fits. Food eaten at home goes to the grocery envelope; toiletries, cleaning supplies, clothing, gifts, etc. go to their own envelopes even when bought at a grocery store. Use 0 only if nothing fits.
- If something is unreadable, give your best reading rather than skipping it, so the items still add up to the subtotal."""


def _image_block(data, media_type, max_side=3000):
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    # Keep plenty of resolution for small receipt text, but stay under the API size cap.
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    for quality in (88, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
        img.thumbnail((int(img.size[0] * 0.8), int(img.size[1] * 0.8)))
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(buf.getvalue()).decode()},
    }


def _looks_right(res):
    """Sanity checks for a quick local read: do the line items add up to the subtotal,
    and is the tax plausible for the items marked taxable (US rates are under ~11%)?"""
    items = res.get("items", [])
    total = round(sum(float(i.get("amount") or 0) for i in items), 2)
    targets = [res.get("subtotal") or 0, (res.get("total") or 0) - (res.get("tax") or 0)]
    if not any(t and abs(total - t) <= 0.05 for t in targets):
        return False
    tax = float(res.get("tax") or 0)
    if tax > 0.05:
        taxable = sum(float(i.get("amount") or 0) for i in items if i.get("taxable"))
        if taxable <= 0 or tax / taxable > 0.11:
            return False
    return True


def _clean_receipt(res):
    m = re.search(r"\d{4}-\d{2}-\d{2}", res.get("purchase_date") or "")
    res["purchase_date"] = m.group(0) if m else ""
    return res


EMAIL_SCHEMA = {
    **RECEIPT_SCHEMA,
    "properties": {
        "is_receipt": {"type": "boolean", "description": "True only if this email confirms a purchase with prices"},
        "order_number": {"type": "string", "description": "Order/confirmation number, or empty"},
        **RECEIPT_SCHEMA["properties"],
    },
    "required": ["is_receipt", "order_number"] + RECEIPT_SCHEMA["required"],
}

EMAIL_SYSTEM = RECEIPT_SYSTEM + """

You are reading an email (and any attachments). First decide `is_receipt`:
- true for order confirmations, digital receipts, invoices and payment receipts that list what was bought and what it cost.
- false for shipping/delivery updates without prices, ads, sales and coupons, newsletters, account notices, surveys, and refunds.
When `is_receipt` is false, return empty strings, zeros and an empty item list.
For an order with several items, the `total` is the order total that was charged."""


def _file_blocks(data, media_type, local):
    if media_type == "application/pdf":
        if local:
            return _pdf_for_local(data)
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(data).decode()}}]
    # Local vision models get a smaller image: faster, and it fits in a laptop GPU.
    return [_image_block(data, media_type, max_side=2048 if local else 3000)]


def parse_receipt(db, data=None, media_type=None, text=None, email=None, attachments=()):
    """Read one receipt. `email` (dict with subject/sender/date) switches to email mode, where the
    model may also say the message isn't a receipt; `attachments` is a list of (bytes, media_type)."""
    local = provider(db) == "ollama"
    content = []
    if email:
        head = f"Email from: {email.get('sender', '')}\nSubject: {email.get('subject', '')}\nDate: {email.get('date', '')}"
        content.append({"type": "text", "text": head + "\n\n" + (text or "(no text body)")})
    elif text:
        content.append({"type": "text", "text": "Receipt text:\n\n" + text})
    if data is not None:
        content.extend(_file_blocks(data, media_type, local))
    for adata, amt in attachments:
        content.extend(_file_blocks(adata, amt, local))
    prompt = "Categories (id: name):\n" + category_lines(db)
    g = _guidance(db)
    if g:
        prompt += "\n\n" + g
    prompt += "\n\nRead this email." if email else "\n\nExtract this receipt."
    content.append({"type": "text", "text": prompt})
    schema, system = (EMAIL_SCHEMA, EMAIL_SYSTEM) if email else (RECEIPT_SCHEMA, RECEIPT_SYSTEM)

    if not local:
        return _clean_receipt(_call(db, content, schema, system, effort="medium"))

    # Small local models: a quick pass first; if the items or tax don't add up, read it again
    # carefully with thinking on (slower, but catches missed lines like coupons).
    mode = get_setting(db, "ollama_think") or "auto"
    res = _call(db, content, schema, system, think=(mode == "always"))
    if mode == "auto" and res.get("is_receipt", True) and not _looks_right(res):
        try:
            res = _call(db, content, schema, system, think=True)
        except AIError:
            pass  # the careful read ran too long (very long receipt): keep the quick read; totals are flagged for review
    return _clean_receipt(res)


# ---------------------------------------------------------------- categorization

ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "category_id": {"type": "integer"}},
                "required": ["index", "category_id"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

CATEGORIZE_SYSTEM = """You categorize spending for a household envelope budget.
For each numbered entry, choose the single best category id from the provided list (prefer the most specific subcategory that fits).
Use 0 only when you genuinely can't tell. Return one result per entry, using the entry's index."""


def categorize(db, entries, kind="card transactions"):
    """entries: list of strings. Returns {index: category_id} (only non-zero, valid ids)."""
    if not entries:
        return {}
    valid = {r["id"] for r in db.execute("SELECT id FROM categories WHERE archived = 0")}
    out = {}
    chunk = 40 if provider(db) == "ollama" else 150  # small local models do better with short lists
    for start in range(0, len(entries), chunk):
        part = entries[start:start + chunk]
        listing = "\n".join(f"{start + i}. {e}" for i, e in enumerate(part))
        prompt = "Categories (id: name):\n" + category_lines(db)
        g = _guidance(db)
        if g:
            prompt += "\n\n" + g
        prompt += f"\n\nCategorize these {kind}:\n{listing}"
        res = _call(db, [{"type": "text", "text": prompt}], ITEMS_SCHEMA, CATEGORIZE_SYSTEM, effort="low")
        for r in res.get("results", []):
            if r.get("category_id") in valid:
                out[int(r["index"])] = r["category_id"]
    return out
