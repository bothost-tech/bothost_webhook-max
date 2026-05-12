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
    """Возвращает (user_id, chat_id, chat_type, text, mid) из Update или пять None.

    Для POST /messages MAX: в личке (dialog) в query обычно нужен user_id отправителя;
    в группе/канале — chat_id. См. https://dev.max.ru/docs-api/methods/POST/messages
    и recipient.chat_type в Update.
    """
    if data.get("update_type") != "message_created":
        return None, None, None, None, None
    msg = data.get("message")
    if not isinstance(msg, dict):
        logger.warning("message_created без объекта message: keys=%s", list(data.keys()))
        return None, None, None, None, None
    recipient = msg.get("recipient")
    if not isinstance(recipient, dict):
        logger.warning("Нет recipient; message keys=%s", list(msg.keys()))
        return None, None, None, None, None
    body = msg.get("body")
    if not isinstance(body, dict):
        logger.warning("Нет body; message=%s", json.dumps(msg, ensure_ascii=False)[:800])
        return None, None, None, None, None
    chat_id = recipient.get("chat_id")
    chat_type = recipient.get("chat_type")
    text = (body.get("text") or "").strip()
    mid = body.get("mid")
    user_id = None
    sender = msg.get("sender")
    if isinstance(sender, dict) and not sender.get("is_bot"):
        user_id = sender.get("user_id")
    return user_id, chat_id, chat_type, text, mid


def send_max_message(
    user_id: int | None,
    chat_id: int | None,
    recipient_chat_type: str | None,
    text: str,
) -> None:
    """POST /messages: user_id или chat_id в query (официальный пример MAX)."""
    url = f"{MAX_API}/messages"
    params = {}
    ct = (recipient_chat_type or "").strip().lower()
    # Группа / канал — отвечаем в тот же чат
    if ct in ("chat", "channel") and chat_id is not None:
        params["chat_id"] = int(chat_id)
    # Личка или неизвестный тип — сначала user_id отправителя (иначе бывает Unknown recipient)
    elif user_id is not None:
        params["user_id"] = int(user_id)
    elif chat_id is not None:
        params["chat_id"] = int(chat_id)
    else:
        logger.warning("send_max_message: нет user_id и chat_id, ответ не отправлен")
        return
    body = {"text": text}
    try:
        r = requests.post(url, headers=api_headers(), params=params, json=body, timeout=15)
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

    # Секрет обязателен только если задан в окружении (иначе каждый POST давал бы 403 и MAX не доставлял бы события).
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(got, WEBHOOK_SECRET):
            return jsonify({"error": "forbidden"}), 403
    else:
        logger.warning(
            "WEBHOOK_SECRET не задан — вебхук без проверки заголовка (только для отладки, в проде задайте secret)"
        )

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        logger.warning("Тело не JSON или пусто")
        return jsonify({"ok": True}), 200

    update_type = data.get("update_type")

    if update_type == "message_created":
        user_id, chat_id, chat_type, user_text, mid = extract_message_payload(data)
        if mid and remember_mid(mid):
            logger.info("Пропуск дубликата по mid=%s", mid)
            return jsonify({"ok": True}), 200
        if user_text and (user_id is not None or chat_id is not None):
            send_max_message(
                int(user_id) if user_id is not None else None,
                int(chat_id) if chat_id is not None else None,
                str(chat_type) if chat_type is not None else None,
                f"Эхо: {user_text}",
            )
        elif user_id is None and chat_id is None:
            logger.warning(
                "Не удалось извлечь user_id/chat_id; payload=%s",
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
