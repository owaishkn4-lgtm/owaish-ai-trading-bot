import os
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
BASE_URL = "https://api.twelvedata.com/price"

MARKETS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "XAU/USD",
    "BTC/USD",
    "ETH/USD"
]

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

    results = []

    for symbol in MARKETS:
        try:
            response = requests.get(
                BASE_URL,
                params={"symbol": symbol},
                headers={"Authorization": f"apikey {API_KEY}"},
                timeout=15
            )
            data = response.json()

            if response.ok and "price" in data:
                results.append({
                    "symbol": symbol,
                    "price": data["price"],
                    "status": "success"
                })
            else:
                results.append({
                    "symbol": symbol,
                    "status": "unavailable",
                    "message": data.get("message", "Data unavailable")
                })

        except requests.RequestException:
            results.append({
                "symbol": symbol,
                "status": "error",
                "message": "Connection error"
            })

    return jsonify({
        "bot": "OWAISH AI TRADER",
        "mode": "DEMO_ONLY",
        "markets": results
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
