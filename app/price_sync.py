"""Authenticated push receiver. Products and delivery cursor commit atomically."""
from __future__ import annotations

import asyncio
import hmac
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import UUID

from aiohttp import web


def ensure_schema(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        if "c3_id" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN c3_id INTEGER")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_c3_id ON products(c3_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS c3_price_sync (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), source TEXT NOT NULL,
            sequence INTEGER NOT NULL, updated_at TEXT NOT NULL)""")


def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Expected object")
    source = payload.get("source")
    if not isinstance(source, str) or str(UUID(source)) != source:
        raise ValueError("Invalid source")
    sequence = payload.get("sequence")
    if type(sequence) is not int or not 0 < sequence < 2**63:
        raise ValueError("Invalid sequence")
    if type(payload.get("full")) is not bool:
        raise ValueError("Invalid full flag")
    products = payload.get("products")
    if not isinstance(products, list) or len(products) > 200_000:
        raise ValueError("Invalid products")
    seen = set()
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("Invalid product")
        pid = product.get("id")
        if type(pid) is not int or not -(2**31) <= pid < 2**31 or pid in seen:
            raise ValueError("Invalid or repeated product id")
        seen.add(pid)
        if type(product.get("deleted")) is not bool:
            raise ValueError("Invalid deleted flag")
        if product["deleted"]:
            continue
        # Historical codes/names need not be unique, non-empty, or positive.
        for field in ("code", "name"):
            if not isinstance(product.get(field), str) or len(product[field]) > 100_000:
                raise ValueError("Invalid product text")
        for field in ("price", "purchase_price"):
            value = product.get(field)
            if value is None and field == "purchase_price":
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid price")
    return payload


def apply_batch(db_path: str, payload: object) -> bool:
    data = validate_payload(payload)
    with closing(sqlite3.connect(db_path, timeout=30)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute("SELECT source,sequence FROM c3_price_sync WHERE singleton=1").fetchone()
        if state and state[0] != data["source"]:
            raise ValueError("Different C3 source; administrator must reconnect explicitly")
        if state and state[1] >= data["sequence"]:
            return False  # Retries and delayed requests cannot roll back newer prices.
        if not state and not data["full"]:
            raise ValueError("Initial full catalog required")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if data["full"]:
            conn.execute("DELETE FROM products")
        for product in data["products"]:
            if product["deleted"]:
                conn.execute("DELETE FROM products WHERE c3_id=?", (product["id"],))
                continue
            conn.execute("""INSERT INTO products(c3_id,code,name,name_cf,price,purchase_price,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(c3_id) DO UPDATE SET
                code=excluded.code,name=excluded.name,name_cf=excluded.name_cf,price=excluded.price,
                purchase_price=excluded.purchase_price,updated_at=excluded.updated_at""",
                (product["id"], product["code"], product["name"], product["name"].casefold(),
                 product["price"], product.get("purchase_price"), now))
        conn.execute("""INSERT INTO c3_price_sync VALUES(1,?,?,?) ON CONFLICT(singleton)
            DO UPDATE SET source=excluded.source,sequence=excluded.sequence,updated_at=excluded.updated_at""",
            (data["source"], data["sequence"], now))
    return True


def create_app(db_path: str, key: str, invalidate_cache) -> web.Application:
    if not 32 <= len(key) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("PRICE_SYNC_KEY must contain 32 to 512 characters")
    ensure_schema(db_path)
    app = web.Application(client_max_size=32 * 1024 * 1024)

    async def receive(request: web.Request) -> web.Response:
        expected = ("Bearer " + key).encode("utf-8")
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode("utf-8"), expected):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            payload = await request.json()
            applied = await asyncio.to_thread(apply_batch, db_path, payload)
        except (ValueError, TypeError, OverflowError):
            return web.json_response({"error": "Invalid catalog or source/sequence conflict"}, status=400)
        if applied:
            invalidate_cache()
        return web.json_response({"sequence": payload["sequence"], "applied": applied},
                                 headers={"Cache-Control": "no-store"})

    app.router.add_post("/api/c3/prices", receive)
    return app


async def start_receiver(db_path: str, invalidate_cache) -> web.AppRunner | None:
    key = os.getenv("PRICE_SYNC_KEY", "")
    if not key:
        return None  # Existing installations keep working without new settings.
    runner = web.AppRunner(create_app(db_path, key, invalidate_cache), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, os.getenv("PRICE_SYNC_HOST", "127.0.0.1"),
                          int(os.getenv("PRICE_SYNC_PORT", "8082")))
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
