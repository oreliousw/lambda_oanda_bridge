#!/usr/bin/env python3
"""
v33_scan_cli.py
CLI strategy engine for V3.3-style momentum pullback/breakout
on 4H + 1H using OANDA data only.

- Scans: EUR/USD, GBP/USD, USD/CAD, USD/CHF, AUD/USD, NZD/USD
- Looks for 30–40 pip opportunities over next ~4–24h
- Trend: 4H + 1H EMA
- Momentum: 1H RSI + pullback/breakout logic
- Can:
  • just print signals
  • send trades to OANDA Bridge (Lambda)
  • send summary to Telegram
"""

import os
import sys
import json
import argparse
from dataclasses import dataclass
from datetime import datetime
import requests
import pandas as pd

# ─────────────────────────────────────────────────────────────
#  CONFIG / ENV
# ─────────────────────────────────────────────────────────────

OANDA_API_KEY     = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV         = os.getenv("OANDA_ENV", "practice")  # "practice" or "live"
OANDA_BRIDGE_URL  = os.getenv("OANDA_BRIDGE_URL", "")
ACCOUNT_SIZE_USD  = float(os.getenv("ACCOUNT_BASE_USD", "200"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

if OANDA_ENV == "live":
    OANDA_REST_URL = "https://api-fxtrade.oanda.com"
else:
    OANDA_REST_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

INSTRUMENTS = [
    "EUR_USD",
    "GBP_USD",
    "USD_CAD",
    "USD_CHF",
    "AUD_USD",
    "NZD_USD",
]

# ─────────────────────────────────────────────────────────────
#  HELPERS: OANDA candles + indicators (RMA, RSI, ATR, EMA)
# ─────────────────────────────────────────────────────────────

def get_candles(instrument: str, granularity: str = "H1", count: int = 200) -> pd.DataFrame:
    url = f"{OANDA_REST_URL}/v3/instruments/{instrument}/candles"
    params = {
        "granularity": granularity,
        "count": count,
        "price": "M",  # mid
    }
    resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()["candles"]

    rows = []
    for c in data:
        if not c["complete"]:
            continue
        rows.append({
            "time": datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
            "open": float(c["mid"]["o"]),
            "high": float(c["mid"]["h"]),
            "low": float(c["mid"]["l"]),
            "close": float(c["mid"]["c"]),
            "volume": int(c["volume"]),
        })
    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df

def rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    r = series.copy()
    if len(series) == 0:
        return series
    r.iloc[0] = series.iloc[:length].mean() if len(series) >= length else series.mean()
    for i in range(1, len(series)):
        r.iloc[i] = alpha * series.iloc[i] + (1 - alpha) * r.iloc[i - 1]
    return r

def calc_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / (avg_loss.replace(0, 1e-10))
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return rma(tr, length)

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def pips_diff(instrument: str, price_diff: float) -> float:
    if "JPY" in instrument:
        return price_diff / 0.01
    else:
        return price_diff / 0.0001

def price_from_pips(instrument: str, price: float, pips: float, direction: str) -> float:
    if "JPY" in instrument:
        step = 0.01
    else:
        step = 0.0001
    diff = pips * step
    return price + diff if direction == "up" else price - diff

# ─────────────────────────────────────────────────────────────
#  STRATEGY: 4H/1H trend + 1H momentum, 30–40 pip goal
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    instrument: str
    side: str           # "BUY", "SELL", "NONE"
    entry_price: float
    sl_price: float
    tp_price: float
    atr_pips: float
    tp_pips: float
    sl_pips: float
    quality: str        # "HIGH", "MEDIUM", "LOW", "SKIP"
    reason: str

def build_signal(
    instrument: str,
    min_atr_pips: float = 8.0,
    tp_min: float = 30.0,
    tp_max: float = 40.0,
    rr: float = 2.0,
    ema_fast: int = 50,
    ema_slow: int = 200,
) -> Signal:
    # 4H + 1H
    h4 = get_candles(instrument, "H4", 200)
    h1 = get_candles(instrument, "H1", 200)

    h4["ema_slow"] = ema(h4["close"], ema_slow)
    h1["ema_slow"] = ema(h1["close"], ema_slow)
    h1["ema_fast"] = ema(h1["close"], ema_fast)
    h1["rsi"]      = calc_rsi(h1["close"], 14)
    h1["atr"]      = calc_atr(h1, 14)

    h4_last = h4.iloc[-1]
    h1_last = h1.iloc[-1]

    atr_pips = pips_diff(instrument, h1_last["atr"])
    if atr_pips < min_atr_pips:
        return Signal(
            instrument, "NONE", h1_last["close"], h1_last["close"],
            h1_last["close"], atr_pips, 0.0, 0.0, "SKIP",
            f"ATR too small ({atr_pips:.1f} pips)"
        )

    # trend
    trend_up   = (h4_last["close"] > h4_last["ema_slow"]) and (h1_last["close"] > h1_last["ema_slow"])
    trend_down = (h4_last["close"] < h4_last["ema_slow"]) and (h1_last["close"] < h1_last["ema_slow"])

    # momentum
    last_rsi = h1_last["rsi"]
    prev_rsi = h1["rsi"].iloc[-5]  # a few candles back

    side = "NONE"
    reason = ""
    quality = "LOW"

    # Pullback or breakout logic, simplified:
    if trend_up:
        # Pullback long: RSI cooled to 45–55 after >60 before
        if prev_rsi > 60 and 45 <= last_rsi <= 55 and h1_last["close"] > h1_last["ema_fast"]:
            side = "BUY"
            reason = "Uptrend + RSI pullback 45–55 after >60 (pullback long)"
            quality = "HIGH"
        # Breakout long: RSI > 60 & near recent high
        elif last_rsi > 60 and h1_last["close"] >= h1["close"].rolling(20).max().iloc[-1] * 0.999:
            side = "BUY"
            reason = "Uptrend + RSI>60 + near 20-bar high (breakout long)"
            quality = "MEDIUM"

    elif trend_down:
        if prev_rsi < 40 and 45 <= last_rsi <= 55 and h1_last["close"] < h1_last["ema_fast"]:
            side = "SELL"
            reason = "Downtrend + RSI pullback 45–55 after <40 (pullback short)"
            quality = "HIGH"
        elif last_rsi < 40 and h1_last["close"] <= h1["close"].rolling(20).min().iloc[-1] * 1.001:
            side = "SELL"
            reason = "Downtrend + RSI<40 + near 20-bar low (breakout short)"
            quality = "MEDIUM"

    entry = float(h1_last["close"])

    if side == "BUY":
        # aim for 30–40 pips TP, SL ≈ TP/rr
        tp_pips = max(tp_min, min(tp_max, atr_pips * 1.2))
        sl_pips = tp_pips / rr
        tp = price_from_pips(instrument, entry, tp_pips, "up")
        sl = price_from_pips(instrument, entry, sl_pips, "down")
    elif side == "SELL":
        tp_pips = max(tp_min, min(tp_max, atr_pips * 1.2))
        sl_pips = tp_pips / rr
        tp = price_from_pips(instrument, entry, tp_pips, "down")
        sl = price_from_pips(instrument, entry, sl_pips, "up")
    else:
        tp_pips = 0.0
        sl_pips = 0.0
        tp = entry
        sl = entry
        if reason == "":
            reason = "No clear trend/momentum alignment"

    return Signal(
        instrument=instrument,
        side=side,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        atr_pips=atr_pips,
        tp_pips=tp_pips,
        sl_pips=sl_pips,
        quality=quality if side != "NONE" else "SKIP",
        reason=reason
    )

# ─────────────────────────────────────────────────────────────
#  OANDA BRIDGE PAYLOAD (replace keys to match your V3.3)
# ─────────────────────────────────────────────────────────────

def build_alert_payload(sig: Signal, risk_fraction: float) -> dict:
    """
    IMPORTANT:
    Replace this with your REAL Pine V3.3 alert JSON format.
    Keep all keys/structure identical to avoid breaking the bridge.
    """
    return {
        "source": "CLI_V33",
        "strategy": "V3.3_OANDA_ONLY",
        "instrument": sig.instrument,
        "side": sig.side,
        "order_type": "market",
        "risk_percent": risk_fraction * 100.0,  # if your bridge expects 20.0 = 20%
        "entry_price": sig.entry_price,
        "stop_loss_price": sig.sl_price,
        "take_profit_price": sig.tp_price,
        "comment": f"{sig.quality} | {sig.reason}",
    }

def send_to_bridge(payload: dict) -> str:
    if not OANDA_BRIDGE_URL:
        return "OANDA_BRIDGE_URL not set – skipping send."
    try:
        r = requests.post(OANDA_BRIDGE_URL, json=payload, timeout=10)
        return f"Bridge {r.status_code}: {r.text}"
    except Exception as e:
        return f"Error sending to bridge: {e}"

# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, data=data, timeout=10)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
#  CLI COMMANDS
# ─────────────────────────────────────────────────────────────

def cmd_scan(args):
    rows = []
    high_signals = []

    for inst in INSTRUMENTS:
        sig = build_signal(inst)
        rows.append(sig)

    print("─────────────── SCAN RESULTS ───────────────")
    for sig in rows:
        print(f"{sig.instrument:8} {sig.side:4} {sig.quality:6} "
              f"ATR:{sig.atr_pips:5.1f} TP:{sig.tp_pips:5.1f} SL:{sig.sl_pips:5.1f}  "
              f"{sig.reason}")
        if sig.side != "NONE" and sig.quality in ("HIGH", "MEDIUM"):
            high_signals.append(sig)

    if args.telegram:
        msg_lines = [f"*V3.3 Scan {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*"]
        for sig in high_signals:
            msg_lines.append(
                f"`{sig.instrument}` {sig.side} {sig.quality} "
                f"TP ~{sig.tp_pips:.1f} pips, SL ~{sig.sl_pips:.1f} pips\n"
                f"{sig.reason}"
            )
        if not high_signals:
            msg_lines.append("_No good setups right now._")
        send_telegram("\n".join(msg_lines))

def cmd_auto(args):
    """
    Auto-trade mode:
    - Scan all instruments
    - For HIGH-quality signals only, send trades to OANDA Bridge
    - Optionally Telegram summary
    """
    risk_fraction = args.risk  # 0.2 = 20%
    traded = []
    skipped = []

    for inst in INSTRUMENTS:
        sig = build_signal(inst)
        if sig.side != "NONE" and sig.quality == "HIGH":
            payload = build_alert_payload(sig, risk_fraction)
            result = send_to_bridge(payload)
            traded.append((sig, result))
            print(f"[TRADE] {inst} {sig.side} → {result}")
        else:
            skipped.append(sig)
            print(f"[SKIP ] {inst} {sig.side} {sig.quality} – {sig.reason}")

    if args.telegram:
        lines = [f"*V3.3 Auto {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*"]
        if traded:
            lines.append("*Trades sent:*")
            for sig, res in traded:
                lines.append(
                    f"`{sig.instrument}` {sig.side} {sig.quality} "
                    f"TP ~{sig.tp_pips:.1f} pips, SL ~{sig.sl_pips:.1f} pips\n"
                    f"{sig.reason}"
                )
        else:
            lines.append("_No HIGH-quality trades sent._")

        send_telegram("\n".join(lines))

def main():
    parser = argparse.ArgumentParser(
        description="V3.3 OANDA-only CLI engine (4H/1H momentum pullback/breakout scanner)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan pairs and print signals")
    p_scan.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    p_scan.set_defaults(func=cmd_scan)

    p_auto = sub.add_parser("auto", help="Scan and auto-trade HIGH-quality signals via OANDA Bridge")
    p_auto.add_argument("--risk", type=float, default=0.20, help="Risk fraction (0.20 = 20%% of account base)")
    p_auto.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    p_auto.set_defaults(func=cmd_auto)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    if not OANDA_API_KEY:
        print("OANDA_API_KEY not set – please export it.")
        sys.exit(1)
    main()
