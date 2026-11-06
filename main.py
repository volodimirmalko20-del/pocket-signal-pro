import os
import time
import sqlite3
import threading
import requests

from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
DB = "bot.db"

def db_init():
    with sqlite3.connect(DB) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY
        )
        """)

def send_telegram(message):
    if not BOT_TOKEN:
        return

    with sqlite3.connect(DB) as con:
        chats = con.execute("SELECT chat_id FROM chats").fetchall()

    for (chat_id,) in chats:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": message},
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
        values[i] - values[i-1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze(data):
    market = data.get("market")
    candles = data.get("candles", [])

    if market not in ("OTC", "REGULAR"):
        return None

    if data.get("timeframe") != "M1":
        return None

    if len(candles) < 35:
        return None

    if not candles[-1].get("closed", False):
        return None

    closes = [float(c["close"]) for c in candles]

    fast = ema(closes, 9)
    slow = ema(closes, 21)
    strength = rsi(closes)

    macd = ema(closes, 12) - ema(closes, 26)
    previous = candles[-1]

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

    if float(previous["close"]) > float(previous["open"]):
        score += 1
    elif float(previous["close"]) < float(previous["open"]):
        score -= 1

    if score >= 3:
        direction = "CALL 🟢"
    elif score <= -3:
        direction = "PUT 🔴"
    else:
        return None

    return (
        f"📊 POCKET SIGNAL PRO\n\n"
        f"Ринок: {market}\n"
        f"Таймфрейм: M1\n"
        f"Сигнал: {direction}\n"
        f"RSI: {strength:.1f}\n"
        f"EMA 9: {fast:.5f}\n"
        f"EMA 21: {slow:.5f}\n"
        f"MACD: {macd:.5f}\n"
        f"Сила сигналу: {abs(score)}/4\n\n"
        f"⚠️ Аналітичний сигнал, не гарантія результату."
    )

@app.route("/health")
def health():
    return jsonify({"status": "online"})

@app.route("/candles", methods=["POST"])
def candles():
    if request.headers.get("X-Ingest-Token") != INGEST_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    try:
        signal = analyze(data)
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
                params={"offset": offset, "timeout": 25},
                timeout=35
            ).json()

            for update in response.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                text = message.get("text", "")

                if not chat_id:
                    continue

                if text == "/start":
                    with sqlite3.connect(DB) as con:
                        con.execute(
                            "INSERT OR IGNORE INTO chats VALUES (?)",
                            (chat_id,)
                        )

                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": (
                                "🚀 Pocket Signal PRO підключено!\n\n"
                                "Бот готовий приймати аналітичні дані.\n"
                                "Ринки: REGULAR та OTC\n"
                                "Таймфрейм: M1\n"
                                "Команда /status — стан бота."
                            )
                        },
                        timeout=10
                    )

                elif text == "/status":
                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": "Pocket Signal PRO працює. Очікує дані ринку."
                        },
                        timeout=10
                    )

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
