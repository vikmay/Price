from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetFile, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    Message,
    PhotoSize,
    ReplyKeyboardRemove,
    Update,
    User,
)
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db import ProductsDB, connect
from app.handlers import register_handlers, router
from app.price_sync import apply_batch, ensure_schema
from app.product_photos import (
    MAX_UPLOAD_BYTES,
    PhotoClient,
    PhotoUploadError,
    flows,
    resolve_base_url,
)
from scripts.configure_c3_sync import configure

KEY = "photo-test-" + "k" * 40
SOURCE = "a03f57b6-4670-4cee-afaf-8dbb105ef901"
GENERATION = "gen-2026-11"
IDENTITY = "11111111-1111-1111-1111-111111111111"


class MockSession(BaseSession):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files
        self.sent: list[SendMessage] = []
        self.callback_answers: list[str] = []
        self.get_file_calls = 0
        self.gate: asyncio.Event | None = None

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetFile):
            self.get_file_calls += 1
            data = self.files[method.file_id]
            return File(
                file_id=method.file_id,
                file_unique_id=method.file_id,
                file_size=len(data),
                file_path="photos/" + method.file_id,
            )
        if isinstance(method, SendMessage):
            self.sent.append(method)
            return Message(
                message_id=len(self.sent),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            self.callback_answers.append(method.text or "")
            return True
        raise AssertionError(type(method).__name__)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        if self.gate is not None:
            await self.gate.wait()
        data = self.files[url.rsplit("/", 1)[-1]]
        for start in range(0, len(data), chunk_size):
            yield data[start:start + chunk_size]


class FakeC3:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.status = 200
        self.redirect = False
        self.identity = IDENTITY
        self.generation = GENERATION
        self.gallery_version = ""
        self.photo_version = ""
        self.payload: bytes | None = None
        self.images: list[dict] = []
        self.version_seq = 0
        self.strict_versions = False
        self.commit_then_fail = 0
        self.conflict_after: int | None = None
        self.omit_version = False
        self.after_post = None
        self.app = web.Application()
        self.app.router.add_get("/api/photos/price/{id}", self.get_photo)
        self.app.router.add_post("/api/photos/price/{id}", self.post_photo)
        self.server = TestServer(self.app)

    async def start(self) -> str:
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        await self.server.close()

    async def get_photo(self, request: web.Request) -> web.Response:
        self.requests.append(("GET", request.path, dict(request.headers), b""))
        if self.redirect:
            raise web.HTTPFound("/api/photos/price/1")
        photo = None
        if self.gallery_version or self.photo_version:
            photo = {"version": self.photo_version, "galleryVersion": self.gallery_version or None}
        return web.json_response(
            {
                "photo": photo,
                "gallerySupported": True,
                "identity": self.identity,
                "recoveryGeneration": self.generation,
            }
        )

    async def post_photo(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.requests.append(("POST", request.path, dict(request.headers), body))
        if self.redirect:
            raise web.HTTPFound("/api/photos/price/1")
        if self.status != 200:
            return web.json_response({"success": False, "message": "test"}, status=self.status)
        if self.payload is not None:
            return web.Response(body=self.payload, content_type="application/json")
        request_id = request.headers.get("X-Photo-Request", "")
        existing = next((item for item in self.images if item["request_id"] == request_id), None)
        if existing is None:
            if self.conflict_after is not None and len(self.images) >= self.conflict_after:
                self.conflict_after = None
                return web.json_response({"success": False, "message": "conflict"}, status=409)
            sent = request.headers.get("X-Photo-Gallery-Version", "")
            expected = self.gallery_version or self.photo_version
            if self.strict_versions and sent != expected:
                return web.json_response({"success": False, "message": "conflict"}, status=409)
            self.version_seq += 1
            version = f"rev-{self.version_seq}"
            existing = {"request_id": request_id, "version": version, "body": body}
            self.images.append(existing)
            self.gallery_version = version
            self.photo_version = version
        if self.after_post is not None:
            hook, self.after_post = self.after_post, None
            await hook()
        if self.commit_then_fail and len(self.images) >= self.commit_then_fail:
            self.commit_then_fail = 0
            return web.json_response({"success": False, "message": "lost"}, status=503)
        if self.omit_version:
            return web.json_response({"success": True})
        return web.json_response(
            {"success": True, "photo": {"version": existing["version"], "galleryVersion": existing["version"]}}
        )

    def posts(self) -> list[tuple[str, str, dict, bytes]]:
        return [item for item in self.requests if item[0] == "POST"]

    def gets(self) -> list[tuple[str, str, dict, bytes]]:
        return [item for item in self.requests if item[0] == "GET"]


class PhotoFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tempdir.name) / "products.db")
        connect(cls.db)
        ensure_schema(cls.db)
        cls.products_db = ProductsDB(cls.db)
        cls.cache_clear = register_handlers(cls.products_db)
        cls.dispatcher = Dispatcher()
        cls.dispatcher.include_router(router)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    async def asyncSetUp(self) -> None:
        flows.clear_all()
        self.session = MockSession({})
        self.bot = Bot(token="42:TEST", session=self.session)
        self.c3 = FakeC3()
        self.base = await self.c3.start()
        self.addCleanup(self.session.files.clear)

    async def asyncTearDown(self) -> None:
        await self.c3.stop()
        await self.bot.session.close()

    def env(self, url: str | None = None, key: str | None = KEY) -> None:
        values = {}
        if url is not None:
            values["PRICE_C3_URL"] = url
        if key is not None:
            values["PRICE_SYNC_KEY"] = key
        patcher = patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("PRICE_C3_URL", "PRICE_SYNC_KEY"):
            if name not in values:
                previous = os.environ.pop(name, None)
                if previous is not None:
                    self.addCleanup(os.environ.__setitem__, name, previous)

    def seed(self, products, generation: str | None = GENERATION) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM products")
            conn.execute("DELETE FROM c3_price_sync")
            conn.execute("DELETE FROM user_access")
            conn.execute(
                "INSERT INTO c3_price_sync(singleton,source,sequence,updated_at,recovery_generation)"
                " VALUES(1,?,?,?,?)",
                (SOURCE, 1, "now", generation),
            )
            for c3_id, code, name in products:
                conn.execute(
                    "INSERT INTO products(c3_id,code,name,name_cf,price,purchase_price,updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (c3_id, code, name, name.casefold(), 10.0, None, "now"),
                )

    async def grant(self, user_id: int, role: str = "allowed", active: bool = True) -> None:
        await self.products_db.upsert_user_role(user_id=user_id, role=role, is_active=active)

    def last_text(self) -> str:
        return self.session.sent[-1].text or ""

    async def wait_locked(self, user_id: int) -> None:
        for _ in range(300):
            flow = flows.get(user_id)
            if flow is not None and flow.lock.locked():
                return
            await asyncio.sleep(0.01)
        self.fail("flow lock was not held")

    def buttons(self) -> list[str]:
        markup = self.session.sent[-1].reply_markup
        return [row[0].callback_data for row in markup.inline_keyboard]

    def reply_labels(self) -> list[str]:
        markup = self.session.sent[-1].reply_markup
        return [button.text for row in markup.keyboard for button in row]

    async def send_text(self, user_id: int, text: str, chat_type: str = "private", chat_id: int | None = None) -> None:
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            text=text,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))

    async def send_photo(self, user_id: int, file_id: str = "photo-1", size: int | None = 1024) -> None:
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            photo=[PhotoSize(file_id=file_id, file_unique_id=file_id, width=10, height=10, file_size=size)],
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))

    async def send_document(
        self,
        user_id: int,
        file_id: str = "doc-1",
        file_name: str = "photo.jpg",
        mime_type: str = "image/jpeg",
        size: int | None = 1024,
    ) -> None:
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            document=Document(
                file_id=file_id,
                file_unique_id=file_id,
                file_name=file_name,
                mime_type=mime_type,
                file_size=size,
            ),
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))

    async def send_album_photo(
        self,
        user_id: int,
        file_id: str,
        message_id: int,
        group_id: str = "grp-1",
        size: int | None = 1024,
    ) -> None:
        message = Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            media_group_id=group_id,
            photo=[PhotoSize(file_id=file_id, file_unique_id=file_id, width=10, height=10, file_size=size)],
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=message_id, message=message))

    async def send_album_document(
        self,
        user_id: int,
        file_id: str,
        message_id: int,
        group_id: str = "grp-1",
        size: int | None = 1024,
    ) -> None:
        message = Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            media_group_id=group_id,
            document=Document(
                file_id=file_id,
                file_unique_id=file_id,
                file_name="scan.jpg",
                mime_type="image/jpeg",
                file_size=size,
            ),
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=message_id, message=message))

    async def press(self, user_id: int, data: str, chat_type: str = "private") -> None:
        message = Message(
            message_id=2,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            text="choose",
        )
        callback = CallbackQuery(
            id="cb1",
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            chat_instance="ci",
            data=data,
            message=message,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=2, callback_query=callback))

    async def choose(self, user_id: int, query: str = "001") -> None:
        await self.send_text(user_id, "/photo " + query)
        await self.press(user_id, self.buttons()[0])

    async def test_allowed_and_admin_flow(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100, "allowed")
        await self.grant(200, "admin")
        self.env(url=self.base)
        self.session.files["photo-1"] = b"\x89PNG-data"
        for user_id in (100, 200):
            await self.send_text(user_id, "/photo 001")
            self.assertIn("Виберіть товар", self.last_text())
            await self.press(user_id, self.buttons()[0])
            self.assertIn("Надішліть фото", self.last_text())
            await self.send_photo(user_id)
            self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
            self.assertIsNone(flows.get(user_id))
        self.assertEqual(self.c3.gets()[-1][1], "/api/photos/price/1")
        posts = self.c3.posts()
        self.assertEqual(len(posts), 2)
        first = {key.lower(): value for key, value in posts[0][2].items()}
        self.assertEqual(first["authorization"], "Bearer " + KEY)
        self.assertEqual(first["x-price-source"], SOURCE)
        self.assertEqual(first["x-c3-generation"], GENERATION)
        self.assertEqual(first["x-price-user"], "100")
        self.assertEqual(first["x-price-identity"], IDENTITY)
        self.assertEqual(first["x-photo-mode"], "append")
        self.assertEqual(first["x-photo-version"], "")
        self.assertEqual(first["x-photo-gallery-version"], "")
        self.assertEqual(posts[0][3], b"\x89PNG-data")
        self.assertEqual({key.lower(): value for key, value in posts[1][2].items()}["x-price-user"], "200")

    async def test_unauthorized_and_revoked_user(self) -> None:
        self.seed([(1, "001", "Фарба")])
        self.env(url=self.base)
        await self.send_text(300, "/photo 001")
        self.assertEqual(self.last_text(), "Доступ заборонено")
        await self.grant(301, "allowed")
        await self.products_db.set_user_active(user_id=301, is_active=False)
        await self.send_text(301, "/photo 001")
        self.assertEqual(self.last_text(), "Доступ заборонено")
        self.assertEqual(self.c3.requests, [])

    async def test_private_chat_restriction(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        await self.send_text(100, "/photo 001", chat_type="group")
        self.assertIn("лише в приватному чаті", self.last_text())
        await self.send_text(100, "/cancel", chat_type="group")
        self.assertIn("лише в приватному чаті", self.last_text())
        await self.send_text(100, "/retry", chat_type="group")
        self.assertIn("лише в приватному чаті", self.last_text())
        self.assertEqual(self.c3.requests, [])

    async def test_duplicate_selection_keeps_historical_rows(self) -> None:
        self.seed([(11, "DUP", "Історичний дублікат"), (12, "DUP", "Історичний дублікат")])
        await self.grant(100)
        self.env(url=self.base)
        await self.send_text(100, "/photo DUP")
        buttons = self.buttons()
        self.assertEqual(len(buttons), 2)
        self.assertNotEqual(buttons[0], buttons[1])
        await self.press(100, buttons[1])
        self.assertEqual(self.c3.gets()[-1][1], "/api/photos/price/12")
        self.session.files["photo-1"] = b"img"
        await self.send_photo(100)
        self.assertEqual(self.c3.posts()[-1][1], "/api/photos/price/12")
        self.assertEqual(self.last_text(), "Фото додано: DUP Історичний дублікат")

    async def test_stale_tampered_and_cross_user_token(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        await self.grant(200)
        self.env(url=self.base)
        await self.send_text(100, "/photo 001")
        token = self.buttons()[0]
        await self.press(100, "pp:unknown-token")
        self.assertIn("Вибір застарів", self.session.callback_answers[-1])
        await self.press(200, token)
        self.assertIn("Вибір застарів", self.session.callback_answers[-1])
        flow = flows.get(100)
        flow.selections[token[len("pp:"):]].expires_at = 0
        await self.press(100, token)
        self.assertIn("Вибір застарів", self.session.callback_answers[-1])
        self.assertEqual(self.c3.requests, [])

    async def test_photo_and_image_document_but_not_html(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100, "admin")
        self.env(url=self.base)
        self.session.files["photo-1"] = b"photo-bytes"
        self.session.files["doc-1"] = b"doc-bytes"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        await self.send_photo(100)
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        await self.send_document(100, file_id="doc-1", file_name="scan.png", mime_type="image/png")
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(len(self.c3.posts()), 2)
        await self.send_document(100, file_id="html-1", file_name="catalog.html", mime_type="text/html")
        self.assertIn("Імпорт HTML вимкнено", self.last_text())
        self.assertEqual(len(self.c3.posts()), 2)

    async def test_bounded_download_before_and_during(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        await self.send_photo(100, file_id="big", size=MAX_UPLOAD_BYTES + 1)
        self.assertIn("не більше 20 МБ", self.last_text())
        self.assertEqual(self.session.get_file_calls, 0)
        self.session.files["huge"] = b"x" * (MAX_UPLOAD_BYTES + 10)
        await self.send_photo(100, file_id="huge", size=1024)
        self.assertIn("не більше 20 МБ", self.last_text())
        self.assertEqual(self.c3.posts(), [])
        self.assertIsNone(flows.get(100).pending)

    async def test_network_retry_reuses_request_and_bytes(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"retry-bytes"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.c3.status = 503
        await self.send_photo(100)
        self.assertIn("Повторити надсилання", self.last_text())
        self.assertIsNotNone(flows.get(100).pending)
        self.c3.status = 200
        await self.send_text(100, "/retry")
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        posts = self.c3.posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["X-Photo-Request"], posts[1][2]["X-Photo-Request"])
        self.assertEqual(posts[0][3], posts[1][3])
        self.assertIsNone(flows.get(100))

    async def test_conflict_requires_reselection(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"conflict"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.c3.status = 409
        await self.send_photo(100)
        self.assertIn("виберіть товар повторно", self.last_text())
        self.assertIsNone(flows.get(100).pending)
        self.assertIsNone(flows.get(100).chosen)
        await self.send_text(100, "/retry")
        self.assertEqual(self.last_text(), "Немає незавершеного завантаження.")
        self.assertEqual(len(self.c3.posts()), 1)

    async def test_generation_change_blocks_upload(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"stale"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE c3_price_sync SET recovery_generation='gen-other'")
        await self.send_photo(100)
        self.assertIn("Каталог C3 змінився", self.last_text())
        self.assertEqual(self.c3.posts(), [])

    async def test_disabled_url_and_legacy_payload(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=None)
        await self.send_text(100, "/photo 001")
        self.assertEqual(self.last_text(), "Фото недоступні: не задано PRICE_C3_URL на сервері бота.")
        self.env(url=self.base)
        self.seed([(1, "001", "Фарба")], generation=None)
        await self.grant(100)
        await self.send_text(100, "/photo 001")
        self.assertIn("немає покоління відновлення C3", self.last_text())
        self.assertEqual(self.c3.requests, [])

    async def test_cancel_clears_only_current_user(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        await self.grant(200)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"pending"
        self.c3.status = 503
        for user_id in (100, 200):
            await self.send_text(user_id, "/photo 001")
            await self.press(user_id, self.buttons()[0])
            await self.send_photo(user_id)
        self.assertIsNotNone(flows.get(100).pending)
        self.assertIsNotNone(flows.get(200).pending)
        await self.send_text(100, "/cancel")
        self.assertEqual(self.last_text(), "Скасовано.")
        self.assertIsNone(flows.get(100))
        self.assertIsNotNone(flows.get(200).pending)
        posts_before = len(self.c3.posts())
        await self.send_photo(100)
        self.assertEqual(len(self.c3.posts()), posts_before)
        await self.send_text(200, "/start")
        self.assertIsNone(flows.get(200))

    async def test_uploader_headers_and_redirect_refusal(self) -> None:
        client = PhotoClient(self.base, KEY, SOURCE, GENERATION, 777)
        await client.upload(5, IDENTITY, "22222222-2222-2222-2222-222222222222", b"bytes")
        headers = {key.lower(): value for key, value in self.c3.posts()[0][2].items()}
        self.assertEqual(headers["authorization"], "Bearer " + KEY)
        self.assertEqual(headers["x-price-source"], SOURCE)
        self.assertEqual(headers["x-c3-generation"], GENERATION)
        self.assertEqual(headers["x-price-user"], "777")
        self.assertEqual(headers["x-price-identity"], IDENTITY)
        self.assertEqual(headers["x-photo-request"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(headers["x-photo-mode"], "append")
        self.assertEqual(headers["x-photo-version"], "")
        self.assertEqual(headers["x-photo-gallery-version"], "")
        self.c3.redirect = True
        with self.assertRaises(PhotoUploadError) as caught:
            await client.upload(5, IDENTITY, "33333333-3333-3333-3333-333333333333", b"bytes")
        self.assertIn("перенаправляє", caught.exception.message)
        self.assertEqual(len(self.c3.posts()), 2)
        self.assertEqual(len(self.c3.requests), 2)

    async def test_base_url_validation(self) -> None:
        cases = {
            "https://c3.example.com": "https://c3.example.com",
            "https://c3.example.com/": "https://c3.example.com",
            "http://127.0.0.1:8080": "http://127.0.0.1:8080",
            "http://localhost:8080": "http://localhost:8080",
            "http://c3.example.com": None,
            "https://user:pass@c3.example.com": None,
            "https://c3.example.com/?a=1": None,
            "https://c3.example.com/#frag": None,
            "https://c3.example.com:99999": None,
            "https://c3.example.com:abc": None,
            "https://[::1": None,
            "": None,
        }
        for value, expected in cases.items():
            with patch.dict(os.environ, {"PRICE_C3_URL": value}):
                self.assertEqual(resolve_base_url(), expected, value)

    async def test_sync_generation_persisted_and_bounded(self) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM c3_price_sync")
        payload = dict(source=SOURCE, sequence=1, full=True, recoveryGeneration=GENERATION, products=[])
        self.assertTrue(apply_batch(self.db, payload))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute("SELECT recovery_generation FROM c3_price_sync").fetchone()[0], GENERATION
            )
        legacy = dict(source=SOURCE, sequence=2, full=False, products=[])
        self.assertTrue(apply_batch(self.db, legacy))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT recovery_generation FROM c3_price_sync").fetchone()[0])
        with self.assertRaises(ValueError):
            apply_batch(
                self.db,
                dict(source=SOURCE, sequence=3, full=False, recoveryGeneration="x" * 201, products=[]),
            )


    async def test_append_to_non_empty_gallery_uses_current_revision(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"append"
        self.c3.gallery_version = "rev-1"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        await self.send_photo(100)
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(self.c3.posts()[0][2]["X-Photo-Gallery-Version"], "rev-1")
        self.assertEqual(self.c3.posts()[0][2]["X-Photo-Version"], "")

    async def test_gallery_revision_falls_back_to_photo_version(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"append"
        self.c3.photo_version = "v-first"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        await self.send_photo(100)
        self.assertEqual(self.c3.posts()[0][2]["X-Photo-Gallery-Version"], "v-first")

    async def test_gallery_changed_between_metadata_and_upload_conflicts(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"append"
        self.c3.gallery_version = "rev-1"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.c3.gallery_version = "rev-2"
        self.c3.status = 409
        await self.send_photo(100)
        self.assertIn("виберіть товар повторно", self.last_text())
        self.assertEqual(self.c3.posts()[0][2]["X-Photo-Gallery-Version"], "rev-1")
        self.assertIsNone(flows.get(100).chosen)
        self.assertIsNone(flows.get(100).pending)

    async def test_selection_ttl_checked_at_accept(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"late"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        flows.get(100).chosen.expires_at = 0
        await self.send_photo(100)
        self.assertIn("Вибір товару застарів", self.last_text())
        self.assertEqual(self.c3.posts(), [])

    async def test_quick_selection_callback_during_delayed_download(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"slow"
        await self.send_text(100, "/photo 001")
        token = self.buttons()[0]
        await self.press(100, token)
        self.session.gate = asyncio.Event()
        task = asyncio.create_task(self.send_photo(100))
        await self.wait_locked(100)
        await self.press(100, token)
        self.assertIn("Зачекайте", self.session.callback_answers[-1])
        self.session.gate.set()
        await task
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(len(self.c3.posts()), 1)

    async def test_cancel_start_and_photo_during_upload(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"slow"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.session.gate = asyncio.Event()
        task = asyncio.create_task(self.send_photo(100))
        await self.wait_locked(100)
        await self.send_text(100, "/cancel")
        self.assertIn("Зачекайте", self.last_text())
        await self.send_text(100, "/start")
        self.assertIn("Зачекайте", self.last_text())
        await self.send_text(100, "/photo 001")
        self.assertIn("Зачекайте", self.last_text())
        self.session.gate.set()
        await task
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(len(self.c3.posts()), 1)

    async def test_queued_second_photo_after_success(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"one"
        self.session.files["photo-2"] = b"two"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.session.gate = asyncio.Event()
        first = asyncio.create_task(self.send_photo(100, "photo-1"))
        await self.wait_locked(100)
        second = asyncio.create_task(self.send_photo(100, "photo-2"))
        await asyncio.sleep(0.05)
        self.session.gate.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(self.c3.posts()), 1)
        self.assertIsNone(flows.get(100))

    async def test_revoke_role_during_delayed_download(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"slow"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.session.gate = asyncio.Event()
        task = asyncio.create_task(self.send_photo(100))
        await self.wait_locked(100)
        await self.products_db.set_user_active(user_id=100, is_active=False)
        self.session.gate.set()
        await task
        self.assertEqual(self.last_text(), "Доступ заборонено")
        self.assertEqual(self.c3.posts(), [])
        self.assertIsNone(flows.get(100))

    async def test_new_photo_after_failed_pending_resets_uuid(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"first"
        self.session.files["photo-2"] = b"second"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.c3.status = 503
        await self.send_photo(100, "photo-1")
        stale = flows.get(100).pending.request_id
        self.c3.status = 200
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        await self.send_photo(100, "photo-2")
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        posts = self.c3.posts()
        self.assertEqual(len(posts), 2)
        self.assertNotEqual(posts[1][2]["X-Photo-Request"], stale)
        self.assertEqual(posts[1][3], b"second")

    async def test_long_telegram_id_header(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(5150000000)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"long"
        await self.send_text(5150000000, "/photo 001")
        await self.press(5150000000, self.buttons()[0])
        await self.send_photo(5150000000)
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(self.c3.posts()[0][2]["X-Price-User"], "5150000000")

    async def test_http200_non_success_payload_is_retryable(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"payload"
        await self.send_text(100, "/photo 001")
        await self.press(100, self.buttons()[0])
        self.c3.payload = b'{"success": false}'
        await self.send_photo(100)
        self.assertIn("Повторити надсилання", self.last_text())
        self.assertIsNotNone(flows.get(100).pending)
        for payload in (b"[1,2]", b'{"success": true}' + b" " * (1024 * 1024)):
            self.c3.payload = payload
            await self.send_text(100, "/retry")
            self.assertIn("Повторити надсилання", self.last_text())
            self.assertIsNotNone(flows.get(100).pending)
        self.c3.payload = None
        await self.send_text(100, "/retry")
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        posts = self.c3.posts()
        self.assertEqual(len(posts), 4)
        self.assertEqual(len({item[2]["X-Photo-Request"] for item in posts}), 1)
        self.assertEqual(len({item[3] for item in posts}), 1)

    async def test_label_keeps_c3_id_for_long_names(self) -> None:
        self.seed([(1, "001", "Дуже довга назва товару для перевірки кнопки вибору фото")])
        await self.grant(100)
        self.env(url=self.base)
        await self.send_text(100, "/photo 001")
        markup = self.session.sent[-1].reply_markup
        label = markup.inline_keyboard[0][0].text
        self.assertLessEqual(len(label), 60)
        self.assertTrue(label.endswith("· ID 1"), label)


    async def test_start_button_flow_without_slash(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"button-bytes"
        await self.send_text(100, "/start")
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        self.assertIn("натисніть «Додати фото»", self.last_text())
        await self.send_text(100, "Додати фото")
        self.assertIn("Введіть код або назву товару для фото", self.last_text())
        self.assertEqual(self.reply_labels(), ["Скасувати"])
        await self.send_text(100, "001")
        self.assertIn("Виберіть товар", self.last_text())
        await self.press(100, self.buttons()[0])
        self.assertIn("Надішліть фото", self.last_text())
        self.assertEqual(self.reply_labels(), ["Скасувати"])
        await self.send_photo(100)
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        self.assertEqual(len(self.c3.posts()), 1)

    async def test_price_search_keeps_main_menu(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        await self.send_text(100, "Фарба")
        self.assertIn("Фарба", self.last_text())
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        await self.send_text(100, "Фарба")
        self.assertIn("Фарба", self.last_text())
        self.assertEqual(self.reply_labels(), ["Додати фото"])

    async def test_cancel_and_retry_buttons_keep_pending_uuid(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["photo-1"] = b"retry-bytes"
        await self.send_text(100, "Додати фото")
        await self.send_text(100, "001")
        await self.press(100, self.buttons()[0])
        self.c3.status = 503
        await self.send_photo(100)
        self.assertIn("Повторити надсилання", self.last_text())
        self.assertEqual(self.reply_labels(), ["Повторити надсилання", "Скасувати"])
        pending = flows.get(100).pending.request_id
        self.c3.status = 200
        await self.send_text(100, "Повторити надсилання")
        self.assertEqual(self.last_text(), "Фото додано: 001 Фарба")
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        posts = self.c3.posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["X-Photo-Request"], pending)
        self.assertEqual(posts[0][2]["X-Photo-Request"], posts[1][2]["X-Photo-Request"])
        self.assertEqual(posts[0][3], posts[1][3])

    async def test_cancel_button_restores_main_menu(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        await self.send_text(100, "Додати фото")
        await self.send_text(100, "Скасувати")
        self.assertEqual(self.last_text(), "Скасовано.")
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        self.assertIsNone(flows.get(100))
        await self.send_text(100, "Скасувати")
        self.assertEqual(self.last_text(), "Скасовано.")
        self.assertEqual(self.c3.requests, [])

    async def test_buttons_respect_access_and_private_chat(self) -> None:
        self.seed([(1, "001", "Фарба")])
        self.env(url=self.base)
        await self.send_text(300, "Додати фото")
        self.assertEqual(self.last_text(), "Доступ заборонено")
        self.assertIsInstance(self.session.sent[-1].reply_markup, ReplyKeyboardRemove)
        await self.grant(100)
        await self.send_text(100, "Додати фото", chat_type="group")
        self.assertIn("лише в приватному чаті", self.last_text())
        await self.send_text(100, "Скасувати", chat_type="group")
        self.assertIn("лише в приватному чаті", self.last_text())
        await self.send_text(100, "Повторити надсилання", chat_type="group")
        self.assertIn("лише в приватному чаті", self.last_text())
        await self.send_text(100, "/start", chat_type="group")
        self.assertIsInstance(self.session.sent[-1].reply_markup, ReplyKeyboardRemove)
        self.assertEqual(self.c3.requests, [])

    async def test_setup_diagnostics_are_specific(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        messages: list[str] = []
        self.env(url=None)
        await self.send_text(100, "/photo 001")
        messages.append(self.last_text())
        self.env(url="http://c3.example.com")
        await self.send_text(100, "/photo 001")
        messages.append(self.last_text())
        self.env(url=self.base, key=None)
        await self.send_text(100, "/photo 001")
        messages.append(self.last_text())
        self.env(url=self.base, key="short")
        await self.send_text(100, "/photo 001")
        messages.append(self.last_text())
        self.env(url=self.base)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE c3_price_sync SET source=''")
        await self.send_text(100, "/photo 001")
        messages.append(self.last_text())
        self.seed([(1, "001", "Фарба")], generation=None)
        await self.grant(100)
        await self.send_text(100, "/photo 001")
        messages.append(self.last_text())
        self.assertEqual(messages[0], "Фото недоступні: не задано PRICE_C3_URL на сервері бота.")
        self.assertEqual(
            messages[1],
            "Фото недоступні: PRICE_C3_URL має некоректний формат (потрібен HTTPS без облікових даних).",
        )
        self.assertEqual(messages[2], "Фото недоступні: не задано PRICE_SYNC_KEY на сервері бота.")
        self.assertIn("PRICE_SYNC_KEY має некоректний формат", messages[3])
        self.assertEqual(
            messages[4],
            "Фото недоступні: каталог Price не має джерела C3. Потрібен новий повний прайс із C3 Trade.",
        )
        self.assertIn("немає покоління відновлення C3", messages[5])
        for text in messages:
            self.assertNotIn(" або ", text)
        self.assertEqual(self.c3.requests, [])


    async def test_album_two_photos_concurrent_save(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a2"] = b"two"
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
        )
        self.assertIn("Отримано 2 фото", self.last_text())
        self.assertEqual(self.reply_labels(), ["Зберегти фото", "Скасувати"])
        self.assertEqual(self.c3.posts(), [])
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 2 фото: 001 Фарба")
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        posts = self.c3.posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual([item[1] for item in posts], ["/api/photos/price/1", "/api/photos/price/1"])
        self.assertEqual([item[3] for item in posts], [b"one", b"two"])
        self.assertEqual(posts[0][2]["X-Photo-Gallery-Version"], "")
        self.assertEqual(posts[1][2]["X-Photo-Gallery-Version"], "rev-1")
        self.assertEqual(len({item[2]["X-Photo-Request"] for item in posts}), 2)
        self.assertEqual(len(self.c3.images), 2)
        self.assertIsNone(flows.get(100))
        self.assertEqual(flows.receipt(100, 100, "grp-1"), "saved")

    async def test_album_three_photos_stable_order(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        for name, payload in (("a1", b"one"), ("a2", b"two"), ("a3", b"three")):
            self.session.files[name] = payload
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a3", 13),
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
        )
        self.assertIn("Отримано 3 фото", self.last_text())
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 3 фото: 001 Фарба")
        posts = self.c3.posts()
        self.assertEqual([item[3] for item in posts], [b"one", b"two", b"three"])
        self.assertEqual(
            [item[2]["X-Photo-Gallery-Version"] for item in posts], ["", "rev-1", "rev-2"]
        )
        self.assertEqual(len(self.c3.images), 3)
        self.assertEqual(len({item[2]["X-Photo-Request"] for item in posts}), 3)

    async def test_album_grouped_image_documents(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["d1"] = b"doc-one"
        self.session.files["d2"] = b"doc-two"
        await self.choose(100)
        await asyncio.gather(
            self.send_album_document(100, "d1", 21),
            self.send_album_document(100, "d2", 22),
        )
        self.assertIn("Отримано 2 фото", self.last_text())
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 2 фото: 001 Фарба")
        self.assertEqual([item[3] for item in self.c3.posts()], [b"doc-one", b"doc-two"])
        self.assertEqual(len(self.c3.images), 2)

    async def test_album_duplicate_update_not_reposted(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a2"] = b"two"
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
        )
        await self.send_album_photo(100, "a1", 11)
        self.assertEqual(len(flows.get(100).album.items), 2)
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 2 фото: 001 Фарба")
        await self.send_album_photo(100, "a1", 11)
        self.assertEqual(len(self.c3.posts()), 2)
        self.assertEqual(len(self.c3.images), 2)
        self.assertEqual(self.last_text(), "Додано 2 фото: 001 Фарба")

    async def test_album_lost_response_retries_only_unresolved(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        for name, payload in (("a1", b"one"), ("a2", b"two"), ("a3", b"three")):
            self.session.files[name] = payload
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
            self.send_album_photo(100, "a3", 13),
        )
        self.c3.commit_then_fail = 2
        await self.send_text(100, "Зберегти фото")
        self.assertIn("Збережено 1 із 3 фото", self.last_text())
        self.assertEqual(self.reply_labels(), ["Повторити надсилання", "Скасувати"])
        self.assertEqual(len(self.c3.images), 2)
        lost = self.c3.posts()[1][2]["X-Photo-Request"]
        await self.send_text(100, "Повторити надсилання")
        self.assertEqual(self.last_text(), "Додано 3 фото: 001 Фарба")
        self.assertEqual(len(self.c3.images), 3)
        retried = [item for item in self.c3.posts() if item[2]["X-Photo-Request"] == lost]
        self.assertEqual(len(retried), 2)
        self.assertEqual(retried[0][3], retried[1][3])
        self.assertEqual(len({item[2]["X-Photo-Request"] for item in self.c3.posts()}), 3)
        self.assertIsNone(flows.get(100))

    async def test_album_conflict_mid_batch_no_rebase(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        for name, payload in (("a1", b"one"), ("a2", b"two"), ("a3", b"three")):
            self.session.files[name] = payload
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
            self.send_album_photo(100, "a3", 13),
        )
        self.c3.conflict_after = 1
        await self.send_text(100, "Зберегти фото")
        self.assertIn("Збережено 1 із 3 фото", self.last_text())
        self.assertIn("виберіть товар повторно", self.last_text())
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        self.assertEqual(len(self.c3.images), 1)
        self.assertEqual(len(self.c3.posts()), 2)
        self.assertIsNone(flows.get(100))

    async def test_album_oversized_member_partial(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a3"] = b"three"
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12, size=MAX_UPLOAD_BYTES + 1),
            self.send_album_photo(100, "a3", 13),
        )
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Збережено 2 із 3 фото. Фото має бути не більше 20 МБ.")
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        self.assertEqual(len(self.c3.images), 2)
        self.assertIsNone(flows.get(100))

    async def test_album_different_group_rejected(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        await self.choose(100)
        await self.send_album_photo(100, "a1", 11, group_id="grp-1")
        await self.send_album_photo(100, "a2", 12, group_id="grp-2")
        self.assertIn("Альбом уже збирається для іншого вибору", self.last_text())
        album = flows.get(100).album
        self.assertEqual(album.group_id, "grp-1")
        self.assertEqual(len(album.items), 1)
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 1 фото: 001 Фарба")
        self.assertEqual(len(self.c3.images), 1)

    async def test_album_cancel_then_old_group_member_ignored(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a2"] = b"two"
        await self.choose(100)
        await self.send_album_photo(100, "a1", 11, group_id="grp-1")
        await self.send_text(100, "Скасувати")
        self.assertEqual(self.last_text(), "Скасовано.")
        self.assertEqual(flows.receipt(100, 100, "grp-1"), "cancelled")
        await self.choose(100)
        await self.send_album_photo(100, "a2", 12, group_id="grp-1")
        self.assertIsNone(flows.get(100).album)
        self.assertIn("Надішліть фото", self.last_text())
        self.assertEqual(self.c3.posts(), [])

    async def test_album_revoked_user_cannot_save(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        await self.choose(100)
        await self.send_album_photo(100, "a1", 11)
        await self.products_db.set_user_active(user_id=100, is_active=False)
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Доступ заборонено")
        self.assertEqual(self.c3.posts(), [])

    async def test_album_concurrent_save_and_cancel(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a2"] = b"two"
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
        )
        self.session.gate = asyncio.Event()
        task = asyncio.create_task(self.send_text(100, "Зберегти фото"))
        await self.wait_locked(100)
        await self.send_text(100, "Скасувати")
        self.assertIn("Зачекайте", self.last_text())
        self.session.gate.set()
        await task
        self.assertEqual(self.last_text(), "Додано 2 фото: 001 Фарба")
        self.assertEqual(len(self.c3.images), 2)
        self.assertIsNone(flows.get(100))

    async def test_album_generation_change_blocks_save(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a2"] = b"two"
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE c3_price_sync SET recovery_generation='gen-other'")
        await self.send_text(100, "Зберегти фото")
        self.assertIn("Збережено 0 із 2 фото", self.last_text())
        self.assertIn("Каталог C3 змінився", self.last_text())
        self.assertEqual(self.c3.posts(), [])
        self.assertIsNone(flows.get(100))

    async def test_album_frozen_during_save_rejects_late_member(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        for name, payload in (("a1", b"one"), ("a2", b"two"), ("a3", b"three"), ("a4", b"four")):
            self.session.files[name] = payload
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
            self.send_album_photo(100, "a3", 13),
        )
        self.c3.commit_then_fail = 2
        self.session.gate = asyncio.Event()
        save = asyncio.create_task(self.send_text(100, "Зберегти фото"))
        await self.wait_locked(100)
        late = asyncio.create_task(self.send_album_photo(100, "a4", 14))
        await asyncio.sleep(0.05)
        self.session.gate.set()
        await asyncio.gather(save, late)
        texts = [item.text or "" for item in self.session.sent]
        self.assertIn("Збережено 1 із 3 фото", texts[-2])
        self.assertIn("Альбом уже зберігається", texts[-1])
        album = flows.get(100).album
        self.assertEqual(sorted(album.items), [11, 12, 13])
        self.assertTrue(album.started)
        await self.send_text(100, "Повторити надсилання")
        self.assertEqual(self.last_text(), "Додано 3 фото: 001 Фарба")
        self.assertEqual(len(self.c3.images), 3)
        self.assertEqual(len(self.c3.posts()), 4)
        self.assertIsNone(flows.get(100))

    async def test_album_missing_version_stops_partial(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        for name, payload in (("a1", b"one"), ("a2", b"two"), ("a3", b"three")):
            self.session.files[name] = payload
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
            self.send_album_photo(100, "a3", 13),
        )
        self.c3.omit_version = True
        await self.send_text(100, "Зберегти фото")
        self.assertIn("Збережено 1 із 3 фото", self.last_text())
        self.assertIn("не повернув версію галереї", self.last_text())
        self.assertEqual(self.reply_labels(), ["Додати фото"])
        self.assertEqual(len(self.c3.posts()), 1)
        self.assertEqual(len(self.c3.images), 1)
        self.assertIsNone(flows.get(100))
        await self.send_text(100, "/retry")
        self.assertEqual(self.last_text(), "Немає незавершеного завантаження.")
        self.assertEqual(len(self.c3.posts()), 1)

    async def test_album_revoked_mid_batch_reports_partial(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        self.env(url=self.base)
        for name, payload in (("a1", b"one"), ("a2", b"two"), ("a3", b"three")):
            self.session.files[name] = payload
        await self.choose(100)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11),
            self.send_album_photo(100, "a2", 12),
            self.send_album_photo(100, "a3", 13),
        )

        async def revoke() -> None:
            await self.products_db.set_user_active(user_id=100, is_active=False)

        self.c3.after_post = revoke
        await self.send_text(100, "Зберегти фото")
        self.assertIn("Збережено 1 із 3 фото", self.last_text())
        self.assertIn("Доступ заборонено", self.last_text())
        self.assertIsInstance(self.session.sent[-1].reply_markup, ReplyKeyboardRemove)
        self.assertEqual(len(self.c3.images), 1)
        self.assertEqual(len(self.c3.posts()), 1)
        self.assertIsNone(flows.get(100))
        self.assertEqual(flows._bytes, 0)

    async def test_album_receipt_scoped_per_user_and_chat(self) -> None:
        self.seed([(1, "001", "Фарба")])
        await self.grant(100)
        await self.grant(200)
        self.env(url=self.base)
        self.session.files["a1"] = b"one"
        self.session.files["a2"] = b"two"
        await self.choose(100)
        await self.choose(200)
        await asyncio.gather(
            self.send_album_photo(100, "a1", 11, group_id="grp-1"),
            self.send_album_photo(200, "a2", 12, group_id="grp-1"),
        )
        await self.send_text(100, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 1 фото: 001 Фарба")
        self.assertEqual(flows.receipt(100, 100, "grp-1"), "saved")
        self.assertIsNone(flows.receipt(200, 200, "grp-1"))
        self.assertIsNotNone(flows.get(200).album)
        await self.send_text(200, "Зберегти фото")
        self.assertEqual(self.last_text(), "Додано 1 фото: 001 Фарба")
        self.assertEqual(len(self.c3.images), 2)
        self.assertEqual([item[3] for item in self.c3.posts()], [b"one", b"two"])


class ConfigureSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / ".env"

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def test_url_forwarded_and_preserved(self) -> None:
        self.path.write_text("TELEGRAM_BOT_TOKEN=1:x\n", encoding="utf-8")
        key = configure(self.path, "k" * 40, "https://c3.example.com/")
        self.assertIn("PRICE_SYNC_KEY=" + key, self.read())
        self.assertIn("PRICE_C3_URL=https://c3.example.com", self.read())
        self.assertIn("TELEGRAM_BOT_TOKEN=1:x", self.read())
        configure(self.path, None, None)
        self.assertIn("PRICE_C3_URL=https://c3.example.com", self.read())

    def test_invalid_url_rejected_before_write(self) -> None:
        self.path.write_text("PRICE_SYNC_KEY=" + "k" * 40 + "\n", encoding="utf-8")
        before = self.read()
        for bad in (
            "http://c3.example.com",
            "https://user:pass@c3.example.com",
            "https://c3.example.com/?a=1",
            "ftp://c3.example.com",
        ):
            with self.assertRaises(ValueError):
                configure(self.path, None, bad)
            self.assertEqual(self.read(), before)
        configure(self.path, None, "http://127.0.0.1:8082")
        self.assertIn("PRICE_C3_URL=http://127.0.0.1:8082", self.read())

    def test_key_only_behavior_preserved(self) -> None:
        key = configure(self.path, None, None)
        self.assertIn("PRICE_SYNC_KEY=" + key, self.read())
        self.assertNotIn("PRICE_C3_URL", self.read())
        configure(self.path, None, None)
        self.assertIn("PRICE_SYNC_KEY=" + key, self.read())

    def test_deploy_forwards_url_variable(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("PRICE_C3_URL: ${{ vars.PRICE_C3_URL }}", workflow)
        self.assertIn("envs: PRICE_SYNC_KEY,PRICE_C3_URL", workflow)


if __name__ == "__main__":
    unittest.main()
