#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OANDA Bridge v2.0 — Python-only MES trade executor (AWS Lambda Ready)
Cleaned of all TradingView logic.

Accepts payloads ONLY from MES, using this format:

{
  "message": "BUY" | "SELL",
  "instrument": "GBP_USD",
  "price": 1.33441,
  "sl": 1.33000,
  "tp": 1.33800,
  "qty": 8000
}

Version: v2.0 — 2025-12-03
"""

import json
import os
import logging
import requests
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────
APP_NAME = "oanda_bridge"
__version__ = "2.0"

def env(name, default=None):
    return os.environ.get(name) or default

OANDA_ENV = env("OANDA_ENV", "practice").lower()
API_DOMAIN = "api-fxtrade.oanda.com" if OANDA_ENV == "live" else "api-fxpractice.oanda.com"

OANDA_API_KEY = env("OANDA_LIVE_API_KEY" if OANDA_ENV == "live" else "OANDA_PRACTICE_API_KEY")
OANDA_ACCOUNT_ID = env("OANDA_LIVE_ACCOUNT_ID" if OANDA_ENV == "live" else "OANDA_PRACTICE_ACCOUNT_ID")

MAX_UNITS = int(env("MAX_UNITS", "12000"))
BASE_CURRENCY = env("BASE_CURRENCY", "USD").upper()

BASE_URL = f"https://{API_DOMAIN}/v3/accounts/{OANDA_ACCOUNT_ID}"
URL_ORDER = f"{BASE_URL}/orders"
URL_PRICING = f"{BASE_URL}/pricing"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(APP_NAME)

# ─────────────────────────────────────────────────────────────
# Helpers
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

# ─────────────────────────────────────────────────────────────
# Order Execution
# ─────────────────────────────────────────────────────────────

def build_order(inst, units, sl, tp):
    return {
        "order": {
            "type": "MARKET",
            "instrument": inst,
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
    """MES-only webhook handler"""
    try:
        body = event.get("body", "{}")
        data = json.loads(body) if isinstance(body, str) else body

        side = data.get("message", "").upper()
        inst = data.get("instrument", "").upper()
        price = float(data.get("price", 0.0))
        sl = float(data.get("sl", 0.0))
        tp = float(data.get("tp", 0.0))
        qty = int(data.get("qty", 0))

        if side not in ("BUY", "SELL") or not inst or qty <= 0:
            return {"statusCode": 400, "body": json.dumps({"error": "Invalid MES payload"})}

        # Flip negative qty for SELL
        units = qty if side == "BUY" else -qty

        order_body = build_order(inst, units, sl, tp)
        code, resp = http_post(URL_ORDER, order_body)

        ok = code == 201
        if not ok:
            log.error(f"OANDA rejected: {resp}")
            return {"statusCode": 400, "body": json.dumps({"status": "error", "oanda": resp})}

        log.info(f"[FILLED] {inst} {side} {units} SL={sl} TP={tp}")

        return {
            "statusCode": 201,
            "body": json.dumps({
                "status": "ok",
                "instrument": inst,
                "side": side,
                "units": units
            })
        }

    except Exception as e:
        log.error(f"Handler error: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
