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


def _format_match_line(code: str, name: str, retail_price: float) -> str:
    retail_str = f"{retail_price:.2f}"
    return f"{code} — {name} — {retail_str}"


def _format_match_line_admin(
    code: str,
    name: str,
    retail_price: float,
    purchase_price: float | None,
) -> str:
    retail_str = f"{retail_price:.2f}"
    if purchase_price is None:
        return f"{code} — {name} — {retail_str}"
    purchase_str = f"{purchase_price:.2f}"
    return f"{code} — {name} — {retail_str}({purchase_str})"


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
    effective_admin: dict[int, bool] = {}

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
            await message.answer("Доступ заборонено")
            return False
        return True

    async def _require_admin(message: Message) -> bool:
        role_tag = await _get_access_role_tag(message.from_user.id)
        if role_tag != "admin":
            await message.answer("Доступ заборонено")
            return False
        return True

    def _cache_key(mode: str, query: str, is_admin: bool) -> str:
        role = "admin" if is_admin else "user"
        return f"{role}:{mode}:{query}"

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

    async def _require_admin_or_reply(message: Message) -> bool:
        return await _require_admin(message)

    def _role_line(role_tag: str | None) -> str:
        if role_tag is None:
            return "нема доступу"
        if role_tag == "admin":
            return "admin"
        if role_tag == "allowed":
            return "allowed"
        return str(role_tag)

    async def _who_role(message: Message) -> str:
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        return _role_line(role_tag)

    @router.message(Command("whoami"))
    async def _whoami(message: Message) -> None:
        if not await _require_admin_or_reply(message):
            return
        role_tag = await _get_access_role_tag(message.from_user.id)
        await message.answer(f"Ваш статус: {_role_line(role_tag)}")

    @router.message(Command("help"))
    async def _help(message: Message) -> None:
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        if role_tag is None:
            await message.answer("Доступ заборонено")
            return

        base_lines = [
            "Команди:",
            "/start — початок",
            "/help — ця підказка",
            "",
            "Пошук:",
            "Просто введіть текст: Код / Назва / Ціна (через кнопки).",
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
            await message.answer("\n".join(base_lines + admin_lines))
            return

        # allowed
        await message.answer(
            "\n".join(
                base_lines
                + [
                    "Ви: allowed",
                    "",
                    "Доступні тільки для admin:",
                    "/reload",
                    "/grant /revoke /grant_admin /revoke_admin",
                    "/admins /allowed /whoami",
                ],
            )
        )

    @router.message(Command("as_user"))
    async def _as_user(message: Message) -> None:
        if not await _require_admin(message):
            return
        uid = message.from_user.id
        effective_admin[uid] = False
        await message.answer("Готово. Відповіді будуть як у user (без purchase_price).")

    @router.message(Command("as_admin"))
    async def _as_admin(message: Message) -> None:
        if not await _require_admin(message):
            return
        uid = message.from_user.id
        effective_admin[uid] = True
        await message.answer("Готово. Відповіді будуть як у admin (з purchase_price).")

    @router.message(Command("admins"))
    async def _admins(message: Message) -> None:
        if not await _require_admin_or_reply(message):
            return
        ids = await products_db.list_users_by_role("admin")
        if not ids:
            await message.answer("Адмінів нема.")
            return
        await message.answer("Адміни: " + ", ".join(str(i) for i in ids))

    @router.message(Command("allowed"))
    async def _allowed(message: Message) -> None:
        if not await _require_admin_or_reply(message):
            return
        ids = await products_db.list_users_by_role("allowed")
        if not ids:
            await message.answer("Allowed нема.")
            return
        await message.answer("Allowed: " + ", ".join(str(i) for i in ids))

    @router.message(Command("grant"))
    async def _grant_allowed(message: Message) -> None:
        if not await _require_admin_or_reply(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await message.answer("Вкажіть user_id або відповісте на повідомлення користувача. Напр: /grant 123456")
            return
        await products_db.upsert_user_role(user_id=target_id, role="allowed", is_active=True)
        await message.answer(f"OK: user {target_id} додано в allowed.")

    @router.message(Command("revoke"))
    async def _revoke_user(message: Message) -> None:
        if not await _require_admin_or_reply(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await message.answer("Вкажіть user_id або відповісте на повідомлення користувача. Напр: /revoke 123456")
            return
        await products_db.set_user_active(user_id=target_id, is_active=False)
        await message.answer(f"OK: user {target_id} відключено (is_active=0).")

    @router.message(Command("grant_admin"))
    async def _grant_admin(message: Message) -> None:
        if not await _require_admin_or_reply(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await message.answer("Вкажіть user_id або відповісте на повідомлення користувача. Напр: /grant_admin 123456")
            return
        await products_db.upsert_user_role(user_id=target_id, role="admin", is_active=True)
        await message.answer(f"OK: user {target_id} зроблено admin.")

    @router.message(Command("revoke_admin"))
    async def _revoke_admin(message: Message) -> None:
        """
        Безпечний варіант: вимикає доступ повністю (не переводить автоматично в allowed).
        """
        if not await _require_admin_or_reply(message):
            return
        target_id = _extract_target_user_id(message)
        if target_id is None:
            await message.answer("Вкажіть user_id або відповісте на повідомлення користувача. Напр: /revoke_admin 123456")
            return
        await products_db.set_user_active(user_id=target_id, is_active=False)
        await message.answer(f"OK: user {target_id} відключено (admin знято, is_active=0).")

    @router.message(Command("start"))
    async def _start(message: Message) -> None:
        if not await _require_allowed(message):
            return
        uid = message.from_user.id
        role_tag = await _get_access_role_tag(uid)
        user_mode[uid] = MODE_NAME
        effective_admin[uid] = (role_tag == "admin")
        await _send_main_hint(message, MODE_NAME)

    @router.message(Command("reload"))
    async def _reload(message: Message) -> None:
        if not await _require_admin(message):
            return
        cache.clear()
        # Keep modes as-is
        await message.answer("Ок. Кеш очищено.")

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
        role_tag = await _get_access_role_tag(uid)
        if role_tag is None:
            await message.answer("Доступ заборонено")
            return

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
        # Try to delete the user's message so the mobile soft keyboard folds back
        # and results occupy the screen.
        try:
            await message.delete()
        except Exception:
            pass

        kb = _make_keyboard(active_mode)
        is_admin = role_tag == "admin" and effective_admin.get(uid, True)
        key = _cache_key(active_mode, text, is_admin)

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

            if is_admin:
                response_lines = [
                    _format_match_line_admin(m.code, m.name, m.price, m.purchase_price)
                    for m in matches
                ]
            else:
                response_lines = [_format_match_line(m.code, m.name, m.price) for m in matches]
            response = "\n".join(response_lines)

            cache.set(key, response)
            await message.answer(response, reply_markup=kb)

        except Exception:
            await message.answer("Помилка під час пошуку. Спробуйте ще раз.", reply_markup=kb)
