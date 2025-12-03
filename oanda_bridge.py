#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OANDA Bridge v2.4 — MES-Only Trade Executor (AWS Lambda Ready)

Features:
- Pure MES JSON contract (no TradingView support)
- POST /webhook executes trades using MES-sized qty
- GET /ping: quick health check + balance
- GET /status: account summary + open positions
- Telegram notifications for fills, rejects, and crashes
- Cached pip size (10-minute TTL) to reduce OANDA metadata calls
- Enforces minimum SL/TP distance (3 pips) using cached pip size
- Request ID tagging in logs and Telegram for traceability

Deployed: 2025-12-03
"""

# ─────────────────────────────────────────────────────────────
# ⬇ Imports
# ─────────────────────────────────────────────────────────────
import json
import os
import logging
import requests
import urllib3
from datetime import datetime
from typing import Optional, Dict, Any
from functools import lru_cache
from time import time

# Silence noisy SSL warnings in Lambda logs (safe for OANDA)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────────────────────
# ⬇ Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oanda_bridge")

# ─────────────────────────────────────────────────────────────
# ⬇ Environment & Constants
# ─────────────────────────────────────────────────────────────
def env(name: str, default: Optional[str] = None) -> str:
    return os.environ.get(name) or default or ""

APP_NAME = "oanda_bridge"
__version__ = "2.4"

OANDA_ENV = env("OANDA_ENV", "practice").lower()
API_DOMAIN = "api-fxtrade.oanda.com" if OANDA_ENV == "live" else "api-fxpractice.oanda.com"

OANDA_API_KEY = env("OANDA_LIVE_API_KEY" if OANDA_ENV == "live" else "OANDA_PRACTICE_API_KEY")
OANDA_ACCOUNT_ID = env("OANDA_LIVE_ACCOUNT_ID" if OANDA_ENV == "live" else "OANDA_PRACTICE_ACCOUNT_ID")

MAX_UNITS = int(env("MAX_UNITS", "12000"))
BASE_CURRENCY = env("BASE_CURRENCY", "USD").upper()

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")

OANDA_API_BASE = f"https://{API_DOMAIN}/v3"
ACCOUNTS_URL = f"{OANDA_API_BASE}/accounts/{OANDA_ACCOUNT_ID}"
ORDERS_URL = f"{ACCOUNTS_URL}/orders"
SUMMARY_URL = f"{ACCOUNTS_URL}/summary"
POSITIONS_URL = f"{ACCOUNTS_URL}/positions"
INSTRUMENTS_URL = f"{ACCOUNTS_URL}/instruments"

# ─────────────────────────────────────────────────────────────
# ⬇ Utilities
# ─────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def jget(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default) if isinstance(d, dict) else default


def is_jpy_pair(instrument: str) -> bool:
    return instrument.upper().endswith("_JPY")


def oanda_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }

# ─────────────────────────────────────────────────────────────
# ⬇ Telegram Notifications
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    """Send Telegram notification if configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5,
        )
    except Exception as e:
        log.error(f"Telegram failed: {e}")

# ─────────────────────────────────────────────────────────────
# ⬇ HTTP Helpers
# ─────────────────────────────────────────────────────────────
def http_get(url: str, params: Optional[Dict[str, Any]] = None):
    try:
        r = requests.get(url, headers=oanda_headers(), params=params, timeout=10)
        return r.status_code, (r.json() if r.content else {})
    except Exception as e:
        log.error(f"GET {url} failed: {e}")
        return 0, {"error": str(e)}


def http_post(url: str, payload: Dict[str, Any]):
    try:
        r = requests.post(url, headers=oanda_headers(), json=payload, timeout=10)
        return r.status_code, (r.json() if r.content else {})
    except Exception as e:
        log.error(f"POST {url} failed: {e}")
        return 0, {"error": str(e)}

# ─────────────────────────────────────────────────────────────
# ⬇ Pip Size Helpers (with TTL Cache)
# ─────────────────────────────────────────────────────────────
def get_pip_size(instrument: str) -> float:
    """
    Query OANDA for pip size via pipLocation.
    Fallback to 0.01 for JPY pairs or 0.0001 otherwise.
    """
    code, data = http_get(INSTRUMENTS_URL, params={"instruments": instrument})
    if code == 200 and data.get("instruments"):
        loc = data["instruments"][0].get("pipLocation", -4)
        try:
            return 10 ** loc
        except Exception:
            pass
    return 0.01 if is_jpy_pair(instrument) else 0.0001


# TTL cache: recalculates every 10 minutes via changing _ts
@lru_cache(maxsize=64)
def get_pip_size_cached(instrument: str, _ts: int = int(time() // 600)) -> float:
    return get_pip_size(instrument)

# ─────────────────────────────────────────────────────────────
# ⬇ SL/TP Adjustment (Min 3 Pips)
# ─────────────────────────────────────────────────────────────
def adjust_sl_tp(instrument: str, entry: float, sl: float, tp: float) -> tuple[float, float]:
    """
    Enforce a minimum SL/TP distance of 3 pips using cached pip size.
    Doesn't alter direction (BUY/SELL); only widens too-tight stops/targets.
    """
    pip = get_pip_size_cached(instrument)
    min_pips = 3
    mindist = min_pips * pip

    orig_sl, orig_tp = sl, tp

    # BUY structure: SL < entry < TP
    if tp > entry and sl < entry:
        if (entry - sl) < mindist:
            sl = entry - mindist
        if (tp - entry) < mindist:
            tp = entry + mindist

    # SELL structure: TP < entry < SL
    elif tp < entry and sl > entry:
        if (sl - entry) < mindist:
            sl = entry + mindist
        if (entry - tp) < mindist:
            tp = entry - mindist

    if sl != orig_sl or tp != orig_tp:
        log.info(f"[ADJUST] {instrument} SL {orig_sl}→{sl} | TP {orig_tp}→{tp}")

    return sl, tp

# ─────────────────────────────────────────────────────────────
# ⬇ Account & Position Helpers
# ─────────────────────────────────────────────────────────────
def get_account_summary():
    return http_get(SUMMARY_URL)


def get_positions():
    code, data = http_get(POSITIONS_URL)
    return data.get("positions", []) if code == 200 else []

# ─────────────────────────────────────────────────────────────
# ⬇ Order Creation
# ─────────────────────────────────────────────────────────────
def build_market_order(instrument: str, units: int, sl: float, tp: float, client_tag: str) -> Dict[str, Any]:
    """Build OANDA MARKET order payload with SL/TP."""
    return {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {
                "id": client_tag,
                "tag": "oanda_bridge",
                "comment": "MES auto-trade"
            },
            "takeProfitOnFill": {"price": f"{tp:.5f}"},
            "stopLossOnFill": {"price": f"{sl:.5f}"}
        }
    }

# ─────────────────────────────────────────────────────────────
# ⬇ Lambda Handler (v2.4)
# ─────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    # Tag everything with request_id for tracing
    request_id = getattr(context, "aws_request_id", "local") if context else "local"

    try:
        http_method = event.get("httpMethod", "")
        path = event.get("path", "")

        log.info(f"[{request_id}] {http_method} {path}")

        # ─────────────────────────────────────────────────────
        # GET /ping — Basic health check
        # ─────────────────────────────────────────────────────
        if http_method == "GET" and path.endswith("/ping"):
            code, data = get_account_summary()
            acct = data.get("account", {}) if code == 200 else {}
            bal = acct.get("balance")

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "ok",
                    "version": __version__,
                    "env": OANDA_ENV,
                    "account": OANDA_ACCOUNT_ID,
                    "balance": bal
                })
            }

        # ─────────────────────────────────────────────────────
        # GET /status — Account summary + open positions
        # ─────────────────────────────────────────────────────
        if http_method == "GET" and path.endswith("/status"):
            code, data = get_account_summary()
            acct = data.get("account", {}) if code == 200 else {}
            positions = get_positions()

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "ok",
                    "version": __version__,
                    "env": OANDA_ENV,
                    "account": OANDA_ACCOUNT_ID,
                    "balance": acct.get("balance"),
                    "NAV": acct.get("NAV"),
                    "marginUsed": acct.get("marginUsed"),
                    "openPositions": positions
                })
            }

        # ─────────────────────────────────────────────────────
        # POST /webhook — MES Trade Execution (price + qty)
        # ─────────────────────────────────────────────────────
        if http_method == "POST" and path.endswith("/webhook"):
            raw_body = event.get("body", "{}")

            try:
                payload = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
            except json.JSONDecodeError as je:
                log.error(f"[{request_id}] Invalid JSON: {je}")
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "Invalid JSON"})
                }

            side = str(jget(payload, "message", "")).upper().strip()
            instrument = str(jget(payload, "instrument", "")).upper().strip()
            price = float(jget(payload, "price", 0.0))
            sl = float(jget(payload, "sl", 0.0))
            tp = float(jget(payload, "tp", 0.0))
            qty = int(jget(payload, "qty", 0))

            # Normalize instrument to OANDA format: EURUSD → EUR_USD
            if "_" not in instrument and len(instrument) >= 6:
                instrument = f"{instrument[:3]}_{instrument[3:]}"
            instrument = instrument.replace("/", "_")

            # Basic validation
            if side not in ("BUY", "SELL") or not instrument or price <= 0 or sl <= 0 or tp <= 0 or qty <= 0:
                log.error(f"[{request_id}] Invalid MES payload: {payload}")
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "Invalid MES payload"})
                }

            # Enforce min SL/TP distance (3 pips)
            sl, tp = adjust_sl_tp(instrument, price, sl, tp)

            # Use MES-sized qty directly (no internal risk sizing)
            units = qty if side == "BUY" else -qty
            if abs(units) > MAX_UNITS:
                units = MAX_UNITS if units > 0 else -MAX_UNITS

            client_tag = f"{APP_NAME}-{request_id}"
            order_body = build_market_order(instrument, units, sl, tp, client_tag)

            code, resp = http_post(ORDERS_URL, order_body)
            ok = (code == 201)

            if not ok:
                log.error(f"[{request_id}] OANDA rejected: {resp}")
                send_telegram(
                    f"❌ OANDA Rejected [{request_id}]\n"
                    f"{instrument} {side} {units}\n"
                    f"Reason: {resp}"
                )
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "OANDA rejected", "oanda": resp})
                }

            log.info(f"[{request_id}] FILLED {instrument} {side} {units} SL={sl} TP={tp}")
            send_telegram(
                f"✔️ Trade Filled [{request_id}]\n"
                f"{instrument} {side} {units}\n"
                f"SL={sl} TP={tp}"
            )

            return {
                "statusCode": 201,
                "body": json.dumps({
                    "status": "ok",
                    "instrument": instrument,
                    "side": side,
                    "units": units
                })
            }

        # ─────────────────────────────────────────────────────
        # Unknown Route
        # ─────────────────────────────────────────────────────
        log.warning(f"[{request_id}] Invalid route: {path}")
        return {
            "statusCode": 404,
            "body": json.dumps({"error": f"Invalid route: {path}"})
        }

    except Exception as e:
        # Global crash safety
        log.exception(f"[{request_id}] Unhandled exception")
        send_telegram(
            f"⚠️ OANDA Bridge Crash [{request_id}]\n"
            f"{type(e).__name__}: {str(e)}"
        )
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"})
        }
