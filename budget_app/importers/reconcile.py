"""Replace spreadsheet history with statement lines, without changing any totals.

For months the imported spreadsheet already covers, each statement line takes the place
of the spreadsheet entry (or entries) it corresponds to, and takes its envelope from them:

1. exact      one line = one entry with the same amount (an existing rule or the card's own
              category picks between same-amount entries in different envelopes)
2. near       one line ~ one entry (e.g. a typo: 2307.79 vs 2307.17); the difference is kept
              as a visible "spreadsheet difference" entry
3. split      one line = 2-3 entries (one purchase spread over several envelopes)
4. combined   2-3 lines from the same merchant = 1 entry
5. pairs      a purchase and its return that cancel out (spreadsheets usually skip these)

Lines that still don't correspond to anything are added as "not budgeted" so they're visible
but don't change the month. Spreadsheet entries without a statement line (cash, hand-entered
amounts, moves between envelopes) stay as they are.

Replaced spreadsheet entries are kept (excluded, `replaced_by` = batch) so an undo restores
them. Before committing, every month/envelope total is compared with what it was; if anything
differs by a cent, all changes are rolled back.
"""
import itertools
from collections import defaultdict
from datetime import date

from ..budget import month_add
from ..categorize import find_category, merchant_key

NOT_IN_SHEET = ("Not in your spreadsheet for this month, so it's left out of the budget to keep the month "
                "matching. Untick 'Not part of the budget' to count it.")


class ReconcileError(Exception):
    pass


def _envelope(db, cid, cache={}):
    if cid is None:
        return None
    if cid not in cache:
        r = db.execute("SELECT id, parent_id FROM categories WHERE id = ?", (cid,)).fetchone()
        cache[cid] = (r["parent_id"] or r["id"]) if r else None
    return cache[cid]


def _totals(db, months):
    q = ",".join("?" * len(months))
    return {(r[0], r[1]): round(r[2], 2) for r in db.execute(
        f"""SELECT t.budget_month, s.category_id, SUM(s.amount) FROM transactions t JOIN splits s ON s.transaction_id = t.id
            WHERE t.excluded = 0 AND t.budget_month IN ({q}) GROUP BY 1, 2""", list(months))}


def _candidate_months(line):
    """The purchase month first; near a month boundary, also the neighbouring month."""
    m = line["date"][:7]
    d = date.fromisoformat(line["date"])
    out = [m]
    if d.day >= 28:
        out.append(month_add(m, 1))
    if d.day <= 3:
        out.append(month_add(m, -1))
    return out


def _same_merchant(a, b):
    ka, kb = merchant_key(a).split(), merchant_key(b).split()
    return bool(ka and kb) and ka[:2] == kb[:2]


def reconcile(db, lines, account, batch_id, rules, stmt_map):
    """lines: parsed statement rows (with 'month' and 'hash') that fall in spreadsheet months.
    Returns counts. Raises ReconcileError (after rolling back) if totals would change."""
    months = sorted({m for ln in lines for m in _candidate_months(ln)} | {ln["month"] for ln in lines})
    before = _totals(db, months)
    db.execute("SAVEPOINT reconcile")
    try:
        counts = _do(db, lines, account, batch_id, rules, stmt_map, months)
        after = _totals(db, months)
        keys = set(before) | set(after)
        bad = [(k, before.get(k, 0), after.get(k, 0)) for k in keys if abs(before.get(k, 0) - after.get(k, 0)) > 0.005]
        if bad:
            raise ReconcileError(f"Totals would change for {len(bad)} month/envelope(s), e.g. {bad[0]}")
    except Exception:
        db.execute("ROLLBACK TO reconcile")
        db.execute("RELEASE reconcile")
        raise
    db.execute("RELEASE reconcile")
    return counts


def _do(db, lines, account, batch_id, rules, stmt_map, months):
    q = ",".join("?" * len(months))
    pool = [dict(r) for r in db.execute(
        f"""SELECT t.id, t.budget_month AS month, t.amount, t.description, s.category_id
            FROM transactions t JOIN splits s ON s.transaction_id = t.id
            WHERE t.source = 'excel' AND t.excluded = 0 AND t.replaced_by IS NULL AND t.budget_month IN ({q})
              AND (SELECT COUNT(*) FROM splits x WHERE x.transaction_id = t.id) = 1""", months)]
    for e in pool:
        e["env"] = _envelope(db, e["category_id"])
        e["used"] = False

    todo = []
    counts = defaultdict(int)
    for ln in sorted(lines, key=lambda x: (x["date"], x["description"])):
        if ln["excluded"]:  # transfers / card payments: record them, they're never budgeted
            _insert(db, ln, account, batch_id, ln["month"], [], excluded=True)
            counts["left_out"] += 1
            continue
        hint, _ = find_category(db, ln["description"], ln["statement_category"], rules=rules, stmt_map=stmt_map)
        ln["hint_env"] = _envelope(db, hint)
        ln["hint"] = hint
        ln["done"] = False
        todo.append(ln)

    def replace(entries, month, line_splits):
        for e in entries:
            e["used"] = True
            db.execute("UPDATE transactions SET excluded = 1, replaced_by = ? WHERE id = ?", (batch_id, e["id"]))

    def free(month):
        return [e for e in pool if not e["used"] and e["month"] == month]

    # 1. exact. First every line against its own month only, then neighbouring months for what's left
    #    (otherwise a payment on the 2nd could take the previous month's entry before that month's own
    #    payment does). Repeat each stage: lines with a clear choice settle the ambiguous ones.
    for stage in (lambda ln: _candidate_months(ln)[:1], _candidate_months):
      progress = True
      while progress:
        progress = False
        for ln in todo:
            if ln["done"]:
                continue
            for m in stage(ln):
                cands = [e for e in free(m) if abs(e["amount"] - ln["amount"]) < 0.005]
                if not cands:
                    continue
                pick = None
                if len({e["env"] for e in cands}) == 1:
                    pick = sorted(cands, key=lambda e: (e["description"] != ln["description"], e["id"]))[0]
                else:
                    named = [e for e in cands if e["description"] == ln["description"]]
                    hinted = [e for e in cands if ln["hint_env"] and e["env"] == ln["hint_env"]]
                    pick = (named or hinted or [None])[0]
                if pick:
                    replace([pick], m, None)
                    _insert(db, ln, account, batch_id, m, [(pick["category_id"], ln["amount"])])
                    ln["done"] = True
                    counts["exact"] += 1
                    progress = True
                break  # only fall back to a neighbouring month when the purchase month has no candidate

    open_lines = lambda: [ln for ln in todo if not ln["done"]]
    taken_amounts = lambda: {round(ln["amount"], 2) for ln in open_lines()}

    # 2. near match (typo in the spreadsheet): within $1/1% in the envelope the merchant's rule points to,
    #    or within 25 cents/0.5% when it's the only entry that close. Own month first, then neighbours.
    def near(ln, m):
        close = lambda e, d, pct: e["amount"] * ln["amount"] > 0 and abs(e["amount"] - ln["amount"]) <= min(d, abs(ln["amount"]) * pct)
        c = [e for e in free(m) if ln["hint_env"] and e["env"] == ln["hint_env"] and close(e, 1.0, 0.01)]
        if not c and abs(ln["amount"]) >= 5:
            # without a clue from the merchant, never treat a round amount (allowance, transfer) as a typo
            c = [e for e in free(m) if close(e, 0.25, 0.005) and round(e["amount"], 2) != round(e["amount"])]
        return c
    for stage in (lambda ln: _candidate_months(ln)[:1], lambda ln: _candidate_months(ln)[1:]):
      for ln in open_lines():
        for m in stage(ln):
            cands = near(ln, m)
            if len(cands) == 1:
                e = cands[0]
                replace([e], m, None)
                _insert(db, ln, account, batch_id, m, [(e["category_id"], ln["amount"])])
                diff = round(e["amount"] - ln["amount"], 2)
                cur = db.execute(
                    """INSERT INTO transactions(date, description, merchant, amount, account, source, budget_month,
                           excluded, reviewed, notes, batch_id) VALUES (?, ?, ?, ?, 'Spreadsheet', 'excel', ?, 0, 1, ?, ?)""",
                    (ln["date"], "Spreadsheet difference", "SPREADSHEET DIFFERENCE", diff, m,
                     f"The spreadsheet had {e['amount']:.2f} for {ln['description']}; the statement says {ln['amount']:.2f}. "
                     "Kept so the month still matches the spreadsheet - delete it to use the statement amount.", batch_id))
                db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)", (cur.lastrowid, e["category_id"], diff))
                ln["done"] = True
                counts["near"] += 1
                break

    # 3. split: one card purchase = 2-3 entries in the same month (unique answer only; bank payments
    #    are bills and aren't split, which also avoids coincidental sums)
    for ln in sorted([x for x in open_lines() if x.get("kind") == "card"], key=lambda x: -abs(x["amount"])):
        m = ln["date"][:7]
        avoid = taken_amounts() - {round(ln["amount"], 2)}
        ents = [e for e in free(m) if e["amount"] * ln["amount"] > 0 and round(e["amount"], 2) not in avoid]
        sols = []
        for n in (2, 3):
            for combo in itertools.combinations(ents, n):
                if abs(sum(e["amount"] for e in combo) - ln["amount"]) < 0.005:
                    sig = tuple(sorted((e["category_id"], round(e["amount"], 2)) for e in combo))
                    if sig not in [s[0] for s in sols]:
                        sols.append((sig, combo))
                if len(sols) > 1:
                    break
            if sols:
                break
        if len(sols) == 1:
            combo = sols[0][1]
            replace(combo, m, None)
            _insert(db, ln, account, batch_id, m, [(e["category_id"], e["amount"]) for e in combo])
            ln["done"] = True
            counts["split"] += 1

    # 4. combined: 2-3 lines of one merchant = 1 spreadsheet entry
    for e in [e for e in pool if not e["used"]]:
        group = [ln for ln in open_lines() if ln["date"][:7] == e["month"]]
        found = None
        for n in (2, 3):
            for combo in itertools.combinations(group, n):
                if all(_same_merchant(combo[0]["description"], c["description"]) for c in combo) and \
                        abs(sum(c["amount"] for c in combo) - e["amount"]) < 0.005:
                    if found:
                        found = "ambiguous"
                        break
                    found = combo
            if found:
                break
        if found and found != "ambiguous":
            replace([e], e["month"], None)
            for c in found:
                _insert(db, c, account, batch_id, e["month"], [(e["category_id"], c["amount"])])
                c["done"] = True
            counts["combined"] += len(found)

    # 5. purchase + return that cancel out
    misc = db.execute("SELECT id FROM categories WHERE parent_id IS NULL AND archived = 0 AND name LIKE 'Misc%' LIMIT 1").fetchone()
    for a in open_lines():
        for b in open_lines():
            if a is b or a["done"] or b["done"] or abs(a["amount"] + b["amount"]) > 0.005:
                continue
            if not _same_merchant(a["description"], b["description"]):
                continue
            cid = a["hint"] or b["hint"] or (misc["id"] if misc else None)
            for ln in (a, b):
                _insert(db, ln, account, batch_id, ln["date"][:7], [(cid, ln["amount"])] if cid else [],
                        notes="Purchase and return that cancel out.")
                ln["done"] = True
            counts["pairs"] += 2

    # 6. the rest: visible, not budgeted
    for ln in open_lines():
        _insert(db, ln, account, batch_id, ln["date"][:7], [], excluded=True, notes=NOT_IN_SHEET)
        counts["not_in_sheet"] += 1
    return counts


def _insert(db, ln, account, batch_id, month, splits, excluded=False, notes=None):
    cur = db.execute(
        """INSERT INTO transactions(date, post_date, description, merchant, amount, account, source, budget_month,
               statement_category, txn_type, excluded, reviewed, notes, import_hash, batch_id)
           VALUES (?, ?, ?, ?, ?, ?, 'statement', ?, ?, ?, ?, 1, ?, ?, ?)""",
        (ln["date"], ln["post_date"], ln["description"], merchant_key(ln["description"]), ln["amount"], account, month,
         ln["statement_category"], ln["txn_type"], int(excluded), notes, ln["hash"], batch_id),
    )
    for cid, amt in splits:
        db.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?, ?, ?)", (cur.lastrowid, cid, round(amt, 2)))
    return cur.lastrowid
