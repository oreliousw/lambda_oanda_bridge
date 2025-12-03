#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oanda_bridge.py — MES-Only OANDA Trade Executor (AWS Lambda-Ready)

Version: v2.3 — 2025-12-03
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

# Silence SSL warning noise (optional but helpful in Lambda logs)
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
__version__ = "2.3"

OANDA_ENV = env("OANDA_ENV", "practice").lower()

API_DOMAIN = (
    "api-fxtrade.oanda.com"
    if OANDA_ENV == "live"
    else "api-fxpractice.oanda.com"
)

OANDA_API_KEY = env(
    "OANDA_LIVE_API_KEY" if OANDA_ENV == "live" else "OANDA_PRACTICE_API_KEY"
)
OANDA_ACCOUNT_ID = env(
    "OANDA_LIVE_ACCOUNT_ID" if OANDA_ENV == "live" else "OANDA_PRACTICE_ACCOUNT_ID"
)

RISK_PERCENT = float(env("RISK_PERCENT", "1.0"))
MAX_UNITS = int(env("MAX_UNITS", "12000"))
BASE_CURRENCY = env("BASE_CURRENCY", "USD").upper()

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")

OANDA_API_BASE = f"https://{API_DOMAIN}/v3"
ACCOUNTS_URL = f"{OANDA_API_BASE}/accounts/{OANDA_ACCOUNT_ID}"
ORDERS_URL = f"{ACCOUNTS_URL}/orders"
PRICING_URL = f"{ACCOUNTS_URL}/pricing"
POSITIONS_URL = f"{ACCOUNTS_URL}/positions"
SUMMARY_URL = f"{ACCOUNTS_URL}/summary"

# ─────────────────────────────────────────────────────────────
# ⬇ Utilities
# ─────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def jget(d: Dict[str, Any], key: str, default: Any = None):
    return d.get(key, default) if isinstance(d, dict) else default


def is_jpy_pair(instrument: str) -> bool:
    return instrument.endswith("_JPY")


def oanda_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────
# ⬇ Telegram Notifications
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except Exception as e:
        log.error(f"Telegram failed: {e}")


# ─────────────────────────────────────────────────────────────
# ⬇ HTTP Helpers
# ─────────────────────────────────────────────────────────────
def http_get(url: str, params=None):
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


def http_put(url: str, payload=None):
    try:
        r = requests.put(url, headers=oanda_headers(), json=payload, timeout=10)
        return r.status_code, (r.json() if r.content else {})
    except Exception as e:
        log.error(f"PUT {url} failed: {e}")
        return 0, {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# ⬇ OANDA Helpers
# ─────────────────────────────────────────────────────────────
def get_pip_size(instrument: str) -> float:
    code, data = http_get(
        f"{ACCOUNTS_URL}/instruments",
        params={"instruments": instrument}
    )
    if code == 200 and data.get("instruments"):
        loc = data["instruments"][0].get("pipLocation", -4)
        return 10 ** loc
    return 0.01 if is_jpy_pair(instrument) else 0.0001


def get_mid_price(instrument: str) -> Optional[float]:
    code, data = http_get(PRICING_URL, params={"instruments": instrument})
    if code == 200 and data.get("prices"):
        try:
            bid = float(data["prices"][0]["bids"][0]["price"])
            ask = float(data["prices"][0]["asks"][0]["price"])
            return (bid + ask) / 2
        except Exception:
            return None
    return None


def get_account_summary():
    return http_get(SUMMARY_URL)


def get_positions():
    code, data = http_get(POSITIONS_URL)
    return data.get("positions", []) if code == 200 else []


def clamp_units(u: int) -> int:
    return max(-MAX_UNITS, min(MAX_UNITS, u))


def signed_units(side: str, units: int) -> int:
    return abs(units) if side == "BUY" else -abs(units)


def calculate_units(instrument: str, side: str, entry: float, sl: float, balance: float):
    pip = get_pip_size(instrument)
    pips_risk = abs(entry - sl) / pip
    if pips_risk <= 0:
        return 0
    risk_usd = balance * (RISK_PERCENT / 100.0)
    pvpu = pip if BASE_CURRENCY == "USD" else pip  # simplified: enough for USD base
    raw = int(risk_usd / (pips_risk * pvpu))
    return clamp_units(signed_units(side, raw))


def adjust_sl_tp(instrument: str, entry: float, sl: float, tp: float):
    min_pips = 3
    pip = get_pip_size(instrument)
    mindist = min_pips * pip

    orig_sl, orig_tp = sl, tp

    if tp > entry and sl < entry:  # BUY
        sl = entry - mindist if (entry - sl) < mindist else sl
        tp = entry + mindist if (tp - entry) < mindist else tp
    elif tp < entry and sl > entry:  # SELL
        sl = entry + mindist if (sl - entry) < mindist else sl
        tp = entry - mindist if (entry - tp) < mindist else tp

    if sl != orig_sl or tp != orig_tp:
        log.info(f"[ADJUST] {instrument} SL {orig_sl}→{sl} | TP {orig_tp}→{tp}")

    return sl, tp


# ─────────────────────────────────────────────────────────────
# ⬇ Order Creation
# ─────────────────────────────────────────────────────────────
def market_order(instrument: str, units: int, sl: float, tp: float, tag: str):
    return {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": tag, "tag": "oanda_bridge"},
            "takeProfitOnFill": {"price": f"{tp:.5f}"},
            "stopLossOnFill": {"price": f"{sl:.5f}"}
        }
    }


def submit_order(payload: Dict[str, Any]):
    return http_post(ORDERS_URL, payload)


# ─────────────────────────────────────────────────────────────
# ⬇ Main Lambda Handler (v2.3)
# ─────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    try:
        http_method = event.get("httpMethod", "")
        path = event.get("path", "")

        # ── /ping ──────────────────────────────────────────────
        if http_method == "GET" and path.endswith("/ping"):
            code, data = get_account_summary()
            bal = jget(jget(data, "account", {}), "balance", None)
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

        # ── /status ────────────────────────────────────────────
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

        # ── /webhook ───────────────────────────────────────────
        if http_method == "POST" and path.endswith("/webhook"):
            raw = event.get("body", "{}")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON"})}

            side = str(jget(payload, "message")).upper()
            instrument = str(jget(payload, "instrument")).upper().replace("/", "_")
            entry = float(jget(payload, "entry", 0.0))
            sl = float(jget(payload, "sl", 0.0))
            tp = float(jget(payload, "tp", 0.0))

            if "_" not in instrument and len(instrument) >= 6:
                instrument = f"{instrument[:3]}_{instrument[3:]}"

            if side not in ("BUY", "SELL") or entry <= 0 or sl <= 0 or tp <= 0:
                return {"statusCode": 400, "body": json.dumps({"error": "Invalid MES payload"})}

            # Account balance
            code, data = get_account_summary()
            bal = float(jget(jget(data, "account", {}), "balance", 0.0))

            # SL/TP enforcement
            sl, tp = adjust_sl_tp(instrument, entry, sl, tp)

            # Unit sizing
            units = calculate_units(instrument, side, entry, sl, bal)
            if units == 0:
                return {"statusCode": 400, "body": json.dumps({"error": "Units=0"})}

            tag = f"{APP_NAME}-{int(datetime.utcnow().timestamp())}"
            order_body = market_order(instrument, units, sl, tp, tag)

            ok, resp = submit_order(order_body)

            if not ok:
                send_telegram(f"❌ OANDA Rejected\n{instrument} {side}\nUnits={units}\n{resp}")
                return {"statusCode": 400, "body": json.dumps({"error": "OANDA rejected", "oanda": resp})}

            send_telegram(f"✔️ Trade Filled\n{instrument} {side} {units}\nSL={sl}\nTP={tp}")

            return {
                "statusCode": 201,
                "body": json.dumps({
                    "status": "ok",
                    "instrument": instrument,
                    "side": side,
                    "units": units
                })
            }

        # ── Unknown route ──────────────────────────────────────
        return {"statusCode": 404, "body": json.dumps({"error": f"Invalid route: {path}"})}

    except json.JSONDecodeError as je:
        log.error(f"Bad JSON: {je}")
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON"})}

    except Exception as e:
        log.exception("Unhandled exception")
        send_telegram(f"⚠️ OANDA Bridge Crash\n{type(e).__name__}: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal server error"})}
