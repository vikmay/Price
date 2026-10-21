"""Local-only receiver for the C3 integration regression; never polls Telegram."""
import sqlite3
import sys
from contextlib import closing

from aiohttp import web
from app.db import connect
from app.price_sync import create_app

path, port = sys.argv[1:]
connect(path)
app = create_app(path, "integration-only-" + "x" * 32, lambda: None)


async def inspect(request):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT c3_id,code,name,price,purchase_price FROM products ORDER BY c3_id")]
    return web.json_response(rows)


app.router.add_get("/test/products", inspect)
web.run_app(app, host="127.0.0.1", port=int(port), access_log=None)
