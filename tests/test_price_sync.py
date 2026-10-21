from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from app.db import connect, ProductsDB
from app.price_sync import apply_batch, create_app, ensure_schema
from scripts.configure_c3_sync import configure

KEY = "test-only-" + "x" * 40
SOURCE = "a03f57b6-4670-4cee-afaf-8dbb105ef901"


def product(pid=1, code="001", price=10):
    return dict(id=pid, code=code, name="Фарба Київ", price=price, purchase_price=0, deleted=False)


def batch(sequence=1, full=True, products=None):
    return dict(source=SOURCE, sequence=sequence, full=full, products=[product()] if products is None else products)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "products.db")
        connect(self.db)
        ensure_schema(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute("SELECT c3_id,code,name,price,purchase_price FROM products ORDER BY c3_id").fetchall()

    async def test_snapshot_delta_rename_delete_duplicates_and_access(self):
        users = ProductsDB(self.db)
        await users.upsert_user_role(user_id=123, role="admin", is_active=True)
        await users.insert_or_update_products([("old", "Старий HTML", 15, None)])
        apply_batch(self.db, batch(products=[product(), product(2)]))
        self.assertEqual(len(self.rows()), 2)  # Same historical code is preserved.
        self.assertEqual(self.rows()[0][1], "001")
        self.assertEqual(await users.get_user_role(123), "admin")
        renamed = product(price=0); renamed["name"] = "Нова назва"; renamed["code"] = "0002"
        apply_batch(self.db, batch(2, False, [renamed, dict(id=2, deleted=True)]))
        self.assertEqual(self.rows(), [(1, "0002", "Нова назва", 0.0, 0.0)])
        matches = await users.search_products_unified("нова", limit=20)
        self.assertEqual(matches[0].code, "0002")
        apply_batch(self.db, batch(3, True, []))
        self.assertEqual(self.rows(), [])
        self.assertEqual(await users.get_user_role(123), "admin")

    async def test_retry_stale_sequence_restart_and_source(self):
        apply_batch(self.db, batch(10))
        self.assertFalse(apply_batch(self.db, batch(10)))
        self.assertFalse(apply_batch(self.db, batch(9, False, [product(price=999)])))
        ensure_schema(self.db)  # Simulate restart; cursor survives.
        self.assertFalse(apply_batch(self.db, batch(8)))
        other = batch(11); other["source"] = "b03f57b6-4670-4cee-afaf-8dbb105ef901"
        with self.assertRaises(ValueError):
            apply_batch(self.db, other)
        self.assertEqual(self.rows()[0][3], 10)

    async def test_invalid_snapshot_does_not_destroy_prices_or_cursor(self):
        with self.assertRaises(ValueError):
            apply_batch(self.db, batch(1, False))
        apply_batch(self.db, batch())
        malformed = batch(2, True, [product(2), product(3)])
        malformed["products"][1]["price"] = float("nan")
        with self.assertRaises(ValueError):
            apply_batch(self.db, malformed)
        duplicate = batch(2, True, [product(), product()])
        with self.assertRaises(ValueError):
            apply_batch(self.db, duplicate)
        self.assertEqual(len(self.rows()), 1)
        # Database error after delete also rolls back all changes and the cursor.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_test BEFORE INSERT ON products BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            apply_batch(self.db, batch(2))
        self.assertEqual(len(self.rows()), 1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT sequence FROM c3_price_sync").fetchone()[0], 1)

    async def test_http_auth_and_cache_invalidation(self):
        # Production Oracle VM uses Python 3.8, which has no asyncio.to_thread.
        legacy_api = patch("asyncio.to_thread", None, create=True)
        legacy_api.start()
        self.addCleanup(legacy_api.stop)
        invalidations = []
        app = create_app(self.db, KEY, lambda: invalidations.append(True))
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/c3/prices", json=batch())
            self.assertEqual(response.status, 401)
            response = await client.post("/api/c3/prices", json=batch(), headers={"Authorization": "Bearer wrong"})
            self.assertEqual(response.status, 401)
            for _ in range(2):
                response = await client.post("/api/c3/prices", json=batch(), headers={"Authorization": "Bearer " + KEY})
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["sequence"], 1)
            self.assertEqual(len(invalidations), 1)
            response = await client.post("/api/c3/prices", data="not-json", headers={"Authorization": "Bearer " + KEY})
            self.assertEqual(response.status, 400)

    async def test_configuration_preserves_bot_settings_and_key(self):
        env = Path(self.temp.name) / ".env"
        env.write_text("TELEGRAM_BOT_TOKEN=placeholder\nDB_PATH=data/existing.db\n# note\n", encoding="utf-8")
        first = configure(env)
        second = configure(env)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        self.assertIn("TELEGRAM_BOT_TOKEN=placeholder\nDB_PATH=data/existing.db\n# note", env.read_text(encoding="utf-8"))
        self.assertEqual(env.read_text(encoding="utf-8").count("PRICE_SYNC_KEY="), 1)


if __name__ == "__main__":
    unittest.main()
