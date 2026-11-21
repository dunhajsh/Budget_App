# Household Budget

A local envelope-budgeting app that replaces a yearly Excel budget. It runs on your own computer, keeps every number in one local file, and does most of the typing for you.

- **Imports your old spreadsheet**: every year, every envelope, every amount inside the `=12.34+56.78` formulas. Carried-over balances are reconciled so they match the spreadsheet to the cent.
- **Imports card statements (CSV)**: purchases are sorted automatically by merchant rules that learn as you categorize ("Always put *Ulta* in *Beauty*?"). Card payments are recognized and left out of the budget.
- **Receipts**: drop in photos, PDFs or pasted email receipts. AI (a free local model through Ollama, or Claude) reads every line item, sorts each into an envelope/subcategory, finds the matching card charge, and splits that charge (tax spread proportionally). One $180 Meijer charge becomes Groceries › Produce, Toiletries, House › Household Supplies, …
- **Amazon**: import your Amazon order history. Each shipment is matched to its card charge and split by item.
- **Envelope budgeting**: income, monthly amounts per envelope ("Fill with usual amounts" / "Copy last month"), carry-over, overspending alerts.
- **Recurring bills**: mortgage, car payment, giving, loans. Add them to a month with one click (suggestions come from your history).
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
2. **Transactions → Import statement**: drop in your card's CSV export.
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

## Data & privacy
- Everything is stored in `data/budget.db` (plus receipt images in `data/receipts/`). `data/` is git-ignored, so no financial data ever goes into the repository. Back it up from **Settings → Import & data → Download backup**.
- With the local (Ollama) option, receipts never leave your computer. With Claude, images and text are sent to Anthropic only when you upload a receipt or click *Suggest categories*. The API key is stored in the local database.

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
  receipts.py         receipt matching and tax-aware splitting
  ai.py               AI calls via Claude or local Ollama (receipt reading, categorization)
  importers/          excel.py · statement.py · amazon.py
  static/, templates/ single-page front end (vanilla JS + Chart.js)
```

### Ideas for later
- Automatic bank/card feeds via Plaid or SimpleFIN (no more CSV downloads).
- Forward emailed receipts to a mailbox the app reads.
- Browser extension that captures Amazon/Meijer order pages while you're logged in.
- Savings goals and sinking funds with target dates.
