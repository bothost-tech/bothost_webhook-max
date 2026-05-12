"""
Эхо-бот MAX Bot API для теста вебхука (см. bothost.ru/blog/post/webhook-max-bot-api).
Переменные окружения: MAX_BOT_TOKEN, WEBHOOK_SECRET; опционально MAX_USE_BEARER=1.
"""
import os
import json
import hmac
import logging

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("echo")

app = Flask(__name__)

MAX_API = "https://platform-api.max.ru"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TOKEN = (os.environ.get("MAX_BOT_TOKEN") or "").strip()
USE_BEARER = os.environ.get("MAX_USE_BEARER", "").lower() in ("1", "true", "yes")

# Идемпотентность: последние обработанные ключи (в памяти; в проде — Redis/БД)
_SEEN_MID = set()
_SEEN_MAX = 500


def auth_value() -> str:
    if USE_BEARER and not TOKEN.lower().startswith("bearer "):
        return f"Bearer {TOKEN}"
    return TOKEN


def api_headers():
    return {
        "Authorization": auth_value(),
        "Content-Type": "application/json",
    }


def extract_message_payload(data: dict):
    """Возвращает (chat_id, text, mid) из Update или (None, None, None)."""
    if data.get("update_type") != "message_created":
        return None, None, None
    msg = data.get("message")
    if not isinstance(msg, dict):
        logger.warning("message_created без объекта message: keys=%s", list(data.keys()))
        return None, None, None
    recipient = msg.get("recipient")
    if not isinstance(recipient, dict):
        logger.warning("Нет recipient; message keys=%s", list(msg.keys()))
        return None, None, None
    body = msg.get("body")
    if not isinstance(body, dict):
        logger.warning("Нет body; message=%s", json.dumps(msg, ensure_ascii=False)[:800])
        return None, None, None
    chat_id = recipient.get("chat_id")
    text = (body.get("text") or "").strip()
    mid = body.get("mid")
    return chat_id, text, mid


def send_max_message(chat_id: int, text: str) -> None:
    url = f"{MAX_API}/messages"
    body = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, headers=api_headers(), json=body, timeout=15)
        if not r.ok:
            logger.error("messages API: %s %s", r.status_code, r.text[:500])
    except requests.RequestException as e:
        logger.exception("Ошибка сети при POST /messages: %s", e)


def remember_mid(mid) -> bool:
    """True если уже обрабатывали (дубликат)."""
    if not mid:
        return False
    if mid in _SEEN_MID:
        return True
    if len(_SEEN_MID) >= _SEEN_MAX:
        _SEEN_MID.clear()
    _SEEN_MID.add(mid)
    return False


@app.route("/webhook", methods=["POST", "HEAD"])
def webhook():
    if request.method == "HEAD":
        return "", 200

    got = request.headers.get("X-Max-Bot-Api-Secret", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(got, WEBHOOK_SECRET):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        logger.warning("Тело не JSON или пусто")
        return jsonify({"ok": True}), 200

    update_type = data.get("update_type")

    if update_type == "message_created":
        chat_id, user_text, mid = extract_message_payload(data)
        if mid and remember_mid(mid):
            logger.info("Пропуск дубликата по mid=%s", mid)
            return jsonify({"ok": True}), 200
        if chat_id is not None and user_text:
            send_max_message(int(chat_id), f"Эхо: {user_text}")
        elif chat_id is None:
            logger.warning(
                "Не удалось извлечь chat_id; payload=%s",
                json.dumps(data, ensure_ascii=False)[:1200],
            )

    elif update_type == "bot_started":
        logger.info("bot_started: %s", json.dumps(data, ensure_ascii=False)[:500])

    return jsonify({"ok": True}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Задайте MAX_BOT_TOKEN")
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
