#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OANDA Bridge v2.2 — Python-only MES Trade Executor (AWS Lambda Ready)

Features:
- Pure MES→OANDA JSON contract (no TradingView logic)
- POST /webhook executes trades
- GET /ping for quick health checks
- GET /status returns account summary + open positions
- Telegram notifications for fills, rejections, and errors
- Clean structure for future features (close_all, simulate, logs)

Deployed: 2025-12-03
"""

# ─────────────────────────────────────────────────────────────
# Imports
# ─────────────────────────────────────────────────────────────
import json
import os
import logging
import requests
from datetime import datetime


# ─────────────────────────────────────────────────────────────
# Environment / Constants
# ─────────────────────────────────────────────────────────────
APP_NAME = "oanda_bridge"
__version__ = "2.2"

def env(name, default=None):
    return os.environ.get(name) or default

# OANDA mode
OANDA_ENV = env("OANDA_ENV", "practice").lower()
API_DOMAIN = "api-fxtrade.oanda.com" if OANDA_ENV == "live" else "api-fxpractice.oanda.com"

# Keys + IDs
OANDA_API_KEY = env("OANDA_LIVE_API_KEY" if OANDA_ENV == "live" else "OANDA_PRACTICE_API_KEY")
OANDA_ACCOUNT_ID = env("OANDA_LIVE_ACCOUNT_ID" if OANDA_ENV == "live" else "OANDA_PRACTICE_ACCOUNT_ID")

# Telegram
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")

# Instrument size limits
MAX_UNITS = int(env("MAX_UNITS", "12000"))
BASE_CURRENCY = env("BASE_CURRENCY", "USD").upper()

# Base URLs
BASE_URL = f"https://{API_DOMAIN}/v3/accounts/{OANDA_ACCOUNT_ID}"
URL_ORDER = f"{BASE_URL}/orders"
URL_SUMMARY = f"{BASE_URL}/summary"
URL_POSITIONS = f"{BASE_URL}/positions"


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(APP_NAME)


# ─────────────────────────────────────────────────────────────
# Utility Helpers
# ─────────────────────────────────────────────────────────────
def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OANDA_API_KEY}"
    }


def http_post(url, payload):
    try:
        r = requests.post(url, headers=headers(), json=payload, timeout=10)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        log.error(f"POST {url} failed: {e}")
        return 0, {"error": str(e)}


def send_telegram(msg: str):
    """Send Telegram notification if bot/chat configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        log.error(f"Telegram failed: {e}")


# ─────────────────────────────────────────────────────────────
# OANDA Query Helpers
# ─────────────────────────────────────────────────────────────
def get_account_status():
    try:
        r = requests.get(URL_SUMMARY, headers=headers(), timeout=10)
        if r.status_code != 200:
            return None, f"Status {r.status_code}"
        return r.json().get("account", {}), None
    except Exception as e:
        return None, str(e)


def get_open_positions():
    try:
        r = requests.get(URL_POSITIONS, headers=headers(), timeout=10)
        if r.status_code != 200:
            return []
        return r.json().get("positions", [])
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# Order Building
# ─────────────────────────────────────────────────────────────
def build_order(instrument: str, units: int, sl: float, tp: float):
    """Build market order payload for OANDA."""
    return {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {"price": f"{tp:.5f}"},
            "stopLossOnFill": {"price": f"{sl:.5f}"}
        }
    }


# ─────────────────────────────────────────────────────────────
# Lambda Handler
# ─────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    try:
        # Extract HTTP context
        http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "")
        path = event.get("path") or event.get("requestContext", {}).get("http", {}).get("path", "")

        # ─────────────────────────────────────────────────────
        # GET /ping — basic health check
        # ─────────────────────────────────────────────────────
        if http_method == "GET" and path.endswith("/ping"):
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "ok",
                    "version": __version__,
                    "env": OANDA_ENV,
                    "account": OANDA_ACCOUNT_ID
                })
            }

        # ─────────────────────────────────────────────────────
        # GET /status — account summary + open positions
        # ─────────────────────────────────────────────────────
        if http_method == "GET" and path.endswith("/status"):
            acct, err = get_account_status()
            positions = get_open_positions()

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "ok",
                    "version": __version__,
                    "env": OANDA_ENV,
                    "account": OANDA_ACCOUNT_ID,
                    "balance": acct.get("balance") if acct else None,
                    "NAV": acct.get("NAV") if acct else None,
                    "marginUsed": acct.get("marginUsed") if acct else None,
                    "openPositions": positions
                })
            }

        # ─────────────────────────────────────────────────────
        # POST /webhook — MES trade execution
        # ─────────────────────────────────────────────────────
        if http_method == "POST" and path.endswith("/webhook"):

            body = event.get("body", "{}")
            data = json.loads(body) if isinstance(body, str) else body

            side = data.get("message", "").upper()
            inst = data.get("instrument", "").upper()
            price = float(data.get("price", 0.0))
            sl = float(data.get("sl", 0.0))
            tp = float(data.get("tp", 0.0))
            qty = int(data.get("qty", 0))

            # Validation
            if side not in ("BUY", "SELL") or not inst or qty <= 0:
                return {"statusCode": 400, "body": json.dumps({"error": "Invalid MES payload"})}

            # Convert BUY qty → positive, SELL → negative
            units = qty if side == "BUY" else -qty

            order_body = build_order(inst, units, sl, tp)
            code, resp = http_post(URL_ORDER, order_body)

            # Failed
            if code != 201:
                log.error(f"OANDA rejected: {resp}")
                send_telegram(f"❌ OANDA Rejected\n{inst} {side}\nUnits={units}\nReason: {resp}")
                return {"statusCode": 400, "body": json.dumps({"status": "error", "oanda": resp})}

            # Success
            log.info(f"[FILLED] {inst} {side} {units} SL={sl} TP={tp}")
            send_telegram(f"✔️ Trade Filled\n{inst} {side} {units}\nSL={sl}\nTP={tp}")

            return {
                "statusCode": 201,
                "body": json.dumps({
                    "status": "ok",
                    "instrument": inst,
                    "side": side,
                    "units": units
                })
            }

        # ─────────────────────────────────────────────────────
        # Invalid route
        # ─────────────────────────────────────────────────────
        return {
            "statusCode": 404,
            "body": json.dumps({"error": f"Invalid route: {path}"})
        }

    except Exception as e:
        log.error(f"Handler error: {e}")
        send_telegram(f"⚠️ Bridge Error: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
