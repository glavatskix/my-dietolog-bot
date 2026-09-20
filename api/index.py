"""
Отдельный Telegram-бот — помощник по питанию и снижению веса, на Gemini API.
Полностью независим от рабочего бота (свой токен, своя память, своя "личность").

Переменные окружения (задаются в панели Vercel, НЕ в этом файле):
  TELEGRAM_TOKEN            — токен ЭТОГО бота от @BotFather (другой, чем у рабочего бота)
  GEMINI_API_KEY            — ключ Gemini (можно тот же, что и у рабочего бота — лимиты
                               считаются по ключу, так что если хочешь независимые лимиты
                               на еду и на работу, лучше завести второй ключ на ai.google.dev)
  UPSTASH_REDIS_REST_URL    — адрес ОТДЕЛЬНОЙ базы Upstash (не той же, что у рабочего бота,
                               чтобы история точно не пересеклась)
  UPSTASH_REDIS_REST_TOKEN  — токен этой базы
"""

import os
import json
import base64
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
MAX_HISTORY_TURNS = 10
HISTORY_TTL_SECONDS = 60 * 60 * 24 * 30

SYSTEM_INSTRUCTION = """Ты — доброжелательный личный помощник по питанию и снижению веса.
Пользователь присылает тебе текст или фотографии еды, а ты помогаешь:
- Прикидывать ПРИБЛИЗИТЕЛЬНЫЕ калории и БЖУ по виду блюда на фото — всегда явно
  говори, что это оценка на глаз, а не точное измерение, диапазоном, а не одним
  идеально точным числом
- Честно и по-доброму говорить, насколько блюдо подходит под цель снижения веса —
  без стыда и морализаторства, без деления еды на "хорошую" и "плохую" — вместо этого
  говори о балансе, размере порции, частоте, с чем лучше сочетать
- Предлагать рецепты и практичные альтернативы, если попросят
- Отвечать тепло и поддерживающе, как хороший тренер, а не как строгий контролёр

ВАЖНЫЕ ГРАНИЦЫ:
- Ты не заменяешь врача или дипломированного диетолога — если пользователь упоминает
  конкретный диагноз, серьёзное расстройство пищевого поведения, лекарства или
  медицинские противопоказания, мягко порекомендуй обратиться к специалисту, не давай
  точных медицинских назначений
- Никогда не поощряй экстремальное ограничение калорий, пропуск приёмов пищи как
  стратегию или любые нездоровые практики питания — если видишь такие сигналы, аккуратно
  и без осуждения предложи более сбалансированный подход
- Не критикуй внешность или вес пользователя, фокусируйся на еде и привычках"""


def redis_command(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    resp = requests.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                          json=list(args), timeout=10)
    resp.raise_for_status()
    return resp.json().get("result")


def load_history(chat_id):
    raw = redis_command("GET", f"chat_history:{chat_id}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_history(chat_id, history):
    trimmed = history[-MAX_HISTORY_TURNS * 2:]
    redis_command("SET", f"chat_history:{chat_id}", json.dumps(trimmed, ensure_ascii=False),
                  "EX", str(HISTORY_TTL_SECONDS))


def telegram_send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [""]
    for chunk in chunks:
        requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=30)


def telegram_get_file_path(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
    resp = requests.get(url, params={"file_id": file_id}, timeout=30)
    resp.raise_for_status()
    return resp.json()["result"]["file_path"]


def telegram_download_file(file_path):
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def call_gemini_text(history):
    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in history]
    payload = {"contents": contents, "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]}}
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=55)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_gemini_with_image(history, caption, image_bytes, mime_type):
    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in history]
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    text_part = {"text": caption.strip() if caption else
                 "Вот фото моей еды — расскажи примерно калории/БЖУ и подходит ли это под похудение."}
    image_part = {"inline_data": {"mime_type": mime_type, "data": b64}}
    contents.append({"role": "user", "parts": [text_part, image_part]})
    payload = {"contents": contents, "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]}}
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=55)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


GREETING_MESSAGE = (
    "Привет! Я твой личный помощник по питанию 🍎\n\n"
    "Вот чем могу помочь:\n"
    "— Присылай фото еды — прикину примерные калории и БЖУ, и скажу, насколько это "
    "вписывается в твою цель\n"
    "— Отвечу на вопросы про питание, помогу с идеями для рациона\n"
    "— Всё это без строгих запретов и стыда за еду — просто баланс и здравый смысл\n\n"
    "Для начала расскажи: какая у тебя сейчас цель — снизить вес, удержать текущий, "
    "набрать, или просто питаться получше? И к какому примерно сроку/темпу хотелось бы "
    "прийти, если это важно?"
)


def handle_update(update):
    message = update.get("message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    history = load_history(chat_id)

    # Первое знакомство — отдельное приветствие с вопросом про цели, а не обычный
    # ответ Gemini. Срабатывает и на команду /start (обычная кнопка "Start" в Telegram),
    # и на самое первое сообщение в чате, если по какой-то причине /start не было.
    user_text = message.get("text", "")
    if user_text.strip() == "/start" or not history:
        telegram_send_message(chat_id, GREETING_MESSAGE)
        history.append({"role": "model", "text": GREETING_MESSAGE})
        save_history(chat_id, history)
        return

    try:
        if "photo" in message:
            caption = message.get("caption", "")
            file_id = message["photo"][-1]["file_id"]
            file_path = telegram_get_file_path(file_id)
            image_bytes = telegram_download_file(file_path)
            reply = call_gemini_with_image(history, caption, image_bytes, "image/jpeg")
            history.append({"role": "user", "text": f"[прислал(а) фото еды] {caption}".strip()})
        elif "text" in message:
            history_with_new = history + [{"role": "user", "text": user_text}]
            reply = call_gemini_text(history_with_new)
            history.append({"role": "user", "text": user_text})
        else:
            telegram_send_message(chat_id, "Пришли текст или фото еды 🙂")
            return

        history.append({"role": "model", "text": reply})
        save_history(chat_id, history)
    except Exception as e:
        reply = f"Не получилось обработать сообщение: {e}"

    telegram_send_message(chat_id, reply)


@app.route("/", methods=["POST"])
@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    handle_update(update)
    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
@app.route("/api/webhook", methods=["GET"])
def health():
    memory_status = "включена" if (UPSTASH_URL and UPSTASH_TOKEN) else "выключена (нет переменных Upstash)"
    return jsonify({"status": "ok", "bot": "nutrition-assistant", "memory": memory_status})
