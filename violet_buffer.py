"""
violet_buffer.py — клиент для Violet Buffer на чистом stdlib (без requests).

Модель: у ЭТОГО бота один project_id (свой токен/секрет, создаётся один раз через
manage.sh create на стороне буфера). owner_id — telegram user_id, баланс общий
для этого owner_id через любые приложения (боты/сайт), использующие тот же буфер
и тот же owner_id.

Все функции синхронные (blocking) — вызывать из async-хендлеров через
asyncio.to_thread(...), чтобы не блокировать event loop бота на время HTTP-запроса.
"""

import os
import json
import time
import hmac
import hashlib
import uuid
import urllib.request
import urllib.error
import logging

BUFFER_URL = os.environ.get("BUFFER_URL", "http://127.0.0.1:8080")
BUFFER_TOKEN = os.environ.get("BUFFER_TOKEN")
BUFFER_SECRET = os.environ.get("BUFFER_SECRET")

DEFAULT_TIMEOUT = 10  # секунд на HTTP-запрос к буферу


class BufferError(Exception):
    """Ошибка при обращении к буферу. .status и .body заполнены, если пришёл ответ с телом."""
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


def _sign(secret: str, timestamp: str, raw_body: str) -> str:
    msg = f"{timestamp}.{raw_body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _request(path: str, method: str = "GET", body_obj=None):
    if not BUFFER_TOKEN or not BUFFER_SECRET:
        raise BufferError("BUFFER_TOKEN / BUFFER_SECRET не заданы в окружении")

    raw_body = json.dumps(body_obj, ensure_ascii=False) if body_obj is not None else ""
    ts = str(int(time.time()))
    sig = _sign(BUFFER_SECRET, ts, raw_body)

    url = f"{BUFFER_URL}{path}"
    data = raw_body.encode("utf-8") if raw_body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Token", BUFFER_TOKEN)
    req.add_header("X-Timestamp", ts)
    req.add_header("X-Signature", sig)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"error": str(e)}
        raise BufferError(payload.get("error", f"HTTP {e.code}"), status=e.code, body=payload)
    except urllib.error.URLError as e:
        raise BufferError(f"Буфер недоступен: {e.reason}")


def get_balance(owner_id) -> int:
    """Возвращает текущий баланс владельца. Бросает BufferError при сбое сети/авторизации."""
    result = _request(f"/api/balance?owner_id={owner_id}", "GET")
    return result["balance"]


def get_events(owner_id, since: int = 0):
    result = _request(f"/api/events?owner_id={owner_id}&since={since}", "GET")
    return result["events"]


def credit(owner_id, amount: int, meta: dict = None, idempotency_key: str = None) -> int:
    """
    Начисляет amount владельцу. Возвращает новый баланс.
    Если idempotency_key не передан — генерируется случайный (обычная разовая операция).
    Передавай свой детерминированный key для операций, которые не должны повториться
    даже при повторном вызове этой функции (например разовый стартовый бонус).
    """
    body = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "owner_id": str(owner_id),
        "kind": "credit",
        "amount": int(amount),
        "meta": meta or {},
    }
    result = _request("/webhook/event", "POST", body)
    return result["balance"]


def debit(owner_id, amount: int, meta: dict = None, idempotency_key: str = None) -> int:
    """
    Списывает amount у владельца. Возвращает новый баланс.
    Бросает BufferError(status=409) с .body['balance'], если средств не хватает —
    вызывающий код должен явно ловить это и показывать понятное сообщение юзеру,
    а не как общую ошибку сети.
    """
    body = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "owner_id": str(owner_id),
        "kind": "debit",
        "amount": int(amount),
        "meta": meta or {},
    }
    result = _request("/webhook/event", "POST", body)
    return result["balance"]


def try_debit(owner_id, amount: int, meta: dict = None, idempotency_key: str = None):
    """
    Вспомогательная обёртка: возвращает (ok: bool, balance_or_none, error_or_none).
    Удобна там, где не хочется ловить исключение вручную на каждый вызов —
    например при покупке спонсорской ссылки.
    """
    try:
        new_balance = debit(owner_id, amount, meta, idempotency_key)
        return True, new_balance, None
    except BufferError as e:
        if e.status == 409:
            return False, e.body.get("balance"), "insufficient_balance"
        logging.error(f"[violet_buffer] debit failed for owner={owner_id}: {e}")
        return False, None, str(e)
