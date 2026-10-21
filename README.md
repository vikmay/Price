# Telegram Product Price Bot (SQLite + aiogram)

## Automatic price updates from C3 Trade

The bot can receive authenticated product updates at **POST `/api/c3/prices`**.
Enable the receiver with `PRICE_SYNC_KEY` (a separate random secret of at least
32 characters). Defaults: `PRICE_SYNC_HOST=127.0.0.1`, `PRICE_SYNC_PORT=8082`.
Use an HTTPS reverse proxy or a secure private tunnel to reach it from C3 Trade.
The receiver applies each catalog batch atomically, preserves Telegram user
access, rejects stale deliveries, and clears search caches without notifications.
Products are identified by C3 IDs, preserving historical duplicate codes.

The existing GitHub deployment configures the receiver automatically when the
**GitHub Actions secret `PRICE_SYNC_KEY`** is set. Use the same key in C3 Trade:
**Сервіс → Telegram Price — обмін цінами** (administrator only). Manual SSH login
is not required for this setup step. Deployment does not provision the HTTPS
address or network tunnel; that address must be reachable from the C3 server.

For one-time local setup on the bot VM, `python scripts/configure_c3_sync.py`
creates a random key in `.env` while preserving other settings. Run with
`--show-key` locally to copy it into C3 Trade, then restart `price-bot`.
Never commit or share the key. Repeated setup preserves the current key.

Without `PRICE_SYNC_KEY`, the existing bot and HTML import work as before.
When the receiver is configured, HTML uploads are disabled to prevent an old
file from replacing automatically synchronized prices. Disabling exchange in
C3 Trade stops sending updates; remove `PRICE_SYNC_KEY` from the bot environment
and restart it to return to manual HTML imports.

Tests (from the Price directory):

```bash
python -m unittest discover -s tests -p test_price_sync.py -v
```

## Tech stack

- Python 3.11+
- aiogram 3.x
- SQLite
- BeautifulSoup4
- asyncio-based architecture
- Optional: `requests` (reserved for future work)

## Project structure

```
/app
  bot.py
  handlers.py
  db.py
  config.py
/scripts
  import_html.py
/data
  products.db
requirements.txt
README.md
```

## Environment variables (and optional `.env`)

- `TELEGRAM_BOT_TOKEN` (required) — **token is never hardcoded**
- `DB_PATH` (optional, default: `data/products.db`)
- `ADMIN_TELEGRAM_ID` (optional, enables `/reload`)

You can:

1. set them in the environment (recommended for production), or
2. create a local `.env` file next to `README.md` and run the bot — if `.env` exists, it will be loaded automatically.

Example `.env` contents are provided in `.env.example`.

Example (Windows PowerShell):

```powershell
$env:TELEGRAM_BOT_TOKEN="123:ABC"
$env:DB_PATH="data/products.db"
$env:ADMIN_TELEGRAM_ID="123456789"
```

## Install

```bash
pip install -r requirements.txt
```

## Run the bot

```bash
python -m app.bot
```

Bot behavior:

- `/start` — shows a hint
- Any text message — searches products by exact or partial name (top 5)
- Response format (per match): `Name — Price UAH`
- If nothing found: `Товар не знайдено`
- `/reload` — clears the in-memory cache (admin-only, optional)

## Import products from HTML into SQLite

1. Prepare a local HTML file containing product names + prices.
2. Run the importer:

```bash
python scripts/import_html.py file.html
```

Importer rules:

- Extracts product name + price using BeautifulSoup heuristics.
- Safe to re-run: upserts by normalized product name (updates price + `updated_at`).
- Creates SQLite schema and indexes automatically on first run.

## SQL schema

Table `products`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `name TEXT NOT NULL`
- `price REAL NOT NULL`
- `updated_at TEXT NOT NULL`

Indexes:

- `idx_products_name` on `name` for faster LIKE/equality lookups.

## Notes on performance

- Bot queries run via SQLite indexes + `LIMIT 5`.
- DB access is async-safe by using `run_in_executor` (no full dataset loading into memory).
