"""SQLite storage. One file (data/budget.db) holds everything; back it up by copying it."""
import os
import sqlite3
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    parent_id      INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    grp            TEXT NOT NULL DEFAULT 'Other',
    default_amount REAL NOT NULL DEFAULT 0,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    archived       INTEGER NOT NULL DEFAULT 0
);

-- Money put into an envelope for a month (the spreadsheet's "Total" column)
CREATE TABLE IF NOT EXISTS allocations (
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month       TEXT NOT NULL,              -- YYYY-MM
    amount      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (category_id, month)
);

-- One-off balance corrections (opening balances, spreadsheet reconciliation)
CREATE TABLE IF NOT EXISTS adjustments (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month       TEXT NOT NULL,
    amount      REAL NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS income (
    id          INTEGER PRIMARY KEY,
    month       TEXT NOT NULL,
    date        TEXT,
    amount      REAL NOT NULL,
    description TEXT,
    source      TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    filename     TEXT,
    budget_month TEXT,
    row_count    INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- amount > 0 is money spent, amount < 0 is a refund/credit
CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY,
    date               TEXT NOT NULL,
    post_date          TEXT,
    description        TEXT NOT NULL,
    merchant           TEXT,
    amount             REAL NOT NULL,
    account            TEXT,
    source             TEXT NOT NULL DEFAULT 'manual',   -- statement | manual | recurring | excel
    budget_month       TEXT NOT NULL,
    statement_category TEXT,
    txn_type           TEXT,
    excluded           INTEGER NOT NULL DEFAULT 0,       -- card payments / transfers
    reviewed           INTEGER NOT NULL DEFAULT 0,
    notes              TEXT,
    import_hash        TEXT UNIQUE,
    batch_id           INTEGER REFERENCES import_batches(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_txn_month ON transactions(budget_month);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(date);

CREATE TABLE IF NOT EXISTS splits (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    amount         REAL NOT NULL,
    memo           TEXT
);
CREATE INDEX IF NOT EXISTS ix_split_txn ON splits(transaction_id);
CREATE INDEX IF NOT EXISTS ix_split_cat ON splits(category_id);

CREATE TABLE IF NOT EXISTS receipts (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL DEFAULT 'upload',   -- upload | text | amazon
    external_id    TEXT UNIQUE,
    filename       TEXT,
    stored_path    TEXT,
    merchant       TEXT,
    purchase_date  TEXT,
    subtotal       REAL,
    tax            REAL,
    total          REAL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | parsed | applied | error
    error          TEXT,
    transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    raw_json       TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS receipt_items (
    id          INTEGER PRIMARY KEY,
    receipt_id  INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    quantity    REAL,
    amount      REAL NOT NULL,
    taxable     INTEGER NOT NULL DEFAULT 1,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY,
    pattern     TEXT NOT NULL,
    match_type  TEXT NOT NULL DEFAULT 'contains',   -- contains | exact | regex
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 0,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fallback: the card issuer's own category -> one of your envelopes
CREATE TABLE IF NOT EXISTS statement_category_map (
    statement_category TEXT PRIMARY KEY,
    category_id        INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recurring (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    amount       REAL NOT NULL,
    day_of_month INTEGER NOT NULL DEFAULT 1,
    account      TEXT NOT NULL DEFAULT 'Checking',
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Mailboxes scanned for emailed receipts. Passwords live in the OS credential store, not here.
CREATE TABLE IF NOT EXISTS email_accounts (
    id           INTEGER PRIMARY KEY,
    address      TEXT NOT NULL UNIQUE,
    provider     TEXT NOT NULL DEFAULT 'gmail',   -- gmail | outlook | other
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL DEFAULT 993,
    folder       TEXT NOT NULL DEFAULT 'INBOX',
    search       TEXT,                            -- Gmail search, e.g. category:purchases
    since        TEXT NOT NULL,                   -- only emails on/after this date
    active       INTEGER NOT NULL DEFAULT 1,
    last_checked TEXT,
    last_error   TEXT
);

-- Every email looked at, so nothing is read twice.
CREATE TABLE IF NOT EXISTS email_seen (
    account_id  INTEGER NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
    message_id  TEXT NOT NULL,
    sent_at     TEXT,
    sender      TEXT,
    subject     TEXT,
    status      TEXT NOT NULL,       -- receipt | skipped | duplicate | error
    reason      TEXT,
    receipt_id  INTEGER REFERENCES receipts(id) ON DELETE SET NULL,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, message_id)
);
"""


def connect(path):
    conn = sqlite3.connect(path, detect_types=0, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS won't add them)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(income)")}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE income ADD COLUMN batch_id INTEGER")


def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


def get_setting(db, key, default=None):
    r = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
