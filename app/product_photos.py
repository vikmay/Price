from __future__ import annotations

import asyncio
import html
import io
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Final
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import aiohttp
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

MAX_UPLOAD_BYTES: Final = 20 * 1024 * 1024
MAX_RESPONSE_BYTES: Final = 1024 * 1024
MAX_SELECTIONS: Final = 20
SELECTION_TTL: Final = 600
FLOW_TTL: Final = 600
MAX_FLOWS: Final = 500
MAX_BUFFERED_BYTES: Final = 64 * 1024 * 1024
CALLBACK_PREFIX: Final = "pp:"
IMAGE_MIME: Final = ("image/jpeg", "image/png", "image/webp")
IMAGE_SUFFIX: Final = (".jpg", ".jpeg", ".png", ".webp")
PHOTO_BUTTON: Final = "Додати фото"
CANCEL_BUTTON: Final = "Скасувати"
RETRY_BUTTON: Final = "Повторити надсилання"
SAVE_BUTTON: Final = "Зберегти фото"
MAX_ALBUM_ITEMS: Final = 10
MAX_RECEIPTS: Final = 500


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PHOTO_BUTTON)]], resize_keyboard=True, is_persistent=True
    )


def _step_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_BUTTON)]], resize_keyboard=True, is_persistent=True
    )


def _pending_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=RETRY_BUTTON)], [KeyboardButton(text=CANCEL_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _album_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SAVE_BUTTON)], [KeyboardButton(text=CANCEL_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


class _CommandOrButton(Command):
    def __init__(self, command: str, button: str) -> None:
        super().__init__(command)
        self._button = button

    async def __call__(self, message: Message, bot: Bot) -> bool | CommandObject:
        if (message.text or "").strip() == self._button:
            return True
        return await super().__call__(message, bot)


@dataclass(frozen=True)
class PhotoProduct:
    c3_id: int
    code: str
    name: str
    price: float
    source: str | None
    generation: str | None


def _state_sync(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    try:
        row = conn.execute(
            "SELECT source, recovery_generation FROM c3_price_sync WHERE singleton=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    if row is None:
        return None, None
    return row[0], row[1]


def _search_sync(db_path: str, query: str, limit: int) -> list[PhotoProduct]:
    text = " ".join((query or "").split())
    if not text:
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        source, generation = _state_sync(conn)
        rows: list[tuple] = []
        if text.isdigit():
            rows = conn.execute(
                "SELECT c3_id,code,name,price FROM products"
                " WHERE c3_id IS NOT NULL AND code=? ORDER BY name_cf LIMIT ?",
                (text, limit),
            ).fetchall()
        if len(rows) < limit:
            rows += conn.execute(
                "SELECT c3_id,code,name,price FROM products"
                " WHERE c3_id IS NOT NULL AND (code || ' ' || name_cf) LIKE ? ORDER BY name_cf LIMIT ?",
                (f"%{text.casefold()}%", limit - len(rows)),
            ).fetchall()
    products: list[PhotoProduct] = []
    seen: set[int] = set()
    for row in rows:
        c3_id = int(row[0])
        if c3_id in seen:
            continue
        seen.add(c3_id)
        products.append(PhotoProduct(c3_id, str(row[1]), str(row[2]), float(row[3]), source, generation))
    return products


def _product_sync(db_path: str, c3_id: int) -> PhotoProduct | None:
    with closing(sqlite3.connect(db_path)) as conn:
        source, generation = _state_sync(conn)
        row = conn.execute(
            "SELECT c3_id,code,name,price FROM products WHERE c3_id=?", (c3_id,)
        ).fetchone()
    if row is None:
        return None
    return PhotoProduct(int(row[0]), str(row[1]), str(row[2]), float(row[3]), source, generation)


def _gallery_version(data: dict) -> str:
    photo = data.get("photo")
    if not isinstance(photo, dict):
        return ""
    for key in ("galleryVersion", "version"):
        value = photo.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class PhotoUploadError(Exception):
    def __init__(self, message: str, status: int = 0, conflict: bool = False, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.conflict = conflict
        self.retryable = retryable


def validate_base_url(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.scheme == "https" and parts.hostname:
        return value.rstrip("/")
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return value.rstrip("/")
    return None


def resolve_base_url() -> str | None:
    return validate_base_url(os.getenv("PRICE_C3_URL"))


class PhotoClient:
    def __init__(self, base: str, key: str, source: str, generation: str, user_id: int) -> None:
        self._base = base
        self._key = key
        self._source = source
        self._generation = generation
        self._user = user_id

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self._key,
            "X-Price-Source": self._source,
            "X-C3-Generation": self._generation,
            "X-Price-User": str(self._user),
        }
        if extra:
            headers.update(extra)
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict:
        timeout = aiohttp.ClientTimeout(total=60, sock_connect=15, sock_read=45)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    self._base + path,
                    headers=self._headers(extra),
                    data=data,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    body = bytearray()
                    while len(body) <= MAX_RESPONSE_BYTES:
                        chunk = await response.content.read(65536)
                        if not chunk:
                            break
                        body += chunk
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise PhotoUploadError("Немає зв'язку з C3 Trade. Натисніть «Повторити надсилання».", retryable=True)
        if status in (301, 302, 303, 307, 308):
            raise PhotoUploadError("Сервер C3 Trade перенаправляє запит. Перевірте адресу.", status=status)
        if status in (401, 403):
            raise PhotoUploadError("C3 Trade відхилив доступ бота. Зверніться до адміністратора.", status=status)
        if status == 404:
            raise PhotoUploadError("Товар не знайдено на сервері C3 Trade.", status=status)
        if status == 409:
            raise PhotoUploadError(
                "Товар або фото вже змінилися. Натисніть «Додати фото» і виберіть товар повторно.",
                status=status,
                conflict=True,
            )
        if status == 413:
            raise PhotoUploadError("Фото завелике для C3 Trade.", status=status)
        if status == 429 or status >= 500:
            raise PhotoUploadError(
                "C3 Trade тимчасово недоступний. Натисніть «Повторити надсилання».", status=status, retryable=True
            )
        if status != 200:
            raise PhotoUploadError("C3 Trade не прийняв фото. Спробуйте пізніше.", status=status)
        def bad(message: str) -> PhotoUploadError:
            suffix = f" Натисніть «{RETRY_BUTTON}»." if method == "POST" else ""
            return PhotoUploadError(message + suffix, status=status, retryable=method == "POST")

        if len(body) > MAX_RESPONSE_BYTES:
            raise bad("C3 Trade повернув завелику відповідь.")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise bad("C3 Trade повернув неочікувану відповідь.")
        if not isinstance(parsed, dict):
            raise bad("C3 Trade повернув неочікувану відповідь.")
        return parsed

    async def metadata(self, c3_id: int) -> dict:
        data = await self._send("GET", f"/api/photos/price/{c3_id}")
        if data.get("gallerySupported") is not True:
            raise PhotoUploadError("C3 Trade не підтримує галерею фото. Оновіть сервер.")
        identity = data.get("identity")
        try:
            parsed = UUID(str(identity))
        except (ValueError, AttributeError, TypeError):
            raise PhotoUploadError("C3 Trade не повернув ідентифікатор товару. Спробуйте пізніше.")
        if parsed.int == 0:
            raise PhotoUploadError("C3 Trade не повернув ідентифікатор товару. Спробуйте пізніше.")
        if data.get("recoveryGeneration") != self._generation:
            raise PhotoUploadError(
                "Каталог C3 змінився. Натисніть «Додати фото» і виберіть товар повторно.", conflict=True
            )
        return data

    async def upload(
        self, c3_id: int, identity: str, request_id: str, data: bytes, gallery_version: str = ""
    ) -> dict:
        extra = {
            "X-Price-Identity": identity,
            "X-Photo-Request": request_id,
            "X-Photo-Mode": "append",
            "X-Photo-Version": "",
            "X-Photo-Gallery-Version": gallery_version,
            "Content-Type": "application/octet-stream",
        }
        result = await self._send("POST", f"/api/photos/price/{c3_id}", data=data, extra=extra)
        if result.get("success") is not True:
            raise PhotoUploadError(
                "C3 Trade не підтвердив збереження фото. Натисніть «Повторити надсилання».", retryable=True
            )
        return result


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, chunk) -> int:
        if self.tell() + len(chunk) > self._limit:
            raise PhotoUploadError("Фото має бути не більше 20 МБ.")
        return super().write(chunk)


@dataclass
class Selection:
    token: str
    user_id: int
    chat_id: int
    product: PhotoProduct
    identity: str
    generation: str
    expires_at: float
    gallery_version: str = ""


@dataclass
class Pending:
    request_id: str
    data: bytes
    selection: Selection
    expires_at: float


@dataclass
class AlbumItem:
    message_id: int
    file_id: str
    file_size: int | None
    request_id: str
    data: bytes | None = None
    done: bool = False
    failed: bool = False


@dataclass
class Album:
    group_id: str
    selection: Selection
    expires_at: float
    items: dict[int, AlbumItem] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    started: bool = False


@dataclass(frozen=True)
class SendResult:
    outcome: str
    message: str = ""
    version: str = ""


@dataclass
class Flow:
    user_id: int
    chat_id: int
    mode: str = ""
    selections: dict[str, Selection] = field(default_factory=dict)
    chosen: Selection | None = None
    pending: Pending | None = None
    album: Album | None = None
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _receipt_key(user_id: int, chat_id: int, group_id: str) -> str:
    return f"{user_id}:{chat_id}:{group_id}"


class PhotoFlows:
    def __init__(self) -> None:
        self._flows: dict[int, Flow] = {}
        self._receipts: dict[str, tuple[float, str]] = {}
        self._bytes = 0

    def prune(self) -> None:
        now = time.time()
        for group_id in [
            group_id for group_id, (expires_at, _) in self._receipts.items() if expires_at <= now
        ]:
            self._receipts.pop(group_id, None)
        for user_id in [
            uid
            for uid, flow in self._flows.items()
            if flow.expires_at <= now and not flow.lock.locked()
        ]:
            self.clear(user_id)

    def receipt(self, user_id: int, chat_id: int, group_id: str) -> str | None:
        key = _receipt_key(user_id, chat_id, group_id)
        entry = self._receipts.get(key)
        if entry is None:
            return None
        if entry[0] <= time.time():
            self._receipts.pop(key, None)
            return None
        return entry[1]

    def mark_receipt(self, user_id: int, chat_id: int, group_id: str, status: str) -> None:
        if not group_id:
            return
        key = _receipt_key(user_id, chat_id, group_id)
        self.prune()
        if len(self._receipts) >= MAX_RECEIPTS:
            oldest = min(self._receipts, key=lambda item: self._receipts[item][0])
            self._receipts.pop(oldest, None)
        self._receipts[key] = (time.time() + FLOW_TTL, status)

    def begin(self, user_id: int, chat_id: int) -> Flow | None:
        self.prune()
        flow = self._flows.get(user_id)
        if flow is not None and flow.chat_id != chat_id:
            if flow.lock.locked():
                return None
            self.clear(user_id)
            flow = None
        if flow is None:
            if len(self._flows) >= MAX_FLOWS:
                idle = [uid for uid, item in self._flows.items() if not item.lock.locked()]
                if idle:
                    self.clear(min(idle, key=lambda uid: self._flows[uid].expires_at))
            flow = Flow(user_id=user_id, chat_id=chat_id)
            flow.expires_at = time.time() + FLOW_TTL
            self._flows[user_id] = flow
        return flow

    def get(self, user_id: int) -> Flow | None:
        flow = self._flows.get(user_id)
        if flow is None:
            return None
        if flow.expires_at <= time.time() and not flow.lock.locked():
            self.clear(user_id)
            return None
        return flow

    def cancel(self, user_id: int) -> bool:
        flow = self._flows.get(user_id)
        if flow is not None and flow.lock.locked():
            return False
        self.clear(user_id)
        return True

    def clear(self, user_id: int, receipt: str = "cancelled") -> None:
        flow = self._flows.pop(user_id, None)
        if flow is None:
            return
        if flow.pending is not None:
            self._bytes = max(0, self._bytes - len(flow.pending.data))
        if flow.album is not None:
            for item in flow.album.items.values():
                if item.data is not None:
                    self._bytes = max(0, self._bytes - len(item.data))
                    item.data = None
            self.mark_receipt(
                flow.album.selection.user_id, flow.album.selection.chat_id, flow.album.group_id, receipt
            )

    def clear_all(self) -> None:
        self._flows.clear()
        self._receipts.clear()
        self._bytes = 0

    def reserve(self, size: int) -> bool:
        if self._bytes + size > MAX_BUFFERED_BYTES:
            return False
        self._bytes += size
        return True

    def release(self, size: int) -> None:
        self._bytes = max(0, self._bytes - size)

    def drop_pending(self, flow: Flow) -> None:
        if flow.pending is not None:
            self.release(len(flow.pending.data))
            flow.pending = None


flows: Final[PhotoFlows] = PhotoFlows()


def register_photo_handlers(
    router: Router,
    products_db,
    role_of: Callable[[int], Awaitable[str | None]],
) -> None:
    reply_remove = ReplyKeyboardRemove()

    async def _answer(message: Message, text: str, markup=None) -> None:
        await message.answer(text, reply_markup=reply_remove if markup is None else markup)

    def _key_problem() -> str | None:
        key = (os.getenv("PRICE_SYNC_KEY") or "").strip()
        if not key:
            return "Фото недоступні: не задано PRICE_SYNC_KEY на сервері бота."
        if not 32 <= len(key) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in key):
            return "Фото недоступні: PRICE_SYNC_KEY має некоректний формат (32–512 символів без керівних символів)."
        return None

    def _url_problem() -> str | None:
        raw = (os.getenv("PRICE_C3_URL") or "").strip()
        if not raw:
            return "Фото недоступні: не задано PRICE_C3_URL на сервері бота."
        if validate_base_url(raw) is None:
            return "Фото недоступні: PRICE_C3_URL має некоректний формат (потрібен HTTPS без облікових даних)."
        return None

    def _catalog_problem(product: PhotoProduct) -> str | None:
        if not product.source:
            return "Фото недоступні: каталог Price не має джерела C3. Потрібен новий повний прайс із C3 Trade."
        if product.generation is None:
            return "Фото недоступні: немає покоління відновлення C3. Оновіть сервер C3 Trade і дочекайтеся нового повного прайсу."
        return None

    def _diagnose(product: PhotoProduct | None) -> str:
        for problem in (_url_problem(), _key_problem(), _catalog_problem(product) if product else None):
            if problem:
                return problem
        return ""

    def _client(flow: Flow, product: PhotoProduct) -> PhotoClient | None:
        base = resolve_base_url()
        key = (os.getenv("PRICE_SYNC_KEY") or "").strip()
        if base is None or _key_problem() is not None or _catalog_problem(product) is not None:
            return None
        return PhotoClient(base, key, product.source, product.generation, flow.user_id)

    async def _hold(lock: asyncio.Lock) -> bool:
        if lock.locked():
            return False
        await lock.acquire()
        return True

    def _current(flow: Flow, selection: Selection) -> bool:
        return flows.get(flow.user_id) is flow and flow.chosen is selection

    def _album_open(album: Album | None) -> bool:
        if album is None:
            return False
        return any(not item.done and not item.failed for item in album.items.values())

    async def _search(message: Message, flow: Flow, query: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            products = await loop.run_in_executor(
                None, _search_sync, products_db.db_path, query, MAX_SELECTIONS
            )
        except Exception:
            if flows.get(flow.user_id) is flow:
                await _answer(message, "Помилка під час пошуку. Спробуйте ще раз.")
            return
        if flows.get(flow.user_id) is not flow:
            return
        if not products:
            await _answer(message, "Товар не знайдено", main_menu())
            return
        problem = _diagnose(products[0])
        if problem:
            await _answer(message, problem, main_menu())
            return
        flow.selections.clear()
        now = time.time()
        buttons: list[list[InlineKeyboardButton]] = []
        for product in products:
            token = secrets.token_urlsafe(12)
            flow.selections[token] = Selection(
                token, flow.user_id, flow.chat_id, product, "", product.generation or "", now + SELECTION_TTL
            )
            suffix = f" · ID {product.c3_id}"
            label = f"{product.code} {product.name}"[: max(0, 60 - len(suffix))] + suffix
            buttons.append(
                [InlineKeyboardButton(text=label, callback_data=CALLBACK_PREFIX + token)]
            )
        flow.mode = "select"
        flow.expires_at = now + FLOW_TTL
        await message.answer(
            "Виберіть товар для фото:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )

    async def _download(message: Message, file_id: str) -> bytes:
        try:
            tg_file = await message.bot.get_file(file_id)
        except Exception:
            raise PhotoUploadError("Не вдалося отримати файл із Telegram. Спробуйте ще раз.", retryable=True)
        if tg_file.file_size is not None and tg_file.file_size > MAX_UPLOAD_BYTES:
            raise PhotoUploadError("Фото має бути не більше 20 МБ.")
        if not tg_file.file_path:
            raise PhotoUploadError("Telegram не повернув файл. Спробуйте ще раз.", retryable=True)
        buffer = _BoundedBuffer(MAX_UPLOAD_BYTES)
        try:
            await message.bot.download_file(tg_file.file_path, destination=buffer)
        except PhotoUploadError:
            raise
        except Exception:
            raise PhotoUploadError("Не вдалося завантажити фото з Telegram. Спробуйте ще раз.", retryable=True)
        data = buffer.getvalue()
        if not data:
            raise PhotoUploadError("Порожній файл фото.")
        return data

    async def _send(
        message: Message, flow: Flow, pending: Pending, finalize: bool = True
    ) -> SendResult:
        selection = pending.selection
        product = selection.product
        if not _current(flow, selection):
            return SendResult("stale")
        try:
            loop = asyncio.get_running_loop()
            current = await loop.run_in_executor(
                None, _product_sync, products_db.db_path, product.c3_id
            )
        except Exception:
            if _current(flow, selection) and finalize:
                await _answer(message, "Помилка читання каталогу. Натисніть «Повторити надсилання».", _pending_menu())
            return SendResult("retry", "Помилка читання каталогу.")
        if not _current(flow, selection):
            return SendResult("stale")
        if current is None or current.source != product.source or current.generation != product.generation:
            flows.drop_pending(flow)
            flow.chosen = None
            flow.mode = "search"
            if finalize:
                await _answer(
                    message,
                    "Каталог C3 змінився. Натисніть «Додати фото» і виберіть товар повторно.",
                    main_menu(),
                )
            return SendResult("conflict", "Каталог C3 змінився.")
        client = _client(flow, product)
        if client is None:
            flows.drop_pending(flow)
            if finalize:
                await _answer(message, _diagnose(product), main_menu())
            return SendResult("fatal", _diagnose(product))
        if (await role_of(flow.user_id)) is None:
            flows.clear(flow.user_id)
            if finalize:
                await _answer(message, "Доступ заборонено")
            return SendResult("denied", "Доступ заборонено")
        if not _current(flow, selection):
            return SendResult("stale")
        try:
            result = await client.upload(
                product.c3_id, selection.identity, pending.request_id, pending.data,
                selection.gallery_version,
            )
        except PhotoUploadError as ex:
            if not _current(flow, selection):
                return SendResult("stale")
            if ex.conflict:
                flows.drop_pending(flow)
                flow.chosen = None
                flow.mode = "search"
                if finalize:
                    await _answer(message, ex.message, main_menu())
                return SendResult("conflict", ex.message)
            if ex.retryable:
                if finalize:
                    await _answer(message, ex.message, _pending_menu())
                return SendResult("retry", ex.message)
            flows.drop_pending(flow)
            if finalize:
                await _answer(message, ex.message, main_menu())
            return SendResult("fatal", ex.message)
        if not _current(flow, selection):
            return SendResult("stale")
        version = _gallery_version(result)
        if version:
            selection.gallery_version = version
        if finalize:
            flows.clear(flow.user_id, "saved")
            await message.answer(
                f"Фото додано: {html.escape(product.code)} {html.escape(product.name)}",
                parse_mode="HTML",
                reply_markup=main_menu(),
            )
        return SendResult("ok", "", version)

    async def _accept(message: Message, flow: Flow, file_id: str, file_size: int | None) -> None:
        if not await _hold(flow.lock):
            await _answer(message, "Зачекайте: попереднє фото ще обробляється.")
            return
        try:
            if flows.get(flow.user_id) is not flow:
                await _answer(message, "Вибір товару втрачено. Натисніть «Додати фото».", main_menu())
                return
            if flow.pending is not None and flow.pending.expires_at > time.time():
                await _answer(
                    message,
                    "Попереднє фото ще не збережено. Натисніть «Повторити надсилання» або «Скасувати».",
                    _pending_menu(),
                )
                return
            if _album_open(flow.album):
                await _answer(
                    message,
                    "Спочатку натисніть «Зберегти фото» або «Скасувати» для альбому.",
                    _album_menu(),
                )
                return
            flows.drop_pending(flow)
            selection = flow.chosen
            if selection is None or selection.expires_at <= time.time():
                flow.chosen = None
                await _answer(message, "Вибір товару застарів. Натисніть «Додати фото».", main_menu())
                return
            if file_size is not None and file_size > MAX_UPLOAD_BYTES:
                await _answer(message, "Фото має бути не більше 20 МБ.", _step_menu())
                return
            if not flows.reserve(MAX_UPLOAD_BYTES):
                await _answer(message, "Зачекайте: одночасно обробляється забагато фото.", _step_menu())
                return
            try:
                data = await _download(message, file_id)
            except PhotoUploadError as ex:
                await _answer(message, ex.message, _step_menu())
                return
            finally:
                flows.release(MAX_UPLOAD_BYTES)
            if not _current(flow, selection):
                await _answer(message, "Вибір товару змінився. Натисніть «Додати фото».", main_menu())
                return
            if (await role_of(flow.user_id)) is None:
                flows.clear(flow.user_id)
                await _answer(message, "Доступ заборонено")
                return
            if not flows.reserve(len(data)):
                await _answer(message, "Зачекайте: одночасно обробляється забагато фото.", _step_menu())
                return
            pending = Pending(str(uuid4()), data, selection, time.time() + FLOW_TTL)
            flow.pending = pending
            flow.mode = "upload"
            flow.expires_at = time.time() + FLOW_TTL
            await _send(message, flow, pending)
        finally:
            flow.lock.release()

    async def _collect(
        message: Message, flow: Flow, file_id: str, file_size: int | None, group_id: str
    ) -> None:
        text = ""
        markup = None
        async with flow.lock:
            if flows.get(flow.user_id) is not flow:
                text, markup = "Вибір товару втрачено. Натисніть «Додати фото».", main_menu()
            elif flows.receipt(flow.user_id, flow.chat_id, group_id) is not None:
                return
            else:
                selection = flow.chosen
                album = flow.album
                if selection is None or selection.expires_at <= time.time():
                    flow.chosen = None
                    text, markup = "Вибір товару застарів. Натисніть «Додати фото».", main_menu()
                elif album is not None and (album.group_id != group_id or album.selection is not selection):
                    text, markup = (
                        "Альбом уже збирається для іншого вибору. Натисніть «Скасувати».",
                        _album_menu(),
                    )
                elif album is not None and message.message_id in album.items:
                    return
                elif album is not None and album.started:
                    text, markup = (
                        "Альбом уже зберігається. Дочекайтеся завершення «Зберегти фото».",
                        _album_menu(),
                    )
                elif album is None and flow.pending is not None and flow.pending.expires_at > time.time():
                    text, markup = (
                        "Попереднє фото ще не збережено. Натисніть «Повторити надсилання» або «Скасувати».",
                        _pending_menu(),
                    )
                else:
                    if album is None:
                        flows.drop_pending(flow)
                        album = Album(group_id, selection, time.time() + FLOW_TTL)
                        flow.album = album
                        flow.mode = "album"
                    if len(album.items) >= MAX_ALBUM_ITEMS:
                        text, markup = "В альбомі може бути не більше 10 фото.", _album_menu()
                    else:
                        album.items[message.message_id] = AlbumItem(
                            message.message_id, file_id, file_size, str(uuid4())
                        )
                        album.order.append(message.message_id)
                        album.expires_at = time.time() + FLOW_TTL
                        flow.expires_at = time.time() + FLOW_TTL
                        text, markup = (
                            f"Отримано {len(album.items)} фото з альбому."
                            f" Натисніть «{SAVE_BUTTON}», щоб зберегти їх у галерею товару.",
                            _album_menu(),
                        )
        if text:
            await _answer(message, text, markup)

    async def _drain(message: Message, flow: Flow, album: Album) -> None:
        selection = album.selection
        product = selection.product
        album.started = True
        saved = sum(1 for item in album.items.values() if item.done)
        total = len(album.order)
        problem = ""
        outcome = "ok"
        for message_id in sorted(album.order):
            item = album.items.get(message_id)
            if item is None or item.done or item.failed:
                continue
            if not _current(flow, selection):
                outcome = "stale"
                break
            if item.data is None:
                if item.file_size is not None and item.file_size > MAX_UPLOAD_BYTES:
                    item.failed = True
                    problem = "Фото має бути не більше 20 МБ."
                    continue
                if not flows.reserve(MAX_UPLOAD_BYTES):
                    problem = "Зачекайте: одночасно обробляється забагато фото."
                    outcome = "retry"
                    break
                try:
                    data = await _download(message, item.file_id)
                except PhotoUploadError as ex:
                    problem = ex.message
                    if ex.retryable:
                        outcome = "retry"
                    else:
                        item.failed = True
                        continue
                    break
                finally:
                    flows.release(MAX_UPLOAD_BYTES)
                if not flows.reserve(len(data)):
                    problem = "Зачекайте: одночасно обробляється забагато фото."
                    outcome = "retry"
                    break
                item.data = data
            result = await _send(
                message,
                flow,
                Pending(item.request_id, item.data, selection, time.time() + FLOW_TTL),
                finalize=False,
            )
            if result.outcome == "ok":
                saved += 1
                item.done = True
                flows.release(len(item.data))
                item.data = None
                if not result.version and _album_open(album):
                    problem = (
                        "C3 Trade не повернув версію галереї."
                        " Натисніть «Додати фото» і виберіть товар повторно."
                    )
                    outcome = "conflict"
                    break
                continue
            outcome = result.outcome
            problem = result.message
            break
        if outcome == "stale":
            return
        if outcome == "denied":
            await _answer(message, f"Збережено {saved} із {total} фото. {problem}")
            return
        if outcome == "ok" and saved == total:
            flows.clear(flow.user_id, "saved")
            await message.answer(
                f"Додано {saved} фото: {html.escape(product.code)} {html.escape(product.name)}",
                parse_mode="HTML",
                reply_markup=main_menu(),
            )
            return
        if outcome in ("conflict", "fatal") or not _album_open(album):
            flows.clear(flow.user_id, "saved" if saved else "cancelled")
            await _answer(message, f"Збережено {saved} із {total} фото. {problem}", main_menu())
            return
        await _answer(
            message,
            f"Збережено {saved} із {total} фото. {problem}",
            _pending_menu(),
        )

    @router.message(_CommandOrButton("save", SAVE_BUTTON))
    async def _save(message: Message) -> None:
        if message.chat.type != "private":
            await _answer(message, "Команда доступна лише в приватному чаті.")
            return
        flow = flows.get(message.from_user.id)
        if flow is None or flow.album is None:
            await _answer(message, "Немає незавершеного альбому.", main_menu())
            return
        if (await role_of(message.from_user.id)) is None:
            await _answer(message, "Доступ заборонено")
            return
        if not await _hold(flow.lock):
            await _answer(message, "Зачекайте: фото вже надсилається.", _album_menu())
            return
        try:
            if flows.get(message.from_user.id) is not flow or flow.album is None:
                await _answer(message, "Немає незавершеного альбому.", main_menu())
                return
            await _drain(message, flow, flow.album)
        finally:
            flow.lock.release()

    @router.message(_CommandOrButton("photo", PHOTO_BUTTON))
    async def _photo_command(message: Message, command: CommandObject | None = None) -> None:
        if message.chat.type != "private":
            await _answer(message, "Фото можна додавати лише в приватному чаті.")
            return
        if (await role_of(message.from_user.id)) is None:
            await _answer(message, "Доступ заборонено")
            return
        flow = flows.begin(message.from_user.id, message.chat.id)
        if flow is None or not await _hold(flow.lock):
            await _answer(message, "Зачекайте: попереднє фото ще надсилається.", _pending_menu())
            return
        try:
            if flows.get(message.from_user.id) is not flow:
                await _answer(message, "Зачекайте: попереднє фото ще надсилається.", _pending_menu())
                return
            if _album_open(flow.album):
                await _answer(
                    message,
                    "Спочатку натисніть «Зберегти фото» або «Скасувати» для альбому.",
                    _album_menu(),
                )
                return
            flows.drop_pending(flow)
            flow.mode = "search"
            flow.chosen = None
            flow.selections.clear()
            flow.expires_at = time.time() + FLOW_TTL
            query = (command.args or "").strip() if command is not None else ""
            if query:
                await _search(message, flow, query)
                return
            await _answer(
                message,
                "Введіть код або назву товару для фото. Натисніть «Скасувати», щоб скасувати.",
                _step_menu(),
            )
        finally:
            flow.lock.release()

    @router.message(_CommandOrButton("cancel", CANCEL_BUTTON))
    async def _cancel(message: Message) -> None:
        if message.chat.type != "private":
            await _answer(message, "Команда доступна лише в приватному чаті.")
            return
        if not flows.cancel(message.from_user.id):
            await _answer(message, "Зачекайте: фото вже надсилається. Спробуйте «Скасувати» пізніше.", _pending_menu())
            return
        await _answer(message, "Скасовано.", main_menu())

    @router.message(_CommandOrButton("retry", RETRY_BUTTON))
    async def _retry(message: Message) -> None:
        if message.chat.type != "private":
            await _answer(message, "Команда доступна лише в приватному чаті.")
            return
        flow = flows.get(message.from_user.id)
        if flow is None or (flow.pending is None and not _album_open(flow.album)):
            await _answer(message, "Немає незавершеного завантаження.", main_menu())
            return
        if (await role_of(message.from_user.id)) is None:
            await _answer(message, "Доступ заборонено")
            return
        if not await _hold(flow.lock):
            await _answer(message, "Зачекайте: фото вже надсилається.", _pending_menu())
            return
        try:
            if flows.get(message.from_user.id) is not flow:
                await _answer(message, "Немає незавершеного завантаження.", main_menu())
                return
            pending = flow.pending
            if _album_open(flow.album):
                await _drain(message, flow, flow.album)
                return
            if pending is None:
                await _answer(message, "Немає незавершеного завантаження.", main_menu())
                return
            await _send(message, flow, pending)
        finally:
            flow.lock.release()

    @router.callback_query(F.data.startswith(CALLBACK_PREFIX))
    async def _select(callback: CallbackQuery) -> None:
        message = callback.message
        if message is None or message.chat.type != "private":
            await callback.answer("Недоступно.", show_alert=True)
            return
        if (await role_of(callback.from_user.id)) is None:
            await callback.answer("Доступ заборонено", show_alert=True)
            return
        flow = flows.get(callback.from_user.id)
        selection = flow.selections.get(callback.data[len(CALLBACK_PREFIX):]) if flow else None
        if selection is None or selection.expires_at <= time.time() or selection.chat_id != message.chat.id:
            await callback.answer("Вибір застарів. Натисніть «Додати фото».", show_alert=True)
            return
        if not await _hold(flow.lock):
            await callback.answer("Зачекайте: фото вже надсилається.", show_alert=True)
            return
        try:
            if flows.get(callback.from_user.id) is not flow:
                await callback.answer("Вибір застарів. Натисніть «Додати фото».", show_alert=True)
                return
            if flow.selections.get(selection.token) is not selection or selection.expires_at <= time.time():
                await callback.answer("Вибір застарів. Натисніть «Додати фото».", show_alert=True)
                return
            if flow.pending is not None or flow.mode == "upload" or _album_open(flow.album):
                await callback.answer("Фото вже надсилається. Зачекайте.", show_alert=True)
                return
            flow.selections = {selection.token: selection}
            await callback.answer()
            product = selection.product
            client = _client(flow, product)
            if client is None:
                await _answer(message, _diagnose(product), main_menu())
                return
            try:
                data = await client.metadata(product.c3_id)
            except PhotoUploadError as ex:
                if flows.get(callback.from_user.id) is flow:
                    await _answer(message, ex.message)
                return
            if flows.get(callback.from_user.id) is not flow:
                return
            if flow.selections.get(selection.token) is not selection:
                return
            chosen = Selection(
                selection.token, selection.user_id, selection.chat_id, product,
                str(data["identity"]), data["recoveryGeneration"], time.time() + SELECTION_TTL,
                _gallery_version(data),
            )
            flow.selections[chosen.token] = chosen
            flow.chosen = chosen
            flow.mode = "upload"
            flow.expires_at = time.time() + FLOW_TTL
            await _answer(
                message,
                f"Надішліть фото товару {product.code} {product.name}, прикріпивши його через Telegram."
                f" Можна надіслати один файл або альбом до {MAX_ALBUM_ITEMS} фото: після альбому"
                f" натисніть «{SAVE_BUTTON}», щоб зберегти всі фото. Натисніть «{CANCEL_BUTTON}», щоб скасувати.",
                _step_menu(),
            )
        finally:
            flow.lock.release()

    @router.message(F.photo)
    async def _photo_message(message: Message) -> None:
        if message.chat.type != "private":
            raise SkipHandler
        group_id = message.media_group_id
        if group_id and flows.receipt(message.from_user.id, message.chat.id, group_id) is not None:
            return
        flow = flows.get(message.from_user.id)
        if flow is None or flow.chosen is None:
            raise SkipHandler
        if (await role_of(message.from_user.id)) is None:
            await _answer(message, "Доступ заборонено")
            return
        photo = message.photo[-1]
        if group_id:
            await _collect(message, flow, photo.file_id, photo.file_size, group_id)
            return
        await _accept(message, flow, photo.file_id, photo.file_size)

    @router.message(F.document)
    async def _photo_document(message: Message) -> None:
        document = message.document
        if document is None or message.chat.type != "private":
            raise SkipHandler
        mime = (document.mime_type or "").lower()
        name = (document.file_name or "").lower()
        if mime not in IMAGE_MIME and not name.endswith(IMAGE_SUFFIX):
            raise SkipHandler
        group_id = message.media_group_id
        if group_id and flows.receipt(message.from_user.id, message.chat.id, group_id) is not None:
            return
        flow = flows.get(message.from_user.id)
        if flow is None or flow.chosen is None:
            raise SkipHandler
        if (await role_of(message.from_user.id)) is None:
            await _answer(message, "Доступ заборонено")
            return
        if group_id:
            await _collect(message, flow, document.file_id, document.file_size, group_id)
            return
        await _accept(message, flow, document.file_id, document.file_size)

    @router.message(F.text)
    async def _photo_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text or text.startswith("/") or message.chat.type != "private":
            raise SkipHandler
        if text in (PHOTO_BUTTON, CANCEL_BUTTON, RETRY_BUTTON, SAVE_BUTTON):
            raise SkipHandler
        flow = flows.get(message.from_user.id)
        if flow is None or flow.mode != "search":
            raise SkipHandler
        if (await role_of(message.from_user.id)) is None:
            await _answer(message, "Доступ заборонено")
            return
        if not await _hold(flow.lock):
            await _answer(message, "Зачекайте: фото вже надсилається.")
            return
        try:
            if flows.get(message.from_user.id) is not flow or flow.mode != "search":
                return
            await _search(message, flow, text)
        finally:
            flow.lock.release()
