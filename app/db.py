from __future__ import annotations

import asyncio
import os
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
    purchase_price: float | None
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
                purchase_price REAL,
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

        if "purchase_price" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN purchase_price REAL;")

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

        # Access control table for bot users (admin/allowed).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_access (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_access_role
            ON user_access(role);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_access_role_active
            ON user_access(role, is_active);
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


def _row_to_match(row: sqlite3.Row) -> ProductMatch:
    purchase_price: float | None
    if row["purchase_price"] is None:
        purchase_price = None
    else:
        purchase_price = float(row["purchase_price"])

    return ProductMatch(
        code=str(row["code"]),
        name=str(row["name"]),
        price=float(row["price"]),
        purchase_price=purchase_price,
        updated_at=str(row["updated_at"]),
    )


def _search_products_by_code_exact_sync(db_path: str, code: str, limit: int) -> list[ProductMatch]:
    clean_code = _normalize_code(code)
    if not clean_code:
        return []

    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            SELECT code, name, price, purchase_price, updated_at
            FROM products
            WHERE code = ?
            ORDER BY updated_at DESC
            LIMIT ?;
            """,
            (clean_code, limit),
        )
        return [_row_to_match(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _search_products_by_price_exact_sync(db_path: str, price: float, limit: int) -> list[ProductMatch]:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            SELECT code, name, price, purchase_price, updated_at
            FROM products
            WHERE price = ?
            ORDER BY updated_at DESC
            LIMIT ?;
            """,
            (float(price), limit),
        )
        return [_row_to_match(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _search_products_by_name_only_sync(db_path: str, query: str, limit: int) -> list[ProductMatch]:
    q = " ".join((query or "").strip().split())
    if not q:
        return []

    q_cf = _casefold_str(q)
    prefix_like = f"{q_cf}%"
    substring_like = f"%{q_cf}%"

    tokens = [t for t in q_cf.split() if t]
    is_single_token = len(tokens) == 1

    NO_MATCH = "§§§§§"
    if is_single_token:
        whole_word_prefix_like = f"{q_cf} %"
        whole_word_middle_like = f"% {q_cf} %"
        whole_word_suffix_like = f"% {q_cf}"
    else:
        whole_word_prefix_like = f"%{NO_MATCH}%"
        whole_word_middle_like = f"%{NO_MATCH}%"
        whole_word_suffix_like = f"%{NO_MATCH}%"

    conn = _get_conn(db_path)
    try:
        rows: list[ProductMatch] = []
        existing_codes: set[str] = set()

        cur = conn.execute(
            """
            SELECT code, name, price, purchase_price, updated_at
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
            rows.append(_row_to_match(row))

        if len(rows) < limit:
            remaining = limit - len(rows)
            extra = remaining * 5

            cur2 = conn.execute(
                """
                SELECT code, name, price, purchase_price, updated_at
                FROM products
                WHERE name_cf LIKE ?
                ORDER BY
                    CASE
                        WHEN name_cf = ? THEN 0
                        WHEN name_cf LIKE ? THEN 1
                        WHEN name_cf LIKE ? THEN 1
                        WHEN name_cf LIKE ? THEN 1
                        WHEN name_cf LIKE ? THEN 2
                        ELSE 3
                    END,
                    name_cf
                LIMIT ?;
                """,
                (
                    substring_like,
                    q_cf,
                    whole_word_prefix_like,
                    whole_word_middle_like,
                    whole_word_suffix_like,
                    prefix_like,
                    extra,
                ),
            )
            for row in cur2.fetchall():
                code = str(row["code"])
                if code in existing_codes:
                    continue
                existing_codes.add(code)
                rows.append(_row_to_match(row))
                if len(rows) >= limit:
                    break

        if not rows:
            tokens = [t for t in q_cf.split() if t]
            if len(tokens) >= 2:
                params = [f"%{t}%" for t in tokens]

                where_and = " AND ".join(["name_cf LIKE ?"] * len(tokens))
                cur3 = conn.execute(
                    f"""
                    SELECT code, name, price, purchase_price, updated_at
                    FROM products
                    WHERE {where_and}
                    LIMIT ?;
                    """,
                    (*params, limit),
                )
                for row in cur3.fetchall():
                    code = str(row["code"])
                    if code in existing_codes:
                        continue
                    existing_codes.add(code)
                    rows.append(_row_to_match(row))
                    if len(rows) >= limit:
                        break

                if not rows:
                    where_or = " OR ".join(["name_cf LIKE ?"] * len(tokens))
                    cur4 = conn.execute(
                        f"""
                        SELECT code, name, price, purchase_price, updated_at
                        FROM products
                        WHERE {where_or}
                        LIMIT ?;
                        """,
                        (*params, limit),
                    )
                    for row in cur4.fetchall():
                        code = str(row["code"])
                        if code in existing_codes:
                            continue
                        existing_codes.add(code)
                        rows.append(_row_to_match(row))
                        if len(rows) >= limit:
                            break

        return rows
    finally:
        conn.close()


def _search_products_sync(db_path: str, query: str, limit: int) -> list[ProductMatch]:
    """
    Combined search (code exact if possible, then name search).
    Not used in handlers currently, but kept consistent.
    """
    q = query.strip()
    if not q:
        return []

    conn = _get_conn(db_path)
    try:
        if _looks_like_code(q):
            cur = conn.execute(
                """
                SELECT code, name, price, purchase_price, updated_at
                FROM products
                WHERE code = ?
                ORDER BY updated_at DESC
                LIMIT ?;
                """,
                (q, limit),
            )
            rows = [_row_to_match(row) for row in cur.fetchall()]
            if rows:
                return rows

        q_cf = _casefold_str(q)
        prefix_like = f"{q_cf}%"
        substring_like = f"%{q_cf}%"

        rows: list[ProductMatch] = []
        existing_codes: set[str] = set()

        cur = conn.execute(
            """
            SELECT code, name, price, purchase_price, updated_at
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
            rows.append(_row_to_match(row))

        if len(rows) < limit:
            remaining = limit - len(rows)
            extra = remaining * 5

            cur2 = conn.execute(
                """
                SELECT code, name, price, purchase_price, updated_at
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
                rows.append(_row_to_match(row))
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
    purchase_price: float | None,
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

        if clean_code:
            cur = conn.execute("SELECT id FROM products WHERE code = ? LIMIT 1;", (clean_code,))
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO products (code, name, name_cf, price, purchase_price, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (clean_code, clean_name, clean_name_cf, float(price), purchase_price, updated_at),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                    SET code = ?, name = ?, name_cf = ?, price = ?, purchase_price = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (
                        clean_code,
                        clean_name,
                        clean_name_cf,
                        float(price),
                        purchase_price,
                        updated_at,
                        int(row["id"]),
                    ),
                )
        else:
            cur = conn.execute("SELECT id FROM products WHERE name_cf = ? LIMIT 1;", (clean_name_cf,))
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO products (code, name, name_cf, price, purchase_price, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    ("", clean_name, clean_name_cf, float(price), purchase_price, updated_at),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                    SET name = ?, name_cf = ?, price = ?, purchase_price = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (
                        clean_name,
                        clean_name_cf,
                        float(price),
                        purchase_price,
                        updated_at,
                        int(row["id"]),
                    ),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_or_update_products_sync(
    db_path: str,
    items: list[tuple[str, str, float, float | None]],
    updated_at: str,
) -> None:
    """
    items: list of (code, name, retail_price, purchase_price)
    """
    if not items:
        return

    conn = _get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")

        select_id_by_code = conn.execute
        select_id_by_name_cf = conn.execute

        insert_sql = """
            INSERT INTO products (code, name, name_cf, price, purchase_price, updated_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """
        update_sql_by_code = """
            UPDATE products
            SET code = ?, name = ?, name_cf = ?, price = ?, purchase_price = ?, updated_at = ?
            WHERE id = ?;
        """
        update_sql_by_name_cf = """
            UPDATE products
            SET name = ?, name_cf = ?, price = ?, purchase_price = ?, updated_at = ?
            WHERE id = ?;
        """

        for code, name, retail_price, purchase_price in items:
            clean_name = " ".join((name or "").split())
            if not clean_name:
                continue

            clean_name_cf = _casefold_str(clean_name)
            clean_code = _normalize_code(code)

            if clean_code:
                cur = select_id_by_code("SELECT id FROM products WHERE code = ? LIMIT 1;", (clean_code,))
                row = cur.fetchone()
                if row is None:
                    conn.execute(
                        insert_sql,
                        (
                            clean_code,
                            clean_name,
                            clean_name_cf,
                            float(retail_price),
                            purchase_price,
                            updated_at,
                        ),
                    )
                else:
                    conn.execute(
                        update_sql_by_code,
                        (
                            clean_code,
                            clean_name,
                            clean_name_cf,
                            float(retail_price),
                            purchase_price,
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
                    conn.execute(
                        insert_sql,
                        (
                            "",
                            clean_name,
                            clean_name_cf,
                            float(retail_price),
                            purchase_price,
                            updated_at,
                        ),
                    )
                else:
                    conn.execute(
                        update_sql_by_name_cf,
                        (
                            clean_name,
                            clean_name_cf,
                            float(retail_price),
                            purchase_price,
                            updated_at,
                            int(row["id"]),
                        ),
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
        return await loop.run_in_executor(None, _search_products_by_name_only_sync, self._db_path, query, limit)

    async def search_products_by_code_exact(
        self,
        code: str,
        limit: int = DEFAULT_LIMIT,
    ) -> list[ProductMatch]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _search_products_by_code_exact_sync, self._db_path, code, limit)

    async def search_products_by_price_exact(
        self,
        price: float,
        limit: int = DEFAULT_LIMIT,
    ) -> list[ProductMatch]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _search_products_by_price_exact_sync, self._db_path, price, limit)

    async def insert_or_update_product(
        self,
        code: str,
        name: str,
        price: float,
        purchase_price: float | None = None,
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
            purchase_price,
            ts,
        )

    async def insert_or_update_products(
        self,
        items: list[tuple[str, str, float, float | None]],
        updated_at: str | None = None,
    ) -> None:
        if not items:
            return
        loop = asyncio.get_running_loop()
        ts = updated_at if updated_at is not None else _utc_now_iso()
        await loop.run_in_executor(None, _insert_or_update_products_sync, self._db_path, items, ts)

    async def get_user_role(self, user_id: int) -> str | None:
        """
        Returns role for user if present, else None.
        Role can be: "admin" or "allowed".
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_user_role_sync, self._db_path, int(user_id))

    async def is_user_active(self, user_id: int) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _is_user_active_sync, self._db_path, int(user_id))

    async def upsert_user_role(self, user_id: int, role: str, is_active: bool = True) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _upsert_user_role_sync,
            self._db_path,
            int(user_id),
            str(role),
            1 if is_active else 0,
            _utc_now_iso(),
        )

    async def set_user_active(self, user_id: int, is_active: bool) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _set_user_active_sync,
            self._db_path,
            int(user_id),
            1 if is_active else 0,
            _utc_now_iso(),
        )

    async def delete_user_access(self, user_id: int) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _delete_user_access_sync, self._db_path, int(user_id))

    async def any_admin_exists(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _any_admin_exists_sync, self._db_path)

    async def list_users_by_role(self, role: str) -> list[int]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _list_users_by_role_sync, self._db_path, str(role))


def _get_user_role_sync(db_path: str, user_id: int) -> str | None:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("SELECT role FROM user_access WHERE user_id = ? LIMIT 1;", (user_id,))
        row = cur.fetchone()
        if row is None:
            return None
        role = row["role"]
        return str(role) if role is not None else None
    finally:
        conn.close()


def _is_user_active_sync(db_path: str, user_id: int) -> bool:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT is_active FROM user_access WHERE user_id = ? LIMIT 1;",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        return int(row["is_active"]) == 1
    finally:
        conn.close()


def _upsert_user_role_sync(db_path: str, user_id: int, role: str, is_active: int, updated_at: str) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO user_access (user_id, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                role = excluded.role,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at;
            """,
            (user_id, role, is_active, updated_at, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def _set_user_active_sync(db_path: str, user_id: int, is_active: int, updated_at: str) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            UPDATE user_access
            SET is_active = ?, updated_at = ?
            WHERE user_id = ?;
            """,
            (is_active, updated_at, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_user_access_sync(db_path: str, user_id: int) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute("DELETE FROM user_access WHERE user_id = ?;", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _any_admin_exists_sync(db_path: str) -> bool:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("SELECT 1 FROM user_access WHERE role = ? AND is_active = 1 LIMIT 1;", ("admin",))
        row = cur.fetchone()
        return row is not None
    finally:
        conn.close()


def _list_users_by_role_sync(db_path: str, role: str) -> list[int]:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id FROM user_access WHERE role = ? AND is_active = 1 ORDER BY user_id ASC;",
            (role,),
        )
        return [int(r["user_id"]) for r in cur.fetchall()]
    finally:
        conn.close()


def utc_now_iso() -> str:
    return _utc_now_iso()
