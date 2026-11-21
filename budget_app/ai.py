"""Claude-powered helpers: read receipts, and suggest categories.

Everything here is optional. Without an API key the app still works; you just
enter receipt line items and categories yourself.
"""
import base64
import io
import json
import os

from .db import get_setting

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_IMAGE_BYTES = 4_500_000  # API limit is 5 MB per image (base64 adds ~33%, so stay below)


class AIError(Exception):
    pass


def api_key(db):
    return get_setting(db, "anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")


def enabled(db):
    return bool(api_key(db))


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


def _call(db, content, schema, system, effort="medium", max_tokens=16000):
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


def _image_block(data, media_type):
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    # Keep plenty of resolution for small receipt text, but stay under the API size cap.
    max_side = 3000
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


def parse_receipt(db, data=None, media_type=None, text=None):
    content = []
    if text:
        content.append({"type": "text", "text": "Receipt text:\n\n" + text})
    elif media_type == "application/pdf":
        content.append(
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                             "data": base64.standard_b64encode(data).decode()}}
        )
    else:
        content.append(_image_block(data, media_type))
    prompt = "Categories (id: name):\n" + category_lines(db)
    g = _guidance(db)
    if g:
        prompt += "\n\n" + g
    prompt += "\n\nExtract this receipt."
    content.append({"type": "text", "text": prompt})
    return _call(db, content, RECEIPT_SCHEMA, RECEIPT_SYSTEM, effort="medium")


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
    chunk = 150
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
