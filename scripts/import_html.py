from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

# Make `app/` importable when running as: `python scripts/import_html.py ...`
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import ProductsDB, connect  # noqa: E402

NAME_CLEAN_RE = re.compile(r"\s+")

# Example values (UA):
# - "960,00"
# - "30,70"
# - "4,50"
# Allow either decimal comma/dot. Thousands separators may appear.
PRICE_RE = re.compile(
    r"(?P<price>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)[\s]*",
    re.IGNORECASE,
)

# Some pages might include currency marker; our provided catalog doesn't.
CURRENCY_MARKERS_RE = re.compile(r"(UAH|₴)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedProduct:
    code: str
    name: str
    price: float


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _default_db_path() -> str:
    return os.getenv("DB_PATH", os.path.join("data", "products.db"))


def _normalize_name(text: str) -> str:
    cleaned = NAME_CLEAN_RE.sub(" ", text or "").strip()
    return cleaned


def _parse_price(text: str, *, allow_small_without_currency: bool) -> float | None:
    """
    Parse numeric price from a text cell.

    If `allow_small_without_currency` is False:
      - when no currency marker exists, we require at least 3 digits
        to avoid picking numbers embedded in product names.

    If True:
      - used for known price columns (like the provided catalog)
        where prices can be small (e.g. 4,50; 1,50).
    """
    if not text:
        return None

    currency_present = CURRENCY_MARKERS_RE.search(text) is not None

    match = PRICE_RE.search(text)
    if not match:
        return None

    raw = match.group("price").strip()
    if not currency_present and not allow_small_without_currency:
        digits_only = re.sub(r"[^\d]", "", raw)
        if len(digits_only) < 3:
            return None

    # Handle thousand separators + decimal separator:
    # If both comma and dot exist, assume last separator is decimal.
    if "," in raw and "." in raw:
        last_comma = raw.rfind(",")
        last_dot = raw.rfind(".")
        decimal_sep_is_comma = last_comma > last_dot
        if decimal_sep_is_comma:
            raw = raw.replace(".", "")
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    else:
        # Only comma or only dot (or none)
        if raw.count(",") == 1 and raw.count(".") == 0:
            raw = raw.replace(",", ".")
        # else: dot already ok; thousand separators unlikely in this path

    try:
        return float(raw)
    except ValueError:
        return None


def _find_catalog_column_indexes(soup: BeautifulSoup) -> tuple[int, int | None, int] | None:
    """
    Detect the specific catalog table layout:
      - Code / Код
      - Название товара / Назва товару
      - В розницу / В розн...

    Returns: (name_col_index, code_col_index_or_none, price_col_index)
    Preference for price column: "В розницу" (retail).
    """
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        cell_texts = [c.get_text(" ", strip=True) for c in cells]
        joined = " ".join(cell_texts).lower()

        has_name = ("назва" in joined) or ("название товара" in joined) or ("название" in joined)
        has_price = ("в розницу" in joined) or ("в розн" in joined) or ("в розн" in joined)
        has_code = ("код" in joined) or ("code" in joined)

        if not (has_name and has_price):
            continue

        # Find indices by header names.
        name_idx: int | None = None
        code_idx: int | None = None
        price_idx: int | None = None

        for i, t in enumerate(cell_texts):
            tl = t.lower()
            if name_idx is None and ("название товара" in tl or tl == "назва" or "назва" in tl):
                name_idx = i
            if code_idx is None and ("код" in tl or tl == "code" or "code" in tl):
                code_idx = i
            if price_idx is None and ("в розницу" in tl or "в розн" in tl):
                price_idx = i

        if name_idx is not None and price_idx is not None:
            return name_idx, code_idx, price_idx

    return None


def _extract_products_from_table(soup: BeautifulSoup) -> list[ParsedProduct]:
    # First try: detect the exact provided catalog layout and parse directly.
    indexes = _find_catalog_column_indexes(soup)
    if indexes is not None:
        name_idx, code_idx, price_idx = indexes
        products: list[ParsedProduct] = []

        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])

            max_idx = max(name_idx, price_idx, code_idx if code_idx is not None else 0)
            if len(cells) <= max_idx:
                continue

            name_cell = cells[name_idx].get_text(" ", strip=True)
            if not name_cell:
                continue

            # Skip header-like rows.
            if "название" in name_cell.lower() or "назва" in name_cell.lower():
                continue

            code_cell = ""
            if code_idx is not None and code_idx < len(cells):
                code_cell = cells[code_idx].get_text(" ", strip=True)

            price_cell = cells[price_idx].get_text(" ", strip=True)

            name = _normalize_name(name_cell)
            price = _parse_price(price_cell, allow_small_without_currency=True)
            code = code_cell.strip()

            if name and price is not None:
                products.append(ParsedProduct(code=code, name=name, price=price))

        return products

    # Fallback generic: similar to earlier logic, but slightly safer:
    # choose the first "price-looking" cell and first non-empty other cell as name.
    products: list[ParsedProduct] = []

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        cell_texts = [c.get_text(" ", strip=True) for c in cells]

        parsed_prices: list[tuple[int, float]] = []
        for idx, t in enumerate(cell_texts):
            price = _parse_price(t, allow_small_without_currency=False)
            if price is not None:
                parsed_prices.append((idx, price))

        if not parsed_prices:
            continue

        price_idx, price = parsed_prices[0]

        name = ""
        for idx, t in enumerate(cell_texts):
            if idx == price_idx:
                continue
            candidate = _normalize_name(t)
            if len(candidate) >= 2:
                name = candidate
                break

        if name and price is not None:
            products.append(ParsedProduct(code="", name=name, price=price))

    return products


def _extract_products_from_price_elements(soup: BeautifulSoup) -> list[ParsedProduct]:
    # Generic fallback: heuristic extraction from text with prices.
    # Kept for compatibility with other HTML sources.
    products: list[ParsedProduct] = []

    price_elements = soup.find_all(string=re.compile(r"(UAH|₴|\d)"))
    for s in price_elements:
        parent = getattr(s, "parent", None)
        if parent is None:
            continue

        price = _parse_price(str(s), allow_small_without_currency=False)
        if price is None:
            continue

        container = parent.find_parent(["tr", "li", "div", "section", "article"]) or parent
        container_text = container.get_text(" ", strip=True)

        # Remove currency markers and the numeric substring (only once).
        name_candidate = CURRENCY_MARKERS_RE.sub("", container_text)
        name_candidate = re.sub(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?\b", "", name_candidate, count=1)

        name = _normalize_name(name_candidate)
        if name and len(name) >= 2:
            products.append(ParsedProduct(code="", name=name, price=price))

    return products


def extract_products(html: str) -> Iterable[ParsedProduct]:
    soup = BeautifulSoup(html, "html.parser")

    # Prefer table-based extraction (catalog is a table).
    products = _extract_products_from_table(soup)
    if products:
        return products

    # Fallback heuristic extraction.
    return _extract_products_from_price_elements(soup)


def _read_html_bytes_with_best_effort_encoding(path: Path) -> str:
    """
    Try to honor encoding from meta tags, like:
      <meta http-equiv="Content-Type" content="text/html; charset=windows-1251">

    Fallback: UTF-8, then CP1251.
    """
    raw = path.read_bytes()

    # Look for charset in the first ~4KB to avoid scanning huge files.
    head = raw[:4096].decode("latin-1", errors="ignore").lower()
    charset_match = re.search(r"charset\s*=\s*([a-z0-9_\-]+)", head)
    if charset_match:
        enc = charset_match.group(1)
        try:
            return raw.decode(enc)
        except Exception:
            pass

    # Fallbacks
    for enc in ("utf-8", "cp1251", "windows-1251"):
        try:
            return raw.decode(enc)
        except Exception:
            continue

    return raw.decode("utf-8", errors="ignore")


def _drop_products_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS products;")
        conn.commit()
    finally:
        conn.close()


def import_file(file_path: str, db_path: str, *, clear_db: bool = False) -> int:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    html = _read_html_bytes_with_best_effort_encoding(path)

    if clear_db:
        _drop_products_table(db_path)

    # Ensure schema exists.
    connect(db_path)
    products_db = ProductsDB(db_path)

    parsed = list(extract_products(html))
    logging.info("Parsed %d product candidates from HTML", len(parsed))

    inserted = len(parsed)

    # Bulk upsert using a single DB executor call (fast for many rows).
    items = [(p.code, p.name, p.price) for p in parsed]

    import asyncio

    async def _run_bulk() -> None:
        await products_db.insert_or_update_products(items)

    asyncio.run(_run_bulk())
    return inserted


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Import products from a local HTML file into SQLite.")
    parser.add_argument("file", help="Path to local HTML file (e.g., file.html).")
    parser.add_argument("--clear", action="store_true", help="Drop and recreate products table before import.")
    args = parser.parse_args()

    db_path = _default_db_path()
    imported_count = import_file(args.file, db_path=db_path, clear_db=args.clear)
    logging.info("Import completed: %d candidates processed", imported_count)


if __name__ == "__main__":
    main()
