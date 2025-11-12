#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oanda_bridge.py — Simplified Lambda handler for TradingView alerts to OANDA trades (AWS Lambda-Ready)

Key features:
- Handles API Gateway /ping (GET) and /webhook (POST)
- Logs trades to CloudWatch
- OANDA REST pricing/orders
- DRY_RUN support
- Supports CLOSE alerts (MES v1.6.7)
- Handles EURUSD ↔ EUR_USD formats
- Enforces 3-pip minimum SL/TP
- SNS test events auto-acknowledged (v1.3.4)

Version: v1.3.4 — 2025-11-12
"""
import json
import os
import logging
import requests
from datetime import datetime
from typing import Optional, Dict, Any

# ─────────────────────────────────────────────────────────────
# ⬇ Environment / Constants
# ─────────────────────────────────────────────────────────────
APP_NAME = "oanda_bridge"
__version__ = "1.3.4"


def env(name: str, default: Optional[str] = None) -> str:
    return os.environ.get(name) or default or ""


OANDA_ENV = env("OANDA_ENV", "practice").lower()
API_DOMAIN = "api-fxtrade.oanda.com" if OANDA_ENV == "live" else "api-fxpractice.oanda.com"
OANDA_API_KEY = env("OANDA_LIVE_API_KEY" if OANDA_ENV == "live" else "OANDA_PRACTICE_API_KEY")
OANDA_ACCOUNT_ID = env("OANDA_LIVE_ACCOUNT_ID" if OANDA_ENV == "live" else "OANDA_PRACTICE_ACCOUNT_ID")
RISK_PERCENT = float(env("RISK_PERCENT", "1.0"))
MAX_UNITS = int(env("MAX_UNITS", "100000"))
BASE_CURRENCY = env("BASE_CURRENCY", "USD").upper()

OANDA_API_BASE = f"https://{API_DOMAIN}/v3"
OANDA_ACCOUNTS_URL = f"{OANDA_API_BASE}/accounts"
OANDA_ACCOUNT_SUMMARY = f"{OANDA_ACCOUNTS_URL}/{OANDA_ACCOUNT_ID}/summary"
OANDA_ORDERS_URL = f"{OANDA_ACCOUNTS_URL}/{OANDA_ACCOUNT_ID}/orders"
OANDA_PRICING_URL = f"{OANDA_ACCOUNTS_URL}/{OANDA_ACCOUNT_ID}/pricing"
OANDA_POSITIONS_URL = f"{OANDA_ACCOUNTS_URL}/{OANDA_ACCOUNT_ID}/positions"

# ─────────────────────────────────────────────────────────────
# ⬇ Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(APP_NAME)

# ─────────────────────────────────────────────────────────────
# ⬇ Utility Functions
# ─────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def jget(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default) if isinstance(d, dict) else default


def is_jpy_pair(instrument: str) -> bool:
    return instrument.upper().endswith("_JPY")


def get_pip_size(instrument: str) -> float:
    """Query OANDA for pip size; fallback to 0.01 (JPY) / 0.0001 (others)."""
    url = f"{OANDA_ACCOUNTS_URL}/{OANDA_ACCOUNT_ID}/instruments"
    code, data = http_get(url, params={"instruments": instrument})
    if code == 200 and data.get("instruments"):
        loc = data["instruments"][0].get("pipLocation", -4)
        return 10 ** loc
    return 0.01 if is_jpy_pair(instrument) else 0.0001


def get_mid_price(instrument: str) -> Optional[float]:
    """Return (bid+ask)/2 mid-price."""
    code, data = http_get(OANDA_PRICING_URL, params={"instruments": instrument})
    if code == 200 and data.get("prices"):
        try:
            bid = float(data["prices"][0]["bids"][0]["price"])
            ask = float(data["prices"][0]["asks"][0]["price"])
            return (bid + ask) / 2
        except Exception:
            return None
    return None


def get_quote_to_usd_rate(quote: str) -> float:
    if quote == BASE_CURRENCY:
        return 1.0
    for conv in (f"{quote}_USD", f"USD_{quote}"):
        mid = get_mid_price(conv)
        if mid and mid > 0:
            return mid if conv.endswith("USD") else 1 / mid
    log.warning(f"No rate for {quote}")
    return 1.0


def get_live_pip_value_per_unit(instrument: str) -> float:
    pip_s = get_pip_size(instrument)
    _, quote = instrument.upper().split("_")
    return pip_s * get_quote_to_usd_rate(quote)


def clamp_units(units: int) -> int:
    return max(-MAX_UNITS, min(MAX_UNITS, units))


def signed_units(side: str, raw_units: int) -> int:
    return abs(raw_units) if side == "BUY" else -abs(raw_units)


def adjust_price_for_min_distance(instrument: str, entry: float, sl: float, tp: float) -> tuple[float, float]:
    """Enforce ≥ 3 pips SL/TP distance."""
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
    if (sl != orig_sl) or (tp != orig_tp):
        log.info(f"[ADJUST] {instrument} SL {orig_sl}→{sl} TP {orig_tp}→{tp}")
    return sl, tp

# ─────────────────────────────────────────────────────────────
# ⬇ HTTP Helpers
# ─────────────────────────────────────────────────────────────
def oanda_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {OANDA_API_KEY}"}


def http_get(url: str, params: Optional[Dict[str, Any]] = None) -> tuple[int, Dict[str, Any]]:
    try:
        r = requests.get(url, headers=oanda_headers(), params=params, timeout=10)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        log.error(f"GET {url} failed: {e}")
        return 0, {"error": str(e)}


def http_post(url: str, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    try:
        r = requests.post(url, headers=oanda_headers(), json=payload, timeout=10)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        log.error(f"POST {url} failed: {e}")
        return 0, {"error": str(e)}


def http_put(url: str, payload: Optional[Dict[str, Any]] = None) -> tuple[int, Dict[str, Any]]:
    try:
        r = requests.put(url, headers=oanda_headers(), json=payload, timeout=10)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        log.error(f"PUT {url} failed: {e}")
        return 0, {"error": str(e)}

# ─────────────────────────────────────────────────────────────
# ⬇ Balance & Sizing
# ─────────────────────────────────────────────────────────────
def get_account_balance() -> tuple[Optional[float], Dict[str, Any]]:
    code, data = http_get(OANDA_ACCOUNT_SUMMARY)
    if code == 200:
        return float(data.get("account", {}).get("balance", 0.0)), data
    return None, data


def calculate_units(instrument: str, side: str, entry: float, sl: float, balance: float, risk_pct: float) -> int:
    risk_usd = balance * (risk_pct / 100.0)
    pip_s = get_pip_size(instrument)
    pips_risk = abs(entry - sl) / pip_s
    pvpu = get_live_pip_value_per_unit(instrument)
    raw = int(risk_usd / (pips_risk * pvpu)) if pips_risk and pvpu else 0
    return clamp_units(signed_units(side, raw))

# ─────────────────────────────────────────────────────────────
# ⬇ Order Creation
# ─────────────────────────────────────────────────────────────
def build_market_order_payload(instrument: str, units: int, sl: float, tp: float, client_tag: str) -> Dict[str, Any]:
    return {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": client_tag, "tag": "oanda_bridge", "comment": "autotrade"},
            "takeProfitOnFill": {"price": f"{tp:.5f}"},
            "stopLossOnFill": {"price": f"{sl:.5f}"}
        }
    }


def submit_order(payload: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    code, data = http_post(OANDA_ORDERS_URL, payload)
    ok = code == 201 and ("orderFillTransaction" in data or "orderCreateTransaction" in data)
    return ok, data

# ─────────────────────────────────────────────────────────────
# ⬇ Position Closure
# ─────────────────────────────────────────────────────────────
def close_position(instrument: str) -> tuple[bool, Dict[str, Any]]:
    url = f"{OANDA_POSITIONS_URL}/{instrument}/close"
    code, data = http_put(url)
    return code == 200, data

# ─────────────────────────────────────────────────────────────
# ⬇ Trade Logging
# ─────────────────────────────────────────────────────────────
def log_trade(alert_id, instrument, side, entry, sl, tp, units, risk_pct, status,
              oanda_order_id=None, oanda_trade_id=None):
    now_s = now_iso()
    trade = {
        "alert_id": alert_id,
        "instrument": instrument,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "units": units,
        "risk_pct": risk_pct,
        "status": status,
        "oanda_order_id": oanda_order_id,
        "oanda_trade_id": oanda_trade_id,
        "created_at": now_s,
        "updated_at": now_s
    }
    log.info(json.dumps(trade))

# ─────────────────────────────────────────────────────────────
# ⬇ Lambda Handler
# ─────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    """Main Lambda entry — handles /ping, /webhook, and SNS tests."""
    try:
        # SNS or direct test check
        if isinstance(event, dict) and event.get("test_sns"):
            log.info("✅ SNS test event acknowledged")
            return {"statusCode": 200, "body": json.dumps({"status": "sns-test-ok"})}

        http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "")
        path = event.get("path") or event.get("requestContext", {}).get("http", {}).get("path", "")

        # ─── /ping ─────────────────────────────────────────────
        if http_method == "GET" and path.endswith("/ping"):
            balance, _ = get_account_balance()
            env_info = {
                "mode": OANDA_ENV,
                "account": OANDA_ACCOUNT_ID,
                "risk_percent": RISK_PERCENT,
                "max_units": MAX_UNITS,
                "base_currency": BASE_CURRENCY
            }
            return {"statusCode": 200, "body": json.dumps({"status": "ok", "balance": balance, "env": env_info})}

        # ─── /webhook ──────────────────────────────────────────
        if http_method == "POST" and path.endswith("/webhook"):
            try:
                body = event.get("body", "{}")
                payload = json.loads(body) if isinstance(body, str) else body
            except json.JSONDecodeError:
                return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON"})}

            side = str(jget(payload, "message", "")).upper().strip()
            instrument = str(jget(payload, "instrument", "")).upper().strip()
            if "_" not in instrument and len(instrument) >= 6:
                instrument = f"{instrument[:3]}_{instrument[3:]}"
            instrument = instrument.replace("/", "_")

            entry = float(jget(payload, "entry", 0.0))
            sl = float(jget(payload, "sl", 0.0))
            tp = float(jget(payload, "tp", 0.0))
            alert_id = str(jget(payload, "alert_id", "")) or f"alert-{int(datetime.utcnow().timestamp())}"
            risk_pct = float(jget(payload, "risk_pct", RISK_PERCENT))

            # CLOSE
            if side == "CLOSE":
                if not instrument:
                    return {"statusCode": 400, "body": json.dumps({"error": "Instrument required for CLOSE"})}
                ok, resp = close_position(instrument)
                if not ok:
                    log.error(f"Close failed: {resp}")
                    return {"statusCode": 400, "body": json.dumps({"status": "error", "oanda": resp})}
                log.info(f"[CLOSE] {instrument}")
                return {"statusCode": 200, "body": json.dumps({"status": "ok", "instrument": instrument, "action": "closed"})}

            # Validate
            if side not in ("BUY", "SELL") or not instrument or entry <= 0 or sl <= 0 or tp <= 0:
                return {"statusCode": 400, "body": json.dumps({"error": "Invalid payload"})}

            balance, _ = get_account_balance()
            if balance is None:
                return {"statusCode": 502, "body": json.dumps({"error": "OANDA auth failed"})}

            sl, tp = adjust_price_for_min_distance(instrument, entry, sl, tp)
            units = calculate_units(instrument, side, entry, sl, balance, risk_pct)
            if units == 0:
                return {"statusCode": 400, "body": json.dumps({"error": "Units=0"})}

            client_tag = f"{APP_NAME}-{alert_id}"
            order_body = build_market_order_payload(instrument, units, sl, tp, client_tag)

            # DRY RUN
            if env("DRY_RUN", "false").lower() == "true":
                log_trade(alert_id, instrument, side, entry, sl, tp, units, risk_pct, "DRY-RUN")
                return {"statusCode": 200, "body": json.dumps({"status": "dry-run", "instrument": instrument, "side": side, "units": units})}

            ok, oanda_resp = submit_order(order_body)
            if not ok:
                log_trade(alert_id, instrument, side, entry, sl, tp, units, risk_pct, "REJECTED")
                return {"statusCode": 400, "body": json.dumps({"status": "error", "oanda": oanda_resp})}

            order_id = (
                oanda_resp.get("orderFillTransaction", {}).get("orderID")
                or oanda_resp.get("orderCreateTransaction", {}).get("id")
            )
            trade_id = oanda_resp.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
            log_trade(alert_id, instrument, side, entry, sl, tp, units, risk_pct, "SUBMITTED", order_id, trade_id)

            return {"statusCode": 201, "body": json.dumps({"status": "ok", "instrument": instrument, "side": side, "units": units})}

        # ─── Unknown Route ─────────────────────────────────────
        return {"statusCode": 404, "body": json.dumps({"error": f"Invalid route: {path}"})}

    except Exception as e:
        log.error(f"Handler error: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
