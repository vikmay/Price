from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from app.webapp import validate_init_data

TOKEN = "123456:TEST-token"


def make(user_id: int = 7, age: int = 10, token: str = TOKEN, tamper: bool = False) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": user_id + 1})
    return urlencode(fields)


class InitDataTests(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(validate_init_data(make(42), TOKEN), 42)

    def test_wrong_token(self) -> None:
        self.assertIsNone(validate_init_data(make(token="999:OTHER"), TOKEN))

    def test_tampered(self) -> None:
        self.assertIsNone(validate_init_data(make(tamper=True), TOKEN))

    def test_expired(self) -> None:
        self.assertIsNone(validate_init_data(make(age=7200), TOKEN))

    def test_empty(self) -> None:
        self.assertIsNone(validate_init_data("", TOKEN))
        self.assertIsNone(validate_init_data("hash=abc", TOKEN))


if __name__ == "__main__":
    unittest.main()
