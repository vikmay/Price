from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.db import ProductsDB
from scripts.import_html import import_file as import_products_from_html

router: Final[Router] = Router(name="handlers")


@dataclass(frozen=True)
class CacheEntry:
    text: str
    expires_at: float


class SimpleTTLCache:
    def __init__(self, ttl_seconds: int = 30, max_items: int = 200) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_items = max_items
        self._items: dict[str, CacheEntry] = {}

    def get(self, key: str) -> str | None:
        entry = self._items.get(key)
        if not entry:
            return None
        if time.time() >= entry.expires_at:
            self._items.pop(key, None)
            return None
        return entry.text

    def set(self, key: str, value: str) -> None:
        if len(self._items) >= self._max_items:
            now = time.time()
            expired_keys = [k for k, v in self._items.items() if v.expires_at <= now]
            if expired_keys:
                for k in expired_keys[: max(1, len(expired_keys) // 2)]:
                    self._items.pop(k, None)
            else:
                self._items.pop(next(iter(self._items), ""), None)

        self._items[key] = CacheEntry(text=value, expires_at=time.time() + self._ttl_seconds)

    def clear(self) -> None:
        self._items.clear()


def _format_match_line(code: str, name: str, price: float) -> str:
    price_str = f"{price:.2f}"
    return f"Код: {code} — {name} — {price_str} грн"


async def _download_document_to_path(message: Message, destination: Path) -> None:
    doc = message.document
    if not doc:
        return

    file_name = (doc.file_name or "").lower()
    suffix = ".htm" if file_name.endswith(".htm") else (".html" if file_name.endswith(".html") else "")

    tg_file = await message.bot.get_file(doc.file_id)
    dest = destination
    if suffix and dest.suffix.lower() != suffix:
        dest = dest.with_suffix(suffix)

    dest.parent.mkdir(parents=True, exist_ok=True)
    await message.bot.download_file(tg_file.file_path, destination=dest)


def _is_admin_user(message: Message) -> bool:
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if not admin_id:
        return False
    try:
        return str(message.from_user.id) == admin_id
    except Exception:
        return False


_PRICE_EXTRACT_RE = re.compile(
    r"(?P<price>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
)


def _parse_price_exact(text: str) -> float | None:
    """
    Parse price from user input to match SQLite exact float equality.
    Accepts decimal comma/dot and optional thousands separators.
    """
    if not text:
        return None

    match = _PRICE_EXTRACT_RE.search(text)
    if not match:
        return None

    raw = match.group("price").strip()

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
        if raw.count(",") == 1 and raw.count(".") == 0:
            raw = raw.replace(",", ".")

    try:
        return float(raw)
    except ValueError:
        return None


def register_handlers(products_db: ProductsDB) -> None:
    cache = SimpleTTLCache(ttl_seconds=30, max_items=300)

    # Mode keys (internal)
    MODE_CODE = "code"
    MODE_NAME = "name"
    MODE_PRICE = "price"

    # Button base labels (display without ✅)
    BTN_CODE = "Код"
    BTN_NAME = "Назва"
    BTN_PRICE = "Ціна"

    # For parsing button clicks / edited texts
    def _normalize_button_text(text: str) -> str:
        return (text or "").replace("✅", "").strip()

    def _make_keyboard(active_mode: str | None) -> ReplyKeyboardMarkup:
        def maybe_check(base: str, mode_key: str) -> str:
            return f"{base} ✅" if active_mode == mode_key else base

        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text=maybe_check(BTN_CODE, MODE_CODE)),
                    KeyboardButton(text=maybe_check(BTN_NAME, MODE_NAME)),
                    KeyboardButton(text=maybe_check(BTN_PRICE, MODE_PRICE)),
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    user_mode: dict[int, str] = {}

    def _cache_key(mode: str, query: str) -> str:
        return f"{mode}:{query}"

    async def _send_main_hint(message: Message, active_mode: str) -> None:
        kb = _make_keyboard(active_mode)
        await message.answer(
            "Оберіть режим пошуку нижче або введіть запит.\n"
            "• Код\n"
            "• Назва\n"
            "• Ціна",
            reply_markup=kb,
        )

    async def _answer_not_found(message: Message, active_mode: str) -> None:
        kb = _make_keyboard(active_mode)
        await message.answer("Товар не знайдено", reply_markup=kb)

    @router.message(Command("start"))
    async def _start(message: Message) -> None:
        user_mode[message.from_user.id] = MODE_NAME
        await _send_main_hint(message, MODE_NAME)

    @router.message(Command("reload"))
    async def _reload(message: Message) -> None:
        if not _is_admin_user(message):
            return
        cache.clear()
        # Keep modes as-is
        await message.answer("Ок. Кеш очищено.")

    @router.message(F.document)
    async def _document_import(message: Message) -> None:
        if not _is_admin_user(message):
            return

        doc = message.document
        if not doc:
            return

        file_name = (doc.file_name or "").lower()
        if not (file_name.endswith(".htm") or file_name.endswith(".html")):
            return

        ts = int(time.time())
        dest_path = Path("data") / f"uploaded_catalog_{ts}.htm"

        active_mode = user_mode.get(message.from_user.id, MODE_NAME)
        kb = _make_keyboard(active_mode)

        try:
            await message.answer("Ок. Завантажую файл і оновлюю базу...", reply_markup=kb)
            await _download_document_to_path(message, destination=dest_path)

            loop = asyncio.get_running_loop()

            def _do_import() -> int:
                return import_products_from_html(
                    str(dest_path),
                    products_db.db_path,
                    clear_db=True,  # clear=2: DROP TABLE
                )

            imported_count = await loop.run_in_executor(None, _do_import)
            cache.clear()
            await message.answer(f"Імпорт завершено. Записів: {imported_count}", reply_markup=kb)
        except Exception:
            cache.clear()
            await message.answer("Сталася помилка при імпорті. Спробуйте ще раз.", reply_markup=kb)

    @router.message(F.text)
    async def _text_search(message: Message) -> None:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return

        uid = message.from_user.id
        active_mode = user_mode.get(uid, MODE_NAME)

        # Button click handling (text buttons)
        normalized = _normalize_button_text(text)
        if normalized in (BTN_CODE, BTN_NAME, BTN_PRICE):
            if normalized == BTN_CODE:
                active_mode = MODE_CODE
            elif normalized == BTN_NAME:
                active_mode = MODE_NAME
            else:
                active_mode = MODE_PRICE

            user_mode[uid] = active_mode
            kb = _make_keyboard(active_mode)

            if active_mode == MODE_CODE:
                await message.answer("Введіть код (число):", reply_markup=kb)
            elif active_mode == MODE_NAME:
                await message.answer("Введіть назву товару:", reply_markup=kb)
            else:
                await message.answer("Введіть точну ціну (наприклад 390 або 390,00):", reply_markup=kb)
            return

        # Query handling
        kb = _make_keyboard(active_mode)
        key = _cache_key(active_mode, text)

        cached = cache.get(key)
        if cached is not None:
            await message.answer(cached, reply_markup=kb)
            return

        try:
            if active_mode == MODE_CODE:
                matches = await products_db.search_products_by_code_exact(text, limit=20)
            elif active_mode == MODE_PRICE:
                price = _parse_price_exact(text)
                if price is None:
                    await message.answer("Невірний формат ціни. Спробуйте ще раз.", reply_markup=kb)
                    return
                matches = await products_db.search_products_by_price_exact(price, limit=20)
            else:
                matches = await products_db.search_products_by_name_only(text, limit=20)

            if not matches:
                await _answer_not_found(message, active_mode)
                return

            response_lines = [_format_match_line(m.code, m.name, m.price) for m in matches]
            response = "\n".join(response_lines)

            cache.set(key, response)
            await message.answer(response, reply_markup=kb)

        except Exception:
            await message.answer("Помилка під час пошуку. Спробуйте ще раз.", reply_markup=kb)
