
import os
import time
import sqlite3
import threading
import requests

from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
DB = "bot.db"


def db_init():
    with sqlite3.connect(DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)


def send_telegram(message, chat_id=None):
    if not BOT_TOKEN:
        return

    if chat_id is not None:
        chats = [(chat_id,)]
    else:
        with sqlite3.connect(DB) as con:
            chats = con.execute(
                "SELECT chat_id FROM chats"
            ).fetchall()

    for (target_chat_id,) in chats:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": target_chat_id,
                    "text": message
                },
                timeout=10
            )
        except Exception as e:
            print("Telegram error:", e)


def ema(values, period):
    result = values[0]
    alpha = 2 / (period + 1)

    for value in values[1:]:
        result = value * alpha + result * (1 - alpha)

    return result


def rsi(values, period=14):
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    recent = changes[-period:]
    gains = [max(x, 0) for x in recent]
    losses = [max(-x, 0) for x in recent]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze(candles):
    if len(candles) < 35:
        return None

    closes = [c["close"] for c in candles]

    fast = ema(closes, 9)
    slow = ema(closes, 21)
    strength = rsi(closes, 14)
    macd = ema(closes, 12) - ema(closes, 26)

    last = candles[-1]
    score = 0

    if fast > slow:
        score += 1
    elif fast < slow:
        score -= 1

    if strength > 50:
        score += 1
    elif strength < 50:
        score -= 1

    if macd > 0:
        score += 1
    elif macd < 0:
        score -= 1

    if last["close"] > last["open"]:
        score += 1
    elif last["close"] < last["open"]:
        score -= 1

    if score >= 3:
        direction = "CALL 🟢"
    elif score <= -3:
        direction = "PUT 🔴"
    else:
        return None

    return (
        "📊 POCKET SIGNAL PRO\n\n"
        "Ринок: REGULAR\n"
        "Інструмент: EUR/USD\n"
        "Таймфрейм: M1\n"
        f"Сигнал: {direction}\n"
        f"RSI: {strength:.1f}\n"
        f"EMA 9: {fast:.5f}\n"
        f"EMA 21: {slow:.5f}\n"
        f"MACD: {macd:.5f}\n"
        f"Сила умов: {abs(score)}/4\n\n"
        "⚠️ Аналітичний сигнал, не гарантія результату."
    )


def fetch_eurusd_signal(chat_id):
    if not TWELVE_DATA_API_KEY:
        send_telegram(
            "Не налаштований ключ Twelve Data на сервері.",
            chat_id
        )
        return

    try:
        response = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": "EUR/USD",
                "interval": "1min",
                "outputsize": 60,
                "apikey": TWELVE_DATA_API_KEY
            },
            timeout=20
        )

        payload = response.json()

        if (
            response.status_code != 200
            or payload.get("status") == "error"
            or not payload.get("values")
        ):
            detail = payload.get(
                "message",
                "Не вдалося отримати котирування."
            )
            send_telegram(
                f"Помилка Twelve Data: {detail}",
                chat_id
            )
            return

        # Найновіша свічка може бути ще незакритою.
        # Для аналізу беремо попередні свічки.
        rows = payload["values"][1:]
        rows.reverse()

        candles = []

        for row in rows:
            candles.append({
                "open": float(row["open"]),
                "close": float(row["close"])
            })

        signal = analyze(candles)

        if signal:
            send_telegram(signal, chat_id)
        else:
            send_telegram(
                "📊 EUR/USD M1\n"
                "Поки немає достатньо узгоджених умов для сигналу.\n"
                "Не відкривай угоду лише через відсутність сигналу.",
                chat_id
            )

    except Exception as e:
        print("Twelve Data error:", e)
        send_telegram(
            "Не вдалося отримати дані EUR/USD. Спробуй пізніше.",
            chat_id
        )


@app.route("/health")
def health():
    return jsonify({"status": "online"})


@app.route("/candles", methods=["POST"])
def candles():
    if request.headers.get("X-Ingest-Token") != INGEST_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    if data.get("market") not in ("OTC", "REGULAR"):
        return jsonify({"error": "Invalid market"}), 400

    if data.get("timeframe") != "M1":
        return jsonify({"error": "Invalid timeframe"}), 400

    if not data.get("candles"):
        return jsonify({"error": "No candles"}), 400

    try:
        if not data["candles"][-1].get("closed", False):
            return jsonify({
                "status": "waiting",
                "signal": "LAST CANDLE NOT CLOSED"
            })

        signal = analyze([
            {
                "open": float(c["open"]),
                "close": float(c["close"])
            }
            for c in data["candles"]
        ])

    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if signal:
        send_telegram(signal)

    return jsonify({
        "status": "ok",
        "signal": signal or "NO SIGNAL"
    })


def telegram_listener():
    offset = None

    while True:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 25
                },
                timeout=35
            ).json()

            for update in response.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                command = message.get("text", "").strip()

                if not chat_id:
                    continue

                if command == "/start":
                    with sqlite3.connect(DB) as con:
                        con.execute(
                            "INSERT OR IGNORE INTO chats VALUES (?)",
                            (chat_id,)
                        )

                    send_telegram(
                        "🚀 POCKET SIGNAL PRO\n\n"
                        "Бот підключено!\n"
                        "REGULAR: EUR/USD\n"
                        "Таймфрейм: M1\n\n"
                        "/eurusd — аналіз EUR/USD\n"
                        "/status — стан бота\n\n"
                        "OTC поки не підключений.",
                        chat_id
                    )

                elif command == "/status":
                    send_telegram(
                        "Pocket Signal PRO працює.\n"
                        "EUR/USD: команда /eurusd.\n"
                        "OTC: ще не підключений.",
                        chat_id
                    )

                elif command == "/eurusd":
                    send_telegram(
                        "Отримую дані EUR/USD M1...",
                        chat_id
                    )
                    fetch_eurusd_signal(chat_id)

        except Exception as e:
            print("Listener error:", e)
            time.sleep(5)


db_init()

if BOT_TOKEN:
    threading.Thread(
        target=telegram_listener,
        daemon=True
    ).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000))
    )
