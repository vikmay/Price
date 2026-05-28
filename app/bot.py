from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher

from app.config import load_settings
from app.db import ProductsDB, connect
from app.handlers import register_handlers, router


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Reduce aiogram per-update noise; keep only warnings/errors by default.
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)


async def _bootstrap_admin_if_needed(products_db: ProductsDB) -> None:
    """
    If DB has no admins, create the first admin from ADMIN_TELEGRAM_ID env.
    Ensures the bot is manageable without manual SQL.
    """
    admin_id_raw = os.getenv("ADMIN_TELEGRAM_ID")
    if not admin_id_raw:
        return

    try:
        admin_id = int(str(admin_id_raw).strip())
    except ValueError:
        return

    # Always ensure ADMIN_TELEGRAM_ID is admin (if set in env).
    await products_db.upsert_user_role(user_id=admin_id, role="admin", is_active=True)


async def main() -> None:
    setup_logging()

    # Smoke mode: verify wiring without requiring TELEGRAM_BOT_TOKEN and without polling.
    if os.getenv("BOT_SMOKE") == "1":
        db_path = os.getenv("DB_PATH", os.path.join("data", "products.db"))
        connect(db_path)
        products_db = ProductsDB(db_path)
        await _bootstrap_admin_if_needed(products_db)
        register_handlers(products_db)
        logging.info("Bot smoke check OK (BOT_SMOKE=1).")
        return

    settings = load_settings()

    connect(settings.DB_PATH)

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    products_db = ProductsDB(settings.DB_PATH)
    await _bootstrap_admin_if_needed(products_db)
    register_handlers(products_db)

    dp.include_router(router)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    # Windows-safe entrypoint.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
