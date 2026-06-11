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
from aiogram.types import Message, ReplyKeyboardRemove

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


def _format_match_line(code: str, name: str, retail_price: float) -> str:
    retail_str = f"{retail_price:.2f}"
    return f"{code} {name} — {retail_str}"


def _format_match_line_admin(
    code: str,
    name: str,
    retail_price: float,
    purchase_price: float | None,
) -> str:
    retail_str = f"{retail_price:.2f}"
    if purchase_price is None:
        return f"{code} {name} — {retail_str}"
    purchase_str = f"{purchase_price:.2f}"
    return f"{code} {name} — {retail_str}({purchase_str})"


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


def register_handlers(products_db: ProductsDB) -> None:
    cache = SimpleTTLCache(ttl_seconds=30, max_items=300)
    effective_admin: dict[int, bool] = {}

    reply_remove = ReplyKeyboardRemove()

    async def _answer(message: Message, text: str) -> None:
        await message.answer(text, reply_markup=reply_remove)

    async def _get_access_role_tag(uid: int) -> str | None:
        role = await products_db.get_user_role(uid)
        if role is None:
            return None
        if not await products_db.is_user_active(uid):
            return None

        role_str = str(role).lower()
        if role_str in ("admin", "allowed"):
            return role_str
        return None

    async def _require_allowed(message: Message) -> bool:
        role_tag = await _get_access_role_tag(message.from_user.id)
        if role_tag is None:
            await _answer(message, "Доступ заборонено")
            return False
        return True

    async def _require_admin(message: Message) -> bool:
        role_tag = await _get_access_role_tag(message.from_user.id)
        if role_tag != "admin":
            await _answer(message, "Доступ заборонено")
            return False
        return True

    def _cache_key(query: str, is_admin: bool) -> str:
        role = "admin" if is_admin else "user"
        return f"{role}:{query}"

    def _role_line(role_tag: str | None) -> str:
        if role_tag is None:
            return "нема доступу"
        if role_tag == "admin":
            return "admin"
        if role_tag == "allowed":
            return "allowed"
        return str(role_tag)

    def _extract_target_user_id(message: Message) -> int | None:
        """
        Supports:
          - /grant 123
          - /grant (as reply to user's message)
        """
        if message.reply_to_message and message.reply_to_message.from_user:
            return int(message.reply_to_message.from_user.id)

        text = message.text or ""
        parts = text.strip().split(maxsplit=1)
        if len(parts) != 2:
            return None

        try:
            return int(parts[1].strip())
        except ValueError:
            return None

    @router.message(Command("whoami"))
    async def _whoami(message: Message) -> None:
        if not await _require_admin(message):
            return
        role_tag = await _get_access_role_tag(message.from_user.id)
        await _answer(message, f"Ваш статус: {_role_line(role_tag)}")

    @router.message(Command("help"))
    async def _help(message: Message) -> None:
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        if role_tag is None:
            await _answer(message, "Доступ заборонено")
            return

        base_lines = [
            "Команди:",
            "/start — початок",
            "/help — ця підказка",
            "",
            "Пошук:",
            "Введіть код або назву товару.",
            "",
        ]

        if role_tag == "admin":
            admin_lines = [
                "Керування доступом (admin):",
                "/admins — список admin",
                "/allowed — список allowed",
                "/whoami — ваш статус",
                "",
                "/grant <user_id> — додати в allowed",
                "/revoke <user_id> — вимкнути доступ (is_active=0)",
                "/grant_admin <user_id> — зробити admin",
                "/revoke_admin <user_id> — вимкнути (admin знято)",
                "",
                "Адмін-перемикач видимості (щоб працювати як user):",
                "/as_user — показувати відповідь як user (без purchase_price)",
                "/as_admin — повернути admin-видимість",
                "",
                "Адміністраторські дії:",
                "/reload — очистити кеш",
            ]
            await _answer(message, "\n".join(base_lines + admin_lines))
            return

        await _answer(
            message,
            "\n".join(
                base_lines
                + [
                    "Ви: allowed",
                    "",
                    "Доступні тільки для admin:",
                    "/reload",
                    "/grant /revoke /grant_admin /revoke_admin",
                    "/admins /allowed /whoami",
                ]
            ),
        )

    @router.message(Command("as_user"))
    async def _as_user(message: Message) -> None:
        if not await _require_admin(message):
            return
        uid = message.from_user.id
        effective_admin[uid] = False
        await _answer(message, "Готово. Відповіді будуть як у user (без purchase_price).")

    @router.message(Command("as_admin"))
    async def _as_admin(message: Message) -> None:
        if not await _require_admin(message):
            return
        uid = message.from_user.id
        effective_admin[uid] = True
        await _answer(message, "Готово. Відповіді будуть як у admin (з purchase_price).")

    @router.message(Command("admins"))
    async def _admins(message: Message) -> None:
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        if role_tag is None or role_tag != "admin":
            await _answer(message, "Доступ заборонено")
            return

        ids = await products_db.list_users_by_role("admin")
        if not ids:
            await _answer(message, "Адмінів нема.")
            return
        await _answer(message, "Адміни: " + ", ".join(str(i) for i in ids))

    @router.message(Command("allowed"))
    async def _allowed(message: Message) -> None:
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        if role_tag is None or role_tag != "admin":
            await _answer(message, "Доступ заборонено")
            return

        ids = await products_db.list_users_by_role("allowed")
        if not ids:
            await _answer(message, "Allowed нема.")
            return
        await _answer(message, "Allowed: " + ", ".join(str(i) for i in ids))

    @router.message(Command("grant"))
    async def _grant_allowed(message: Message) -> None:
        if not await _require_admin(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await _answer(message, "Вкажіть user_id або відповісте на повідомлення користувача. Напр: /grant 123456")
            return
        await products_db.upsert_user_role(user_id=target_id, role="allowed", is_active=True)
        await _answer(message, f"OK: user {target_id} додано в allowed.")

    @router.message(Command("revoke"))
    async def _revoke_user(message: Message) -> None:
        if not await _require_admin(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await _answer(message, "Вкажіть user_id або відповісте на повідомлення користувача. Напр: /revoke 123456")
            return
        await products_db.set_user_active(user_id=target_id, is_active=False)
        await _answer(message, f"OK: user {target_id} відключено (is_active=0).")

    @router.message(Command("grant_admin"))
    async def _grant_admin(message: Message) -> None:
        if not await _require_admin(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await _answer(message, "Вкажіть user_id або відповісте на повідомлення користувача. Напр: /grant_admin 123456")
            return
        await products_db.upsert_user_role(user_id=target_id, role="admin", is_active=True)
        await _answer(message, f"OK: user {target_id} зроблено admin.")

    @router.message(Command("revoke_admin"))
    async def _revoke_admin(message: Message) -> None:
        if not await _require_admin(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await _answer(message, "Вкажіть user_id або відповісте на повідомлення користувача. Напр: /revoke_admin 123456")
            return
        await products_db.set_user_active(user_id=target_id, is_active=False)
        await _answer(message, f"OK: user {target_id} відключено (admin знято, is_active=0).")

    @router.message(Command("start"))
    async def _start(message: Message) -> None:
        if not await _require_allowed(message):
            return
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        effective_admin[uid] = (role_tag == "admin")
        await _answer(message, "Введіть код або назву товару.")

    @router.message(Command("reload"))
    async def _reload(message: Message) -> None:
        if not await _require_admin(message):
            return
        cache.clear()
        await _answer(message, "Ок. Кеш очищено.")

    @router.message(F.document)
    async def _document_import(message: Message) -> None:
        if not await _require_admin(message):
            return

        doc = message.document
        if not doc:
            return

        file_name = (doc.file_name or "").lower()
        if not (file_name.endswith(".htm") or file_name.endswith(".html")):
            return

        ts = int(time.time())
        dest_path = Path("data") / f"uploaded_catalog_{ts}.htm"

        try:
            await _answer(message, "Ок. Завантажую файл і оновлюю базу...")
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

            # Сповіщення всіх активних користувачів про оновлення цін
            users_to_notify: list[int] = []
            for role in ("admin", "allowed"):
                users_to_notify.extend(await products_db.list_users_by_role(role))
            for uid in users_to_notify:
                try:
                    await message.bot.send_message(uid, "Ціни оновлено ✅")
                except Exception:
                    pass

            await _answer(message, f"Імпорт завершено. Записів: {imported_count}")
        except Exception:
            cache.clear()
            await _answer(message, "Сталася помилка при імпорті. Спробуйте ще раз.")

    @router.message(F.text)
    async def _text_search(message: Message) -> None:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return

        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        if role_tag is None:
            await _answer(message, "Доступ заборонено")
            return

        # Try to delete the user's message so the mobile soft keyboard folds back
        # and results occupy the screen.
        try:
            await message.delete()
        except Exception:
            pass

        is_admin = role_tag == "admin" and effective_admin.get(uid, True)
        key = _cache_key(text, is_admin)

        cached = cache.get(key)
        if cached is not None:
            await message.answer(cached, reply_markup=reply_remove)
            return

        try:
            matches = await products_db.search_products_unified(text, limit=20)

            if not matches:
                await _answer(message, "Товар не знайдено")
                return

            if is_admin:
                response_lines = [
                    _format_match_line_admin(m.code, m.name, m.price, m.purchase_price)
                    for m in matches
                ]
            else:
                response_lines = [_format_match_line(m.code, m.name, m.price) for m in matches]

            response = "\n".join(response_lines)

            cache.set(key, response)
            await message.answer(response, reply_markup=reply_remove)

        except Exception:
            await _answer(message, "Помилка під час пошуку. Спробуйте ще раз.")
