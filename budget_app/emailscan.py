"""Find emailed receipts (order confirmations, digital receipts) in your mailboxes.

- Connects over IMAP, read-only: nothing is marked read, moved, sent or deleted.
- Gmail: searches with Gmail's own search (default: its "Purchases" category).
  Other mailboxes: one folder, filtered by subject.
- Each candidate email is read by the AI. Real receipts become receipts in the app,
  matched to a card charge; everything else is logged as skipped so it's never read twice.
- Passwords (app passwords) are kept in the operating system's credential store
  (Windows Credential Manager / macOS Keychain), never in the database.
"""
import email
import email.policy
import imaplib
import os
import re
import threading
import time
from datetime import date, datetime
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from . import ai, receipts
from .db import connect

KEYRING_SERVICE = "household-budget-email"

PRESETS = {
    "gmail": {"host": "imap.gmail.com", "port": 993, "folder": "INBOX", "search": "category:purchases OR label:receipts"},
    "outlook": {"host": "outlook.office365.com", "port": 993, "folder": "INBOX", "search": None},
    "other": {"host": "", "port": 993, "folder": "INBOX", "search": None},
}

# Subjects that are never receipts (checked before spending any AI time).
SKIP_SUBJECT = re.compile(
    r"\b(shipped|has shipped|delivered|out for delivery|arriving|track your|tracking|rate your|review your|"
    r"how did we do|survey|password|sign[- ]?in|security alert|verify your|verification code|unsubscribe)\b", re.I)
# For mailboxes without Gmail's search, the subject must look like a purchase.
RECEIPT_SUBJECT = re.compile(
    r"\b(receipt|order|purchase|invoice|payment|paid|transaction|confirmation|confirmed|thank you for (your )?"
    r"(order|shopping|purchase|visit)|your .{0,40}(order|purchase|receipt))\b", re.I)
MONEY = re.compile(r"(\$\s?\d[\d,]*\.\d\d|\b\d[\d,]*\.\d\d\b)")

MAX_PER_SCAN = 150          # per account per run; the next run continues
MAX_TEXT = 20000


class EmailError(Exception):
    pass


# ---------------------------------------------------------------- passwords

def _keyring():
    try:
        import keyring
        return keyring
    except ImportError as e:  # pragma: no cover
        raise EmailError("Saving email passwords needs the 'keyring' package. Run: pip install -r requirements.txt") from e


def get_password(address):
    return _keyring().get_password(KEYRING_SERVICE, address)


def set_password(address, password):
    _keyring().set_password(KEYRING_SERVICE, address, password)


def delete_password(address):
    try:
        _keyring().delete_password(KEYRING_SERVICE, address)
    except Exception:
        pass


# ---------------------------------------------------------------- IMAP

def login(acct, password):
    try:
        imap = imaplib.IMAP4_SSL(acct["host"], int(acct["port"] or 993), timeout=20)
    except OSError as e:
        raise EmailError(f"Couldn't reach {acct['host']} (port {acct['port'] or 993}). Workplace networks and VPNs often "
                         "block email ports - try again from home Wi-Fi. Otherwise check the server name.") from e
    try:
        imap.login(acct["address"], password)
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if acct.get("provider") == "gmail":
            raise EmailError("Gmail rejected the sign-in. Use a 16-letter app password (Google Account → Security → "
                             "2-Step Verification → App passwords), not your normal password.") from e
        if acct.get("provider") == "outlook":
            raise EmailError("Outlook rejected the sign-in. Microsoft has turned off app-password sign-in for many "
                             "personal accounts. Instead, set up an Outlook rule that forwards receipts to one of your "
                             "Gmail accounts (see the help on this page).") from e
        raise EmailError(f"Sign-in failed: {msg}") from e
    return imap


def _ok(typ, data, what):
    if typ != "OK":
        raise EmailError(f"The mail server refused to {what}: {data[0].decode(errors='replace') if data else typ}")
    return data


def _gmail_all_mail(imap):
    """Gmail's 'All Mail' folder (its name is localized, so find it by its \\All flag)."""
    typ, data = imap.list()
    for line in data or []:
        s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        if "\\All" in s:
            m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
            if m:
                return m.group(1)
    return "[Gmail]/All Mail"


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_uids(imap, acct):
    """UIDs of emails worth looking at, oldest first."""
    since = date.fromisoformat(acct["since"])
    if acct.get("provider") == "gmail" and acct.get("search"):
        _ok(*imap.select(_quote(_gmail_all_mail(imap)), readonly=True), "open All Mail")
        query = f"({acct['search']}) after:{since:%Y/%m/%d}"
        data = _ok(*imap.uid("SEARCH", "X-GM-RAW", _quote(query)), "search")
    else:
        _ok(*imap.select(_quote(acct.get("folder") or "INBOX"), readonly=True), f"open folder {acct.get('folder')}")
        data = _ok(*imap.uid("SEARCH", "SINCE", since.strftime("%d-%b-%Y")), "search")
    return [int(u) for u in (data[0] or b"").split()]


def fetch_headers(imap, uids):
    """{uid: (message_id, subject, sender, date_iso)} - headers only, without marking anything read."""
    out = {}
    for i in range(0, len(uids), 100):
        chunk = ",".join(str(u) for u in uids[i:i + 100])
        data = _ok(*imap.uid("FETCH", chunk, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"), "read headers")
        for part in data:
            if not isinstance(part, tuple):
                continue
            m = re.search(rb"UID (\d+)", part[0])
            if not m:
                continue
            msg = email.message_from_bytes(part[1], policy=email.policy.default)
            out[int(m.group(1))] = _header_info(msg)
    return out


def _header_info(msg):
    try:
        sent = parsedate_to_datetime(msg["Date"]).date().isoformat() if msg["Date"] else ""
    except (TypeError, ValueError):
        sent = ""
    name, addr = parseaddr(str(msg["From"] or ""))
    return {
        "message_id": str(msg["Message-ID"] or "").strip(),
        "subject": str(msg["Subject"] or "").strip(),
        "sender": f"{name} <{addr}>" if name else addr,
        "date": sent,
    }


def fetch_message(imap, uid):
    data = _ok(*imap.uid("FETCH", str(uid), "(BODY.PEEK[])"), "read an email")
    for part in data:
        if isinstance(part, tuple):
            return email.message_from_bytes(part[1], policy=email.policy.default)
    raise EmailError("Email body was empty")


# ---------------------------------------------------------------- email content

class _HTMLText(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(html):
    p = _HTMLText()
    p.feed(html)
    return tidy("".join(p.out))


def tidy(text):
    out = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t\u00a0\u200b\u200c\u034f]+", " ", ln)   # spaces, nbsp and zero-width padding
        ln = re.sub(r"( ?\| ?)+", " | ", ln).strip(" |")           # collapse empty table cells
        if ln:
            out.append(ln)
    return "\n".join(out)[:MAX_TEXT]


def extract(msg):
    """(body_text, [(bytes, media_type, filename)]) - the readable body and any receipt attachments."""
    plain, html, files = None, None, []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        if disp == "attachment" or (ctype == "application/pdf"):
            if ctype == "application/pdf" or ctype in ("image/jpeg", "image/png", "image/webp"):
                payload = part.get_payload(decode=True) or b""
                if len(payload) > 15000 or ctype == "application/pdf":  # skip logos and tiny icons
                    files.append((payload, ctype, part.get_filename() or "attachment"))
            continue
        if ctype == "text/plain" and plain is None:
            plain = part.get_content()
        elif ctype == "text/html" and html is None:
            html = part.get_content()
    text = ""
    if html:
        text = html_to_text(html)
    if plain and len(tidy(plain)) >= max(200, len(text) // 3):
        text = tidy(plain)
    return text, files[:3]


# ---------------------------------------------------------------- scanning

def _seen_status(db, account_id, message_id):
    r = db.execute("SELECT status FROM email_seen WHERE account_id = ? AND message_id = ?",
                   (account_id, message_id)).fetchone()
    return r["status"] if r else None


def _mark(db, account_id, info, status, reason=None, receipt_id=None):
    db.execute(
        """INSERT INTO email_seen(account_id, message_id, sent_at, sender, subject, status, reason, receipt_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(account_id, message_id) DO UPDATE SET status = excluded.status, reason = excluded.reason,
               receipt_id = excluded.receipt_id, seen_at = datetime('now')""",
        (account_id, info["message_id"], info["date"], info["sender"][:200], info["subject"][:300], status, reason, receipt_id),
    )


def _duplicate_of(db, res, info):
    """An existing receipt for the same order (e.g. a confirmation and a later receipt email)."""
    num = (res.get("order_number") or "").strip()
    if num and len(num) >= 4:
        r = db.execute("SELECT id FROM receipts WHERE json_extract(raw_json, '$.order_number') = ? LIMIT 1", (num,)).fetchone()
        if r:
            return r["id"]
    r = db.execute("SELECT id FROM receipts WHERE external_id = ?", ("email:" + info["message_id"],)).fetchone()
    return r["id"] if r else None


def screen(acct, info):
    """Cheap subject check before any AI time is spent. Returns a skip reason or None."""
    subj = info["subject"]
    if SKIP_SUBJECT.search(subj):
        return "Shipping/account notice"
    if not (acct.get("provider") == "gmail" and acct.get("search")) and not RECEIPT_SUBJECT.search(subj):
        return "Subject doesn't look like a purchase"
    return None


def process_message(db, acct, info, msg, receipt_dir):
    """Read one email. Returns ('receipt'|'skipped'|'duplicate', reason, receipt_id)."""
    text, files = extract(msg)
    if not files and not MONEY.search(text):
        return "skipped", "No prices in the email", None
    email_meta = {"subject": info["subject"], "sender": info["sender"], "date": info["date"],
                  "account": acct["address"]}
    res = ai.parse_receipt(db, text=text, email=email_meta, attachments=[(d, mt) for d, mt, _ in files])
    if not res.get("is_receipt") or not (res.get("items") or res.get("total")):
        return "skipped", "Not a receipt", None
    dup = _duplicate_of(db, res, info)
    if dup:
        return "duplicate", f"Same order as receipt #{dup}", dup

    stored = None
    if files:
        data, mt, fname = files[0]
        ext = ".pdf" if mt == "application/pdf" else ".jpg" if mt == "image/jpeg" else "." + mt.split("/")[1]
        stored = f"{datetime.now():%Y%m%d%H%M%S%f}_email{ext}"
        with open(os.path.join(receipt_dir, stored), "wb") as f:
            f.write(data)
    cur = db.execute(
        "INSERT INTO receipts(source, external_id, filename, stored_path, status) VALUES ('email', ?, ?, ?, 'pending')",
        ("email:" + info["message_id"], (info["subject"] or "Emailed receipt")[:200], stored),
    )
    rid = cur.lastrowid
    receipts.save_parsed(db, rid, res, text=text, email=email_meta)
    m = receipts.best_match(db, rid)
    if m:
        db.execute("UPDATE receipts SET transaction_id = ? WHERE id = ?", (m["id"], rid))
    return "receipt", res.get("merchant"), rid


def scan_account(db, acct, password, receipt_dir, progress=None, imap=None, should_stop=lambda: False):
    """Scan one mailbox. `imap` can be passed in (tests); otherwise we log in."""
    progress = progress or (lambda **kw: None)
    own = imap is None
    imap = imap or login(acct, password)
    counts = {"receipts": 0, "skipped": 0, "duplicates": 0, "errors": 0, "remaining": 0}
    try:
        uids = find_uids(imap, acct)
        headers = fetch_headers(imap, uids)
        todo = []
        for uid in uids:
            info = headers.get(uid)
            if not info:
                continue
            if not info["message_id"]:
                info["message_id"] = f"uid:{acct['id']}:{uid}"
            seen = _seen_status(db, acct["id"], info["message_id"])
            if seen and seen != "retry":
                continue
            reason = None if seen == "retry" else screen(acct, info)  # "Read again" skips the subject filter
            if reason:
                _mark(db, acct["id"], info, "skipped", reason)
                counts["skipped"] += 1
                continue
            todo.append((uid, info))
        db.commit()
        counts["remaining"] = max(0, len(todo) - MAX_PER_SCAN)
        todo = todo[:MAX_PER_SCAN]
        for n, (uid, info) in enumerate(todo, 1):
            if should_stop():
                counts["remaining"] += len(todo) - n + 1
                break
            progress(step=f"{acct['address']}: {n} of {len(todo)} - {info['subject'][:70]}", done=n - 1, total=len(todo))
            try:
                msg = fetch_message(imap, uid)
                status, reason, rid = process_message(db, acct, info, msg, receipt_dir)
            except ai.AIError as e:
                # AI unavailable: stop here and leave the email unread so the next scan retries it.
                counts["errors"] += 1
                db.commit()
                raise EmailError(str(e)) from e
            except Exception as e:  # one bad email shouldn't stop the scan
                status, reason, rid = "error", str(e)[:300], None
            _mark(db, acct["id"], info, status, reason, rid)
            counts[{"receipt": "receipts", "skipped": "skipped", "duplicate": "duplicates", "error": "errors"}[status]] += 1
            db.commit()
        db.execute("UPDATE email_accounts SET last_checked = datetime('now'), last_error = NULL WHERE id = ?", (acct["id"],))
        db.commit()
        return counts
    finally:
        if own:
            try:
                imap.logout()
            except Exception:
                pass


# ---------------------------------------------------------------- background job

class ScanJob:
    """One scan at a time, in a background thread; the page polls `status()`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False}
        self._stop = False

    def status(self):
        with self.lock:
            return dict(self.state)

    def _update(self, **kw):
        with self.lock:
            self.state.update(kw)

    def cancel(self):
        self._stop = True

    def start(self, db_path, receipt_dir, account_ids=None):
        with self.lock:
            if self.state.get("running"):
                raise EmailError("A scan is already running.")
            self.state = {"running": True, "started_at": time.time(), "step": "Connecting…", "done": 0, "total": 0,
                          "results": [], "finished_at": None}
        self._stop = False
        t = threading.Thread(target=self._run, args=(db_path, receipt_dir, account_ids), daemon=True)
        t.start()

    def _run(self, db_path, receipt_dir, account_ids):
        db = connect(db_path)
        try:
            q = "SELECT * FROM email_accounts WHERE active = 1"
            accts = [dict(a) for a in db.execute(q + " ORDER BY id")]
            if account_ids:
                accts = [a for a in accts if a["id"] in account_ids]
            results = []
            for acct in accts:
                if self._stop:
                    break
                self._update(step=f"{acct['address']}: connecting…", done=0, total=0)
                try:
                    pw = get_password(acct["address"])
                    if not pw:
                        raise EmailError("No saved password - re-enter it in Settings → Email.")
                    c = scan_account(db, acct, pw, receipt_dir, progress=self._update, should_stop=lambda: self._stop)
                    results.append({"address": acct["address"], **c})
                except Exception as e:
                    msg = str(e) if isinstance(e, EmailError) else f"{type(e).__name__}: {e}"
                    db.execute("UPDATE email_accounts SET last_checked = datetime('now'), last_error = ? WHERE id = ?",
                               (msg, acct["id"]))
                    db.commit()
                    results.append({"address": acct["address"], "error": msg})
                self._update(results=list(results))
            self._update(running=False, finished_at=time.time(), step="Cancelled" if self._stop else "Done",
                         results=results)
        finally:
            db.close()
            with self.lock:
                self.state["running"] = False


JOB = ScanJob()


def account_dict(db, a):
    a = dict(a)
    counts = {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) n FROM email_seen WHERE account_id = ? GROUP BY status", (a["id"],))}
    a["counts"] = counts
    try:
        a["has_password"] = bool(get_password(a["address"]))
    except EmailError:
        a["has_password"] = False
    return a

