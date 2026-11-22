# Household Budget

A local envelope-budgeting app that replaces a yearly Excel budget. It runs on your own computer, keeps every number in one local file, and does most of the typing for you.

- **Imports your old spreadsheet**: every year, every envelope, every amount inside the `=12.34+56.78` formulas. Carried-over balances are reconciled so they match the spreadsheet to the cent.
- **Imports card statements and bank accounts (CSV)**: purchases are sorted automatically by merchant rules that learn as you categorize ("Always put *Ulta* in *Beauty*?"). From bank accounts, bills are budgeted, paychecks and interest become income (optionally for next month's budget), and transfers and card payments are left out.
- **Statements replace spreadsheet history, totals unchanged**: for months the spreadsheet already covers, each statement line takes the place of the spreadsheet entry it matches (exactly, split over several envelopes, several lines combined, or with a small typo kept as a visible difference) and takes that entry's envelope. Purchase/return pairs are added; lines the spreadsheet doesn't have are shown but not budgeted. Every month/envelope total is checked to the cent before anything is saved, and undo restores the spreadsheet entries. Merchant rules are learned from the result.
- **Receipts**: drop in photos, PDFs or pasted email receipts. AI (a free local model through Ollama, or Claude) reads every line item, sorts each into an envelope/subcategory, finds the matching card charge, and splits that charge (tax spread proportionally). One $180 Meijer charge becomes Groceries › Produce, Toiletries, House › Household Supplies, …
- **Matching receipts to charges**: suggestions are only charges with the exact amount or the store's name. A receipt can go on several charges (charged in parts) and several receipts on one charge: e.g. an Amazon order shipped in two boxes but charged once ($20.00 + $5.50 = one $25.50 charge) is found automatically, and the charge is split by the items of both. *Find another charge* searches any charge by name or amount.
- **Coupons**: store receipts' item discounts come off their item; coupons on the whole order ("Total Basket Coupon", "$15 off" mPerks offers) become their own *Coupon* line.
- **Item memory**: when you save a receipt, the app remembers where you filed each item and files the same product the same way on future receipts (your choice beats the AI's guess; "BANANAS 2.3 LB" = "Bananas"). Edit or forget items under Settings → Item memory.
- **Amazon**: import your Amazon order history. Each shipment is matched to its card charge and split by item.
- **Email receipts**: connect Gmail/Outlook/IMAP mailboxes (read-only). Order confirmations and digital receipts are found, read, matched to card charges and split, with no typing.
- **Envelope budgeting**: income, monthly amounts per envelope ("Fill with usual amounts" / "Copy last month"), carry-over, overspending alerts.
- **Recurring bills**: mortgage, car payment, giving, loans. Add them to a month with one click (suggestions come from your history).
- **Spending breakdown**: each envelope this month vs. last month and its average; per envelope, subcategories stacked by month (in $ or % share) with a month-by-month table; any two months compared side by side ("What changed?" ranks the items that moved most); and a drill-down into any subcategory: stores, every charge, and the receipt items inside it with what you paid each time (low/high/last, price trend).
- **Reports**: income vs. spending trends, a year grid like the old spreadsheet, envelope drill-downs, top receipt items, and Excel export.

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python run.py            # opens http://127.0.0.1:5000
```

On Windows you can double-click **Start Budget.bat**.

`python run.py --lan` also serves it on your home Wi-Fi so you can open it on your phone and snap receipt photos directly.

### First-time setup
1. **Settings → Import & data**: drop in your budget `.xlsx`.
2. **Transactions → Import statement**: drop in your card and bank CSV exports (several at once is fine). Include a few past months your spreadsheet already covers so the app can learn how you categorize.
3. **Settings → AI & receipts** (optional): turn on automatic receipt reading and category suggestions. Everything else works without it. Pick one:
   - **Local (Ollama)**: free, and nothing leaves your computer. Install [Ollama](https://ollama.com), run `ollama pull qwen3.5:4b` (or any model that reads images), then choose it in Settings. A receipt takes about 15 seconds; if the items or tax don't add up, it's re-read carefully (about 1 minute).
   - **Claude**: most accurate on crumpled or long receipts. Paste an Anthropic API key from <https://console.anthropic.com/>. A few cents per receipt.

## Monthly routine (about 10 minutes)
1. **Budget**: add paychecks, click *Fill with usual amounts*, adjust, and aim for **$0 left to budget**. Click *Add bills* for recurring bills.
2. **Transactions → Import statement** when the statement closes. Most rows arrive categorized (marked *auto*).
3. **Needs category**: pick a category for what's left. Answer "Always…?" to create a rule so it's automatic next month.
4. **Receipts**: drop in receipts for big mixed purchases (Meijer, Walmart, Target) and import Amazon orders. Each split is applied to its card charge.
5. **Needs review → Mark all reviewed**, then check the **Dashboard**.

## Store accounts (Meijer, Amazon, …)

Neither Meijer nor Amazon offers a public API for personal purchase history, and logging in with your password to scrape their sites breaks their terms of service, trips 2-factor/captcha checks, and means storing your passwords. So the app uses the official data each store gives you:

| Store | How to get item-level data in |
|---|---|
| **Amazon** | amazon.com → *Request Your Data* → *Your Orders*. Amazon emails a zip; drop it on **Receipts → Import Amazon orders**. Repeat every month or two (only new shipments are added). |
| **Meijer** | Meijer app → *Purchases* (mPerks digital receipts) → open a receipt → screenshot it, or save the emailed Meijer.com order receipt as PDF. Drop it on **Receipts**. |
| **Anything else** | Photo of the paper receipt, PDF, or paste the text of an emailed receipt. |

### Email receipts

**Settings → Email** connects mailboxes; **Receipts → Check email** scans them in the background.

- **Gmail**: needs 2-Step Verification and an [app password](https://myaccount.google.com/apppasswords). The app searches Gmail's *Purchases* category plus anything labeled *Receipts* (editable; any Gmail search works).
- **Outlook.com / Hotmail**: Microsoft has turned off app-password sign-in for many personal accounts. If it's refused, add an Outlook rule that forwards receipt emails to a Gmail account, and label them *Receipts* there.
- **Other mail**: any IMAP server; one folder, filtered to purchase-like subjects.

It is read-only (nothing is marked read, moved or deleted). App passwords are kept in the operating system's credential store (Windows Credential Manager), not in the database. Shipping notices and ads are skipped; a second email for the same order is recognized as a duplicate. Every email checked is listed under **Settings → Email**, with *Read again* for anything wrongly skipped. Note: some workplace networks block mail ports (993); scan from home if the connection fails.

## Data & privacy
- Everything is stored in `data/budget.db` (plus receipt images in `data/receipts/`). `data/` is git-ignored, so no financial data ever goes into the repository. Back it up from **Settings → Import & data → Download backup**.
- With the local (Ollama) option, receipts and emails never leave your computer. With Claude, images and text are sent to Anthropic only when you upload a receipt or click *Suggest categories*. The API key is stored in the local database.

## Development

```bash
python -m unittest discover -s tests -v
```

Code layout:

```
budget_app/
  api.py              JSON API (Flask blueprint)
  budget.py           envelope math: carry-over, available, history
  categorize.py       merchant normalization + rules
  itemmemory.py       remembered receipt-item categories
  receipts.py         receipt reading flow and tax-aware splitting
  matching.py         receipt <-> card charge suggestions and links (many-to-many)
  spending.py         spending by envelope/subcategory and drill-down detail
  ai.py               AI calls via Claude or local Ollama (receipt reading, categorization)
  emailscan.py        IMAP mailbox scanning for emailed receipts
  importers/          excel.py · statement.py · amazon.py
  static/, templates/ single-page front end (vanilla JS + Chart.js)
```

### Ideas for later
- Automatic bank/card feeds via Plaid or SimpleFIN (no more CSV downloads).
- Browser extension that captures Amazon/Meijer order pages while you're logged in.
- Savings goals and sinking funds with target dates.
