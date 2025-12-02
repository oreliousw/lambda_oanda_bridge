#!/usr/bin/env python3
"""
v33_auto_cli.py
Hybrid 4H + 1H + 15M strategy engine using OANDA data only.

- Instruments: EUR/USD, GBP/USD, USD/CAD, USD/CHF, AUD/USD, NZD/USD
- Trend bias: 4H + 1H EMA
- Momentum: 1H RSI
- Timing: 15M RSI + 15M EMA50 + local structure
- Target: 30–40 pip move (4–24h)
- Trade lifecycle: SL/TP handled by OANDA via Lambda OANDA Bridge
- Risk per trade: fixed 25% (bridge does actual sizing)
- Max open trades: 3 (checked via OANDA openPositions)
- Trading window: Sunday 14:00 → Friday 14:00 (server local time)
- Usage:
    python v33_auto_cli.py scan --telegram
    python v33_auto_cli.py auto --telegram
"""

import os
import sys
import json
import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple

import requests
import pandas as pd

# ─────────────────────────────────────────────────────────────
#  CONFIG / ENV
# ─────────────────────────────────────────────────────────────

OANDA_API_KEY     = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV         = os.getenv("OANDA_ENV", "practice")  # "practice" or "live"
OANDA_BRIDGE_URL  = os.getenv("OANDA_BRIDGE_URL", "")   # Lambda webhook URL

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

if OANDA_ENV == "live":
    OANDA_REST_URL = "https://api-fxtrade.oanda.com"
else:
    OANDA_REST_URL = "https://api-fxpractice.oanda.com"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

INSTRUMENTS = [
    "EUR_USD",
    "GBP_USD",
    "USD_CAD",
    "USD_CHF",
    "AUD_USD",
    "NZD_USD",
]

# Risk is fixed at 25% (bridge does actual money/units sizing)
RISK_PERCENT = 25.0  # <- core knob you mentioned


# ─────────────────────────────────────────────────────────────
#  TIME / TRADING WINDOW
# ─────────────────────────────────────────────────────────────

def trading_window_open() -> bool:
    """
    Allow trading only:
    - Sunday 14:00 → Friday 14:00 (server local time)
    Python weekday: Monday=0 ... Sunday=6
    """
    now = datetime.now()
    wd = now.weekday()   # 0=Mon, 4=Fri, 6=Sun
    hour = now.hour

    # Sunday: only after 14:00
    if wd == 6:  # Sunday
        return hour >= 14

    # Monday–Thursday: always open
    if 0 <= wd <= 3:
        return True

    # Friday: only before 14:00
    if wd == 4:  # Friday
        return hour < 14

    # Saturday: closed
    return False


# ─────────────────────────────────────────────────────────────
#  OANDA: candles, open trades
# ─────────────────────────────────────────────────────────────

def get_candles(instrument: str, granularity: str, count: int = 250) -> pd.DataFrame:
    """
    Pull mid-price candles from OANDA.
    """
    url = f"{OANDA_REST_URL}/v3/instruments/{instrument}/candles"
    params = {
        "granularity": granularity,
        "count": count,
        "price": "M",
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
            "low":  float(c["mid"]["l"]),
            "close":float(c["mid"]["c"]),
            "volume": int(c["volume"]),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"No candles returned for {instrument} {granularity}")
    df.set_index("time", inplace=True)
    return df


def get_open_trade_count() -> int:
    """
    Count how many open positions exist in the OANDA account.
    Used to enforce max 3 open trades.
    """
    url = f"{OANDA_REST_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openPositions"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    positions = resp.json().get("positions", [])
    return len(positions)


# ─────────────────────────────────────────────────────────────
#  INDICATORS: RMA, RSI, ATR, EMA, pips
# ─────────────────────────────────────────────────────────────

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
upper = 0.01
    else:
        upper = 0.0001
    return price_diff / upper


def price_from_pips(instrument: str, price: float, pips: float, direction: str) -> float:
    if "JPY" in instrument:
        step = 0.01
    else:
        step = 0.0001
    diff = pips * step
    return price + diff if direction == "up" else price - diff


# ─────────────────────────────────────────────────────────────
#  STRATEGY LOGIC: 4H + 1H + 15M hybrid
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
    ema_slow: int = 200,
    ema_fast_1h: int = 50,
    ema_fast_15m: int = 50,
) -> Signal:
    """
    Hybrid logic:
    - 4H + 1H EMA for trend
    - 1H RSI for broader momentum
    - 15M RSI + EMA50 + local high/low for timing
    """

    # Fetch candles
    h4 = get_candles(instrument, "H4", 250)
    h1 = get_candles(instrument, "H1", 250)
    m15 = get_candles(instrument, "M15", 250)

    # Indicators
    h4["ema_slow"]   = ema(h4["close"], ema_slow)
    h1["ema_slow"]   = ema(h1["close"], ema_slow)
    h1["ema_fast"]   = ema(h1["close"], ema_fast_1h)
    h1["rsi"]        = calc_rsi(h1["close"], 14)
    h1["atr"]        = calc_atr(h1, 14)

    m15["ema_fast"]  = ema(m15["close"], ema_fast_15m)
    m15["rsi"]       = calc_rsi(m15["close"], 14)
    m15["high20"]    = m15["close"].rolling(20).max()
    m15["low20"]     = m15["close"].rolling(20).min()

    h4_last = h4.iloc[-1]
    h1_last = h1.iloc[-1]
    m15_last = m15.iloc[-1]

    # ATR in pips (from 1H)
    atr_pips = pips_diff(instrument, h1_last["atr"])
    if atr_pips < min_atr_pips:
        return Signal(
            instrument, "NONE", h1_last["close"], h1_last["close"],
            h1_last["close"], atr_pips, 0.0, 0.0, "SKIP",
            f"ATR too small ({atr_pips:.1f} pips)"
        )

    # Trend bias (4H + 1H)
    trend_up   = (h4_last["close"] > h4_last["ema_slow"]) and (h1_last["close"] > h1_last["ema_slow"])
    trend_down = (h4_last["close"] < h4_last["ema_slow"]) and (h1_last["close"] < h1_last["ema_slow"])

    # 1H momentum
    h1_last_rsi  = h1_last["rsi"]
    h1_prev_rsi  = h1["rsi"].iloc[-5]

    # 15M timing
    m15_last_rsi = m15_last["rsi"]
    m15_prev_rsi = m15["rsi"].iloc[-5]
    m15_high20   = m15_last["high20"]
    m15_low20    = m15_last["low20"]
    m15_ema_fast = m15_last["ema_fast"]

    entry = float(h1_last["close"])

    # Distance of M15 price from its EMA50 (used for pullback “zone”)
    ema_zone_distance = abs(m15_last["close"] - m15_ema_fast)
    ema_zone_ok = ema_zone_distance <= h1_last["atr"] * 0.5  # within half ATR is a nice pullback zone

    side = "NONE"
    reason = ""
    quality = "LOW"

    # ── LONG LOGIC ────────────────────────────────────────────
    # Pullback long
    if trend_up:
        if (h1_prev_rsi > 60
            and 40 <= m15_last_rsi <= 60
            and ema_zone_ok
           ):
            side = "BUY"
            reason = "Uptrend 4H/1H + 1H RSI>60 prior + 15M RSI 40–60 near EMA50 (pullback long)"
            quality = "HIGH"

        # Breakout long
        elif (h1_last_rsi > 60
              and m15_last_rsi > 60
              and m15_high20 > 0
              and m15_last["close"] >= m15_high20 * 0.999
              and m15_last["close"] > m15_ema_fast
             ):
            side = "BUY"
            reason = "Uptrend 4H/1H + 1H/15M RSI>60 + 15M near 20-bar high (breakout long)"
            quality = "HIGH"

    # ── SHORT LOGIC ───────────────────────────────────────────
    if trend_down and side == "NONE":
        # Pullback short
        if (h1_prev_rsi < 40
            and 40 <= m15_last_rsi <= 60
            and ema_zone_ok
           ):
            side = "SELL"
            reason = "Downtrend 4H/1H + 1H RSI<40 prior + 15M RSI 40–60 near EMA50 (pullback short)"
            quality = "HIGH"

        # Breakout short
        elif (h1_last_rsi < 40
              and m15_last_rsi < 40
              and m15_low20 > 0
              and m15_last["close"] <= m15_low20 * 1.001
              and m15_last["close"] < m15_ema_fast
             ):
            side = "SELL"
            reason = "Downtrend 4H/1H + 1H/15M RSI<40 + 15M near 20-bar low (breakout short)"
            quality = "HIGH"

    # If we still don't have a side, no trade
    if side == "NONE":
        return Signal(
            instrument, "NONE", entry, entry, entry,
            atr_pips, 0.0, 0.0, "SKIP",
            "No clear 4H/1H trend + 15M timing alignment"
        )

    # ── SL/TP based on ATR + 30–40 pip band ──────────────────
    tp_pips_raw = atr_pips * 1.2
    tp_pips = max(tp_min, min(tp_max, tp_pips_raw))
    sl_pips = tp_pips / rr

    if side == "BUY":
        tp = price_from_pips(instrument, entry, tp_pips, "up")
        sl = price_from_pips(instrument, entry, sl_pips, "down")
    else:  # SELL
        tp = price_from_pips(instrument, entry, tp_pips, "down")
        sl = price_from_pips(instrument, entry, sl_pips, "up")

    return Signal(
        instrument=instrument,
        side=side,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        atr_pips=atr_pips,
        tp_pips=tp_pips,
        sl_pips=sl_pips,
        quality=quality,
        reason=reason
    )


# ─────────────────────────────────────────────────────────────
#  OANDA BRIDGE PAYLOAD (match to your lambda format)
# ─────────────────────────────────────────────────────────────

def build_alert_payload(sig: Signal) -> dict:
    """
    IMPORTANT:
    Replace keys/structure here to exactly match your Pine V3.3 JSON alerts.
    Keep alert JSON compatible with your existing Lambda OANDA Bridge.
    """
    payload = {
        "source": "CLI_V33",
        "strategy": "V3.3_HYBRID_4H_1H_15M",
        "instrument": sig.instrument,
        "side": sig.side,
        "order_type": "market",
        "risk_percent": RISK_PERCENT,  # Bridge expects 25.0 = 25%
        "entry_price": sig.entry_price,
        "stop_loss_price": sig.sl_price,
        "take_profit_price": sig.tp_price,
        "comment": f"{sig.quality} | {sig.reason}",
    }
    return payload


def send_to_bridge(payload: dict) -> str:
    if not OANDA_BRIDGE_URL:
        return "OANDA_BRIDGE_URL not set – skipping bridge send."
    try:
        resp = requests.post(OANDA_BRIDGE_URL, json=payload, timeout=15)
        return f"Bridge {resp.status_code}: {resp.text}"
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
    if not trading_window_open():
        msg = "Trading window CLOSED – scan only (no auto trades)."
        print(msg)
        if args.telegram:
            send_telegram(f"*V3.3 Scan*\n{msg}")
        # still allow scan
    signals: List[Signal] = []

    for inst in INSTRUMENTS:
        try:
            sig = build_signal(inst)
        except Exception as e:
            print(f"[ERROR] {inst}: {e}")
            continue
        signals.append(sig)

    print("─────────────── SCAN RESULTS ───────────────")
    for sig in signals:
        print(f"{sig.instrument:8} {sig.side:4} {sig.quality:6} "
              f"ATR:{sig.atr_pips:5.1f} TP:{sig.tp_pips:5.1f} SL:{sig.sl_pips:5.1f}  "
              f"{sig.reason}")

    if args.telegram:
        high = [s for s in signals if s.side != "NONE" and s.quality == "HIGH"]
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"*V3.3 Scan {ts}*"]
        for s in high:
            lines.append(
                f"`{s.instrument}` {s.side} {s.quality} "
                f"TP ~{s.tp_pips:.1f}p, SL ~{s.sl_pips:.1f}p\n{s.reason}"
            )
        if not high:
            lines.append("_No HIGH-quality setups right now._")
        send_telegram("\n".join(lines))


def cmd_auto(args):
    """
    Auto-trade mode (for systemd timer):
    - Respect trading window
    - Respect max 3 open trades
    - Only send HIGH-quality trades
    """
    now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not trading_window_open():
        msg = f"V3.3 Auto {now_ts}: Trading window CLOSED, skipping."
        print(msg)
        if args.telegram:
            send_telegram(f"*V3.3 Auto*\n{msg}")
        return

    try:
        open_trades = get_open_trade_count()
    except Exception as e:
        msg = f"V3.3 Auto {now_ts}: Error getting open trades: {e}"
        print(msg)
        if args.telegram:
            send_telegram(f"*V3.3 Auto*\n{msg}")
        return

    if open_trades >= 3:
        msg = f"V3.3 Auto {now_ts}: Max trades open ({open_trades}) – no new trades."
        print(msg)
        if args.telegram:
            send_telegram(f"*V3.3 Auto*\n{msg}")
        return

    signals: List[Signal] = []
    trades_sent: List[Tuple[Signal, str]] = []

    for inst in INSTRUMENTS:
        try:
            sig = build_signal(inst)
        except Exception as e:
            print(f"[ERROR] {inst}: {e}")
            continue
        signals.append(sig)

    # Only HIGH-quality
    high_signals = [s for s in signals if s.side != "NONE" and s.quality == "HIGH"]

    # Respect max 3 trades on account
    slots_left = max(0, 3 - open_trades)
    high_signals = high_signals[:slots_left]

    for s in high_signals:
        payload = build_alert_payload(s)
        result = send_to_bridge(payload)
        trades_sent.append((s, result))
        print(f"[TRADE] {s.instrument} {s.side} {s.quality} → {result}")

    for s in signals:
        if s not in high_signals:
            print(f"[SKIP ] {s.instrument} {s.side} {s.quality} – {s.reason}")

    if args.telegram:
        lines = [f"*V3.3 Auto {now_ts}*"]
        lines.append(f"Open trades before run: {open_trades}")

        if trades_sent:
            lines.append("*Trades sent:*")
            for s, res in trades_sent:
                lines.append(
                    f"`{s.instrument}` {s.side} {s.quality} "
                    f"TP ~{s.tp_pips:.1f}p, SL ~{s.sl_pips:.1f}p\n{s.reason}"
                )
        else:
            lines.append("_No HIGH-quality trades sent this cycle._")

        send_telegram("\n".join(lines))


def main():
    if not OANDA_API_KEY:
        print("OANDA_API_KEY not set – export it first.")
        sys.exit(1)
    if not OANDA_ACCOUNT_ID:
        print("OANDA_ACCOUNT_ID not set – export it first.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="V3.3 Hybrid 4H+1H+15M auto-trader (OANDA-only, CLI)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan pairs and print signals")
    p_scan.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    p_scan.set_defaults(func=cmd_scan)

    p_auto = sub.add_parser("auto", help="Auto-mode: scan + send HIGH-quality trades to OANDA Bridge")
    p_auto.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    p_auto.set_defaults(func=cmd_auto)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
