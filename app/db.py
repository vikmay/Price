from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

DEFAULT_LIMIT: Final[int] = 5


@dataclass(frozen=True)
class ProductMatch:
    code: str
    name: str
    price: float
    updated_at: str


def _utc_now_iso() -> str:
    # ISO-8601 is fine for sorting/display; we don't parse it in queries.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _casefold_str(value: str) -> str:
    return (value or "").casefold()


def _normalize_code(value: str) -> str:
    # Keep digits/letters exactly (important for leading zeros in codes),
    # but trim whitespace and normalize internal whitespace.
    # We don't do casefold here because codes are expected to be numeric,
    # and case changes would be undesirable for SKU-like values.
    return " ".join((value or "").split())


def connect(db_path: str) -> None:
    """
    Create the schema + indexes if they don't exist.

    Important: SQLite's Unicode case folding is not reliable for Cyrillic.
    We therefore store a Python `casefold()` normalized column (name_cf)
    and search/order by it for case-insensitive behavior.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=ON;")

        # Create table (with name_cf and code).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                name_cf TEXT NOT NULL,
                price REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # Ensure legacy columns exist on older DBs.
        cur = conn.execute("PRAGMA table_info(products);")
        cols = {row[1] for row in cur.fetchall()}  # row[1] is column name

        if "code" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN code TEXT;")

        if "name_cf" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN name_cf TEXT;")

        # Backfill missing values.
        conn.execute(
            "UPDATE products SET code = '' WHERE code IS NULL;",
        )
        rows = conn.execute(
            """
            SELECT id, name
            FROM products
            WHERE name_cf IS NULL OR name_cf = '';
            """
        ).fetchall()
        if rows:
            for row in rows:
                pid = int(row[0])
                name = str(row[1])
                conn.execute("UPDATE products SET name_cf = ? WHERE id = ?;", (_casefold_str(name), pid))

        # Indexes
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_products_code
            ON products(code);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_products_name
            ON products(name);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_products_name_cf
            ON products(name_cf);
            """
        )

        conn.commit()
    finally:
        conn.close()


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _looks_like_code(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    # Most catalog codes are numeric; treat digits-only as "code search".
    return q.isdigit()


def _search_products_by_code_exact_sync(db_path: str, code: str, limit: int) -> list[ProductMatch]:
    clean_code = _normalize_code(code)
    if not clean_code:
        return []

    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            SELECT code, name, price, updated_at
            FROM products
            WHERE code = ?
            ORDER BY updated_at DESC
            LIMIT ?;
            """,
            (clean_code, limit),
        )
        rows: list[ProductMatch] = []
        for row in cur.fetchall():
            rows.append(
                ProductMatch(
                    code=str(row["code"]),
                    name=str(row["name"]),
                    price=float(row["price"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        return rows
    finally:
        conn.close()


def _search_products_by_price_exact_sync(db_path: str, price: float, limit: int) -> list[ProductMatch]:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            SELECT code, name, price, updated_at
            FROM products
            WHERE price = ?
            ORDER BY updated_at DESC
            LIMIT ?;
            """,
            (float(price), limit),
        )
        rows: list[ProductMatch] = []
        for row in cur.fetchall():
            rows.append(
                ProductMatch(
                    code=str(row["code"]),
                    name=str(row["name"]),
                    price=float(row["price"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        return rows
    finally:
        conn.close()


def _search_products_by_name_only_sync(db_path: str, query: str, limit: int) -> list[ProductMatch]:
    q = query.strip()
    if not q:
        return []

    q_cf = _casefold_str(q)
    prefix_like = f"{q_cf}%"
    substring_like = f"%{q_cf}%"

    conn = _get_conn(db_path)
    try:
        rows: list[ProductMatch] = []
        existing_codes: set[str] = set()

        cur = conn.execute(
            """
            SELECT code, name, price, updated_at
            FROM products
            WHERE name_cf LIKE ?
            ORDER BY
                CASE
                    WHEN name_cf = ? THEN 0
                    WHEN name_cf LIKE ? THEN 1
                    ELSE 2
                END,
                name_cf
            LIMIT ?;
            """,
            (prefix_like, q_cf, prefix_like, limit),
        )
        for row in cur.fetchall():
            code = str(row["code"])
            if code in existing_codes:
                continue
            existing_codes.add(code)
            rows.append(
                ProductMatch(
                    code=code,
                    name=str(row["name"]),
                    price=float(row["price"]),
                    updated_at=str(row["updated_at"]),
                )
            )

        if len(rows) < limit:
            remaining = limit - len(rows)
            extra = remaining * 5

            cur2 = conn.execute(
                """
                SELECT code, name, price, updated_at
                FROM products
                WHERE name_cf LIKE ?
                ORDER BY
                    CASE
                        WHEN name_cf = ? THEN 0
                        WHEN name_cf LIKE ? THEN 1
                        ELSE 2
                    END,
                    name_cf
                LIMIT ?;
                """,
                (substring_like, q_cf, prefix_like, extra),
            )
            for row in cur2.fetchall():
                code = str(row["code"])
                if code in existing_codes:
                    continue
                existing_codes.add(code)
                rows.append(
                    ProductMatch(
                        code=code,
                        name=str(row["name"]),
                        price=float(row["price"]),
                        updated_at=str(row["updated_at"]),
                    )
                )
                if len(rows) >= limit:
                    break

        return rows
    finally:
        conn.close()


def _search_products_sync(db_path: str, query: str, limit: int) -> list[ProductMatch]:
    q = query.strip()
    if not q:
        return []

    conn = _get_conn(db_path)
    try:
        # 1) Exact match by code (digits-only) if it looks like a code.
        if _looks_like_code(q):
            cur = conn.execute(
                """
                SELECT code, name, price, updated_at
                FROM products
                WHERE code = ?
                ORDER BY updated_at DESC
                LIMIT ?;
                """,
                (q, limit),
            )
            rows: list[ProductMatch] = []
            for row in cur.fetchall():
                rows.append(
                    ProductMatch(
                        code=str(row["code"]),
                        name=str(row["name"]),
                        price=float(row["price"]),
                        updated_at=str(row["updated_at"]),
                    )
                )
            if rows:
                return rows
            # If no code matches, fall through to name search.

        # 2) Case-insensitive name search (prefix first, then substring).
        q_cf = _casefold_str(q)
        prefix_like = f"{q_cf}%"
        substring_like = f"%{q_cf}%"

        rows = []
        existing_codes: set[str] = set()

        cur = conn.execute(
            """
            SELECT code, name, price, updated_at
            FROM products
            WHERE name_cf LIKE ?
            ORDER BY
                CASE
                    WHEN name_cf = ? THEN 0
                    WHEN name_cf LIKE ? THEN 1
                    ELSE 2
                END,
                name_cf
            LIMIT ?;
            """,
            (prefix_like, q_cf, prefix_like, limit),
        )
        for row in cur.fetchall():
            code = str(row["code"])
            if code in existing_codes:
                continue
            existing_codes.add(code)
            rows.append(
                ProductMatch(
                    code=code,
                    name=str(row["name"]),
                    price=float(row["price"]),
                    updated_at=str(row["updated_at"]),
                )
            )

        # Fallback: substring search only if we didn't fill the list.
        if len(rows) < limit:
            remaining = limit - len(rows)
            extra = remaining * 5

            cur2 = conn.execute(
                """
                SELECT code, name, price, updated_at
                FROM products
                WHERE name_cf LIKE ?
                ORDER BY
                    CASE
                        WHEN name_cf = ? THEN 0
                        WHEN name_cf LIKE ? THEN 1
                        ELSE 2
                    END,
                    name_cf
                LIMIT ?;
                """,
                (substring_like, q_cf, prefix_like, extra),
            )
            for row in cur2.fetchall():
                code = str(row["code"])
                if code in existing_codes:
                    continue
                existing_codes.add(code)
                rows.append(
                    ProductMatch(
                        code=code,
                        name=str(row["name"]),
                        price=float(row["price"]),
                        updated_at=str(row["updated_at"]),
                    )
                )
                if len(rows) >= limit:
                    break

        return rows
    finally:
        conn.close()


def _insert_or_update_product_sync(
    db_path: str,
    code: str,
    name: str,
    price: float,
    updated_at: str,
) -> None:
    clean_name = " ".join((name or "").split())
    if not clean_name:
        return

    clean_name_cf = _casefold_str(clean_name)
    clean_code = _normalize_code(code)

    conn = _get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")

        # Prefer upsert by code if present; fallback to name_cf.
        if clean_code:
            cur = conn.execute("SELECT id FROM products WHERE code = ? LIMIT 1;", (clean_code,))
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO products (code, name, name_cf, price, updated_at)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (clean_code, clean_name, clean_name_cf, price, updated_at),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                    SET code = ?, name = ?, name_cf = ?, price = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (clean_code, clean_name, clean_name_cf, price, updated_at, int(row["id"])),
                )
        else:
            cur = conn.execute("SELECT id FROM products WHERE name_cf = ? LIMIT 1;", (clean_name_cf,))
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO products (code, name, name_cf, price, updated_at)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    ("", clean_name, clean_name_cf, price, updated_at),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                    SET name = ?, name_cf = ?, price = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (clean_name, clean_name_cf, price, updated_at, int(row["id"])),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_or_update_products_sync(
    db_path: str,
    items: list[tuple[str, str, float]],
    updated_at: str,
) -> None:
    """
    items: list of (code, name, price)
    """
    if not items:
        return

    conn = _get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")

        select_id_by_code = conn.execute
        select_id_by_name_cf = conn.execute

        insert_sql = """
            INSERT INTO products (code, name, name_cf, price, updated_at)
            VALUES (?, ?, ?, ?, ?);
        """
        update_sql_by_code = """
            UPDATE products
            SET code = ?, name = ?, name_cf = ?, price = ?, updated_at = ?
            WHERE id = ?;
        """
        update_sql_by_name_cf = """
            UPDATE products
            SET name = ?, name_cf = ?, price = ?, updated_at = ?
            WHERE id = ?;
        """

        for code, name, price in items:
            clean_name = " ".join((name or "").split())
            if not clean_name:
                continue

            clean_name_cf = _casefold_str(clean_name)
            clean_code = _normalize_code(code)

            if clean_code:
                cur = select_id_by_code("SELECT id FROM products WHERE code = ? LIMIT 1;", (clean_code,))
                row = cur.fetchone()
                if row is None:
                    conn.execute(insert_sql, (clean_code, clean_name, clean_name_cf, float(price), updated_at))
                else:
                    conn.execute(
                        update_sql_by_code,
                        (
                            clean_code,
                            clean_name,
                            clean_name_cf,
                            float(price),
                            updated_at,
                            int(row["id"]),
                        ),
                    )
            else:
                cur = select_id_by_name_cf(
                    "SELECT id FROM products WHERE name_cf = ? LIMIT 1;",
                    (clean_name_cf,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.execute(insert_sql, ("", clean_name, clean_name_cf, float(price), updated_at))
                else:
                    conn.execute(
                        update_sql_by_name_cf,
                        (clean_name, clean_name_cf, float(price), updated_at, int(row["id"])),
                    )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class ProductsDB:
    """
    Async-safe wrapper around SQLite using the default executor.
    Suitable for a lightweight aiogram bot on a 1GB VPS.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> str:
        return self._db_path

    async def search_products(self, query: str, limit: int = DEFAULT_LIMIT) -> list[ProductMatch]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _search_products_sync, self._db_path, query, limit)

    async def search_products_by_name_only(
        self,
        query: str,
        limit: int = DEFAULT_LIMIT,
    ) -> list[ProductMatch]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _search_products_by_name_only_sync,
            self._db_path,
            query,
            limit,
        )

    async def search_products_by_code_exact(
        self,
        code: str,
        limit: int = DEFAULT_LIMIT,
    ) -> list[ProductMatch]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _search_products_by_code_exact_sync,
            self._db_path,
            code,
            limit,
        )

    async def search_products_by_price_exact(
        self,
        price: float,
        limit: int = DEFAULT_LIMIT,
    ) -> list[ProductMatch]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _search_products_by_price_exact_sync,
            self._db_path,
            price,
            limit,
        )

    async def insert_or_update_product(
        self,
        code: str,
        name: str,
        price: float,
        updated_at: str | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        ts = updated_at if updated_at is not None else _utc_now_iso()
        await loop.run_in_executor(
            None,
            _insert_or_update_product_sync,
            self._db_path,
            code,
            name,
            float(price),
            ts,
        )

    async def insert_or_update_products(
        self,
        items: list[tuple[str, str, float]],
        updated_at: str | None = None,
    ) -> None:
        if not items:
            return
        loop = asyncio.get_running_loop()
        ts = updated_at if updated_at is not None else _utc_now_iso()
        await loop.run_in_executor(None, _insert_or_update_products_sync, self._db_path, items, ts)


# Convenience for scripts that don't want the class:
def utc_now_iso() -> str:
    return _utc_now_iso()
