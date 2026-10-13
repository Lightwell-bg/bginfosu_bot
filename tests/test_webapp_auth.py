"""Проверка initData Telegram Mini App (подпись, срок, пользователь)."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from bginfo.webapp.auth import AuthError, parse_header, verify_init_data

TOKEN = "123456:TESTTOKEN"


def make_init_data(user=None, token=TOKEN, auth_date=None, extra=None, bad_hash=False, drop_user=False) -> str:
    """initData, подписанный как Telegram (hash исключается из строки проверки, signature — нет)."""
    fields = {"auth_date": str(int(time.time()) if auth_date is None else auth_date), "query_id": "AAH",
              "signature": "sig"}
    if not drop_user:
        fields["user"] = json.dumps(user or {"id": 42, "first_name": "Т", "username": "tester"}, ensure_ascii=False)
    fields.update(extra or {})
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if bad_hash:
        fields["hash"] = "0" * 64
    return urlencode(fields)


def test_valid_init_data():
    u = verify_init_data(make_init_data(), TOKEN, 24)
    assert (u.id, u.username, u.first_name, u.last_name) == (42, "tester", "Т", None)


def test_wrong_token_and_bad_hash():
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(token="999:OTHER"), TOKEN, 24)
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(bad_hash=True), TOKEN, 24)


def test_tampered_field_rejected():
    data = make_init_data(user={"id": 42})
    tampered = data.replace("42", "43")
    assert tampered != data
    with pytest.raises(AuthError):
        verify_init_data(tampered, TOKEN, 24)


def test_expired_and_future_auth_date():
    now = time.time()
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(auth_date=int(now - 25 * 3600)), TOKEN, 24)
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(auth_date=int(now + 600)), TOKEN, 24)
    # небольшое расхождение часов допустимо
    assert verify_init_data(make_init_data(auth_date=int(now + 60)), TOKEN, 24).id == 42


def test_no_or_bad_user():
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(drop_user=True), TOKEN, 24)
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(extra={"user": "not json"}), TOKEN, 24)
    with pytest.raises(AuthError):
        verify_init_data(make_init_data(extra={"user": json.dumps({"first_name": "x"})}), TOKEN, 24)


@pytest.mark.parametrize("garbage", ["", "garbage", "a=1&a=2&hash=x", "hash=abc", "\x00\xff", "x" * 5000])
def test_garbage(garbage):
    with pytest.raises(AuthError):
        verify_init_data(garbage, TOKEN, 24)


def test_header_parsing():
    assert parse_header("tma abc=1") == "abc=1"
    for bad in (None, "", "Bearer x", "tma", "TMA x"):
        with pytest.raises(AuthError):
            parse_header(bad)
