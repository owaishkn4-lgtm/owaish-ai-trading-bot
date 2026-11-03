import os
import time
import calendar
from datetime import timedelta

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("Set a fresh, strong SECRET_KEY environment variable in Render.")

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_NAME="owaish_session",
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

FRONTEND_ORIGIN = "https://owaish-ai-trading-bot.onrender.com"
DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("TWELVE_DATA_API_KEY")
BASE_URL = "https://api.twelvedata.com/time_series"
MARKETS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD", "BTC/USD", "ETH/USD"]

candle_cache = {}
scan_cache = {"time": 0, "results": []}
CANDLE_CACHE_SECONDS = 10
SCAN_CACHE_SECONDS = 60

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured in Render.")


def get_db_connection():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        sslmode="require",
    )


def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    symbol VARCHAR(30) NOT NULL,
                    side VARCHAR(10) NOT NULL CHECK (side IN ('BUY','SELL')),
                    entry_price DOUBLE PRECISION NOT NULL,
                    stop_loss DOUBLE PRECISION,
                    target DOUBLE PRECISION,
                    quantity DOUBLE PRECISION,
                    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                    entry_time TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    exit_price DOUBLE PRECISION,
                    exit_time TIMESTAMPTZ,
                    pnl DOUBLE PRECISION DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS demo_accounts (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    balance DOUBLE PRECISION NOT NULL DEFAULT 5000,
                    closed_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                    day_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
                    market_symbol VARCHAR(30) NOT NULL DEFAULT 'EUR/USD',
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
        app.logger.info("Database tables ready.")
    finally:
        conn.close()


init_db()


def current_user_id():
    return session.get("user_id")


def require_login():
    uid = current_user_id()
    if not uid:
        return None, (jsonify({"error": "Login required"}), 401)
    return uid, None


def valid_number(value, field, minimum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return number


@app.after_request
def add_security_headers(response):
    origin = request.headers.get("Origin")
    if origin == FRONTEND_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = FRONTEND_ORIGIN
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.route("/")
def home():
    return "OWAISH AI TRADER API is running!"


@app.route("/health")
def health():
    try:
        conn = get_db_connection()
        conn.close()
        return jsonify({"status": "ok", "database": "connected"})
    except Exception:
        app.logger.exception("Database health check failed")
        return jsonify({"status": "error", "database": "unavailable"}), 503


@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")

    if not email or "@" not in email or len(email) > 255:
        return jsonify({"error": "Enter a valid email"}), 400
    if not isinstance(password, str) or len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, password_hash)
                VALUES (%s, %s)
                RETURNING id, email
                """,
                (email, generate_password_hash(password)),
            )
            user = cur.fetchone()
            cur.execute(
                "INSERT INTO demo_accounts (user_id, balance) VALUES (%s, 5000)",
                (user["id"],),
            )

        conn.commit()
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]

        return jsonify({
            "message": "Registration successful",
            "user": dict(user),
        }), 201

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Email already registered"}), 409
    finally:
        conn.close()


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")

    if not email or not isinstance(password, str):
        return jsonify({"error": "Email and password required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash FROM users WHERE email = %s",
                (email,),
            )
            user = cur.fetchone()

            if not user or not check_password_hash(user["password_hash"], password):
                return jsonify({"error": "Invalid email or password"}), 401

            cur.execute(
                """
                INSERT INTO demo_accounts (user_id)
                VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user["id"],),
            )

        conn.commit()
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]

        return jsonify({
            "message": "Login successful",
            "user": {"id": user["id"], "email": user["email"]},
        })

    finally:
        conn.close()


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logout successful"})


@app.route("/api/me")
def me():
    uid, error = require_login()
    if error:
        return error

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email FROM users WHERE id = %s", (uid,))
            user = cur.fetchone()

        if not user:
            session.clear()
            return jsonify({"error": "User not found"}), 401

        return jsonify({"user": dict(user)})
    finally:
        conn.close()


@app.route("/api/account", methods=["GET", "PUT"])
def account():
    uid, error = require_login()
    if error:
        return error

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if request.method == "GET":
                cur.execute(
                    """
                    SELECT balance, closed_pnl, day_loss, market_symbol
                    FROM demo_accounts WHERE user_id = %s
                    """,
                    (uid,),
                )
                row = cur.fetchone()

                if not row:
                    cur.execute(
                        """
                        INSERT INTO demo_accounts (user_id)
                        VALUES (%s)
                        RETURNING balance, closed_pnl, day_loss, market_symbol
                        """,
                        (uid,),
                    )
                    row = cur.fetchone()
                    conn.commit()

                return jsonify(dict(row))

            data = request.get_json(silent=True) or {}
            balance = valid_number(data.get("balance"), "balance", 0)
            closed_pnl = valid_number(data.get("closed_pnl", 0), "closed_pnl")
            day_loss = valid_number(data.get("day_loss", 0), "day_loss", 0)
            symbol = str(data.get("market_symbol", "EUR/USD"))

            if symbol not in MARKETS:
                return jsonify({"error": "Unsupported market"}), 400

            cur.execute("""
                INSERT INTO demo_accounts
                    (user_id, balance, closed_pnl, day_loss, market_symbol, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    balance = EXCLUDED.balance,
                    closed_pnl = EXCLUDED.closed_pnl,
                    day_loss = EXCLUDED.day_loss,
                    market_symbol = EXCLUDED.market_symbol,
                    updated_at = CURRENT_TIMESTAMP
            """, (uid, balance, closed_pnl, day_loss, symbol))

        conn.commit()
        return jsonify({"message": "Demo account saved"})

    except ValueError as exc:
        conn.rollback()
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/trades", methods=["GET", "POST"])
def trades():
    uid, error = require_login()
    if error:
        return error

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if request.method == "GET":
                cur.execute("""
                    SELECT id, symbol, side, entry_price, stop_loss, target, quantity,
                           status, entry_time, exit_price, exit_time, pnl
                    FROM trades
                    WHERE user_id = %s
                    ORDER BY entry_time DESC
                    LIMIT 200
                """, (uid,))
                return jsonify({"trades": [dict(row) for row in cur.fetchall()]})

            data = request.get_json(silent=True) or {}
            symbol = str(data.get("symbol", ""))
            side = str(data.get("side", "")).upper()

            if symbol not in MARKETS or side not in ("BUY", "SELL"):
                return jsonify({"error": "Invalid symbol or side"}), 400

            entry = valid_number(data.get("entry_price"), "entry_price", 0.0000001)
            stop = valid_number(data.get("stop_loss"), "stop_loss", 0.0000001)
            target = valid_number(data.get("target"), "target", 0.0000001)
            quantity = valid_number(data.get("quantity", 0), "quantity", 0)

            cur.execute("""
                INSERT INTO trades
                    (user_id, symbol, side, entry_price, stop_loss, target, quantity, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN')
                RETURNING id, symbol, side, entry_price, stop_loss, target,
                          quantity, status, entry_time
            """, (uid, symbol, side, entry, stop, target, quantity))

            row = cur.fetchone()

        conn.commit()
        return jsonify({"trade": dict(row)}), 201

    except ValueError as exc:
        conn.rollback()
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/trades/<int:trade_id>/close", methods=["POST"])
def close_trade(trade_id):
    uid, error = require_login()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    try:
        exit_price = valid_number(data.get("exit_price"), "exit_price", 0.0000001)
        pnl = valid_number(data.get("pnl"), "pnl")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE trades
                SET status = 'CLOSED',
                    exit_price = %s,
                    exit_time = CURRENT_TIMESTAMP,
                    pnl = %s
                WHERE id = %s AND user_id = %s AND status = 'OPEN'
                RETURNING id, status, exit_price, exit_time, pnl
            """, (exit_price, pnl, trade_id, uid))

            row = cur.fetchone()

        if not row:
            conn.rollback()
            return jsonify({"error": "Open trade not found"}), 404

        conn.commit()
        return jsonify({"trade": dict(row)})
    finally:
        conn.close()


def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    result = [sum(values[:period]) / period]

    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])

    return result


def calculate_rsi(prices):
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    if not changes:
        return 50.0

    recent = changes[-14:]
    gains = [max(value, 0) for value in recent]
    losses = [max(-value, 0) for value in recent]
    avg_gain = sum(gains) / len(recent)
    avg_loss = sum(losses) / len(recent)

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def fetch_one_minute_candles(symbol, outputsize=500):
    if not API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured in Render.")

    response = requests.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "interval": "1min",
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": API_KEY,
        },
        timeout=20,
    )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Market-data provider returned an unreadable response.")

    if not response.ok or "values" not in data:
        message = data.get("message") or data.get("code") or "Market data unavailable"
        raise RuntimeError(str(message))

    rows = []
    for item in reversed(data["values"]):
        parsed = time.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S")
        timestamp = calendar.timegm(parsed)
        rows.append({
            "time": timestamp,
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
        })

    return rows


def aggregate_candles(source_rows, minutes):
    grouped = {}

    for candle in source_rows:
        bucket = (candle["time"] // (minutes * 60)) * (minutes * 60)

        if bucket not in grouped:
            grouped[bucket] = {
                "time": bucket,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
            }
        else:
            grouped[bucket]["high"] = max(grouped[bucket]["high"], candle["high"])
            grouped[bucket]["low"] = min(grouped[bucket]["low"], candle["low"])
            grouped[bucket]["close"] = candle["close"]

    return sorted(grouped.values(), key=lambda row: row["time"])


def get_candles_cached(symbol, minutes):
    key = (symbol, minutes)
    now = time.time()
    cached = candle_cache.get(key)

    if cached and now - cached["time"] < CANDLE_CACHE_SECONDS:
        return cached["candles"]

    source = fetch_one_minute_candles(symbol, outputsize=500)
    result = aggregate_candles(source, minutes)
    candle_cache[key] = {"time": now, "candles": result}
    return result


def analyse_market(symbol):
    rows = fetch_one_minute_candles(symbol, outputsize=100)
    prices = [row["close"] for row in rows]

    if len(prices) < 35:
        raise ValueError("Not enough price history")

    rsi = calculate_rsi(prices)
    fast = ema(prices, 12)
    slow = ema(prices, 26)
    macd_line = [fast[i + 14] - slow[i] for i in range(len(slow))]
    signal_line = ema(macd_line, 9)

    if not signal_line:
        raise ValueError("MACD data unavailable")

    macd = macd_line[-1]
    signal = signal_line[-1]

    trade_signal = (
        "BUY" if macd > signal and 50 < rsi < 70
        else "SELL" if macd < signal and 30 < rsi < 50
        else "WAIT"
    )

    reason = (
        "Illustrative bullish indicator alignment" if trade_signal == "BUY"
        else "Illustrative bearish indicator alignment" if trade_signal == "SELL"
        else "No clear indicator alignment"
    )

    return {
        "symbol": symbol,
        "price": prices[-1],
        "rsi": rsi,
        "macd": round(macd, 8),
        "macd_signal": round(signal, 8),
        "signal": trade_signal,
        "reason": reason,
        "status": "success",
    }


@app.route("/api/candles")
def candles():
    if not API_KEY:
        return jsonify({"error": "TWELVE_DATA_API_KEY is not configured"}), 500

    symbol = request.args.get("symbol", "EUR/USD")
    timeframe = request.args.get("timeframe", "1")

    if symbol not in MARKETS:
        return jsonify({"error": "Unsupported market"}), 400

    try:
        minutes = int(timeframe)
    except (TypeError, ValueError):
        return jsonify({"error": "Timeframe must be a whole number from 1 to 10"}), 400

    if minutes < 1 or minutes > 10:
        return jsonify({"error": "Timeframe must be between 1 and 10 minutes"}), 400

    try:
        rows = get_candles_cached(symbol, minutes)

        if not rows:
            return jsonify({"error": "No candle data returned by provider"}), 502

        return jsonify({
            "symbol": symbol,
            "interval": f"{minutes}min",
            "candles": rows,
        })

    except Exception as exc:
        app.logger.exception("Candle request failed")
        return jsonify({"error": str(exc)}), 502


@app.route("/api/scan")
def scan():
    if not API_KEY:
        return jsonify({"error": "TWELVE_DATA_API_KEY is not configured"}), 500

    now = time.time()

    if scan_cache["results"] and now - scan_cache["time"] < SCAN_CACHE_SECONDS:
        return jsonify({
            "bot": "OWAISH AI TRADER",
            "mode": "DEMO_ONLY",
            "markets": scan_cache["results"],
        })

    results = []

    for symbol in MARKETS:
        try:
            results.append(analyse_market(symbol))
        except Exception as exc:
            app.logger.warning("Analysis unavailable for %s: %s", symbol, exc)
            results.append({
                "symbol": symbol,
                "status": "unavailable",
                "signal": "WAIT",
                "message": str(exc),
            })

    scan_cache["time"] = now
    scan_cache["results"] = results

    return jsonify({
        "bot": "OWAISH AI TRADER",
        "mode": "DEMO_ONLY",
        "markets": results,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
