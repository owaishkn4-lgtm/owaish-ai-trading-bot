
import os
import time
import requests
from flask import Flask, jsonify

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
BASE_URL = "https://api.twelvedata.com/time_series"

MARKETS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "XAU/USD",
    "BTC/USD",
    "ETH/USD"
]

cache = {"time": 0, "results": []}


def ema(values, period):
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append(
            (value - result[-1]) * multiplier + result[-1]
        )
    return result


def calculate_rsi(prices):
    changes = [
        prices[i] - prices[i - 1]
        for i in range(1, len(prices))
    ]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14

    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50

    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def analyse_market(symbol):
    response = requests.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "interval": "5min",
            "outputsize": 60,
            "apikey": API_KEY
        },
        timeout=20
    )
    data = response.json()

    if not response.ok or "values" not in data:
        return {
            "symbol": symbol,
            "status": "unavailable",
            "message": data.get("message", "Market data unavailable")
        }

    prices = [
        float(item["close"])
        for item in reversed(data["values"])
    ]

    if len(prices) < 35:
        raise ValueError("Not enough price history")

    rsi = calculate_rsi(prices)

    fast = ema(prices, 12)
    slow = ema(prices, 26)
    offset = len(prices) - len(slow)
    macd_line = [
        fast[i + offset] - slow[i]
        for i in range(len(slow))
    ]
    signal_line = ema(macd_line, 9)

    if not signal_line:
        raise ValueError("MACD data unavailable")

    macd = macd_line[-1]
    signal = signal_line[-1]

    if macd > signal and 50 < rsi < 70:
        trade_signal = "BUY"
    elif macd < signal and 30 < rsi < 50:
        trade_signal = "SELL"
    else:
        trade_signal = "WAIT"

    return {
        "symbol": symbol,
        "price": prices[-1],
        "rsi": rsi,
        "macd": round(macd, 6),
        "macd_signal": round(signal, 6),
        "signal": trade_signal,
        "status": "success"
    }


@app.route("/")
def home():
    return "OWAISH AI TRADER API is running!"


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/scan")
def scan():
    if not API_KEY:
        return jsonify({"error": "API key not configured"}), 500

    # Cache results for 5 minutes to reduce API usage
    if time.time() - cache["time"] < 300 and cache["results"]:
        return jsonify({
            "bot": "OWAISH AI TRADER",
            "mode": "DEMO_ONLY",
            "markets": cache["results"]
        })

    results = []

    for symbol in MARKETS:
        try:
            results.append(analyse_market(symbol))
        except Exception:
            results.append({
                "symbol": symbol,
                "status": "error",
                "message": "Analysis unavailable. Try again later."
            })

    cache["time"] = time.time()
    cache["results"] = results

    return jsonify({
        "bot": "OWAISH AI TRADER",
        "mode": "DEMO_ONLY",
        "markets": results
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
