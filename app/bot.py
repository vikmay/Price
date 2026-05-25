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
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def main() -> None:
    setup_logging()

    # Smoke mode: verify wiring without requiring TELEGRAM_BOT_TOKEN and without polling.
    if os.getenv("BOT_SMOKE") == "1":
        db_path = os.getenv("DB_PATH", os.path.join("data", "products.db"))
        connect(db_path)
        products_db = ProductsDB(db_path)
        register_handlers(products_db)
        logging.info("Bot smoke check OK (BOT_SMOKE=1).")
        return

    settings = load_settings()

    connect(settings.DB_PATH)

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    products_db = ProductsDB(settings.DB_PATH)
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
