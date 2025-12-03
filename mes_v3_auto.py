#!/usr/bin/env python3
"""
MES v3.1 – Impulse Breakout / Continuation – CLI Auto-Trader + Backtester (OANDA-only, Python)

Changes vs v3.0
---------------
- Removed: 1H ATR expansion filter (ATR_now > ATR_prev).
- Added: 3-bar M15 impulse + acceleration filter:
    * Last 3 M15 candles all bullish or all bearish.
    * Total move over those 3 bars >= IMPULSE_MIN_PIPS.
    * Average range of those 3 bars >= IMPULSE_MIN_AVG_RANGE_ATR_FACTOR * 1H ATR.
- Kept:
    * Break of recent structure (last 2 highs/lows on M15).
    * Candle body >= 30% of 1H ATR (breakout strength).
    * EMA200 trend filter (1H + 4H must align for direction).
    * SL at swing high/low, TP at 2.0R multiples.
    * Same risk sizing and bridge/Telegram plumbing.

Usage
-----
AUTO (used by systemd service):
    python mes_v3_auto.py auto

BACKTEST (manual):
    python mes_v3_auto.py backtest
    python mes_v3_auto.py backtest --days 60
    python mes_v3_auto.py backtest --pair EUR_USD --days 90
"""

import os
import sys
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import List, Optional

import pathlib

import requests
import pandas as pd
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# .ENV LOADING SUPPORT
# ─────────────────────────────────────────────────────────────

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
    print(f"[MES] Loaded environment from {ENV_PATH}")
else:
    print("[MES] No .env file found – using OS/systemd/1Password `op run` environment")

# ─────────────────────────────────────────────────────────────
# CONFIG / ENV
# ─────────────────────────────────────────────────────────────

OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
OANDA_BRIDGE_URL = os.getenv("OANDA_BRIDGE_URL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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

# Risk config (kept from v2.1.1 / v3.0)
RISK_PERCENT = 25.0
MIN_DOLLAR_PER_PIP = 1.0
MAX_UNITS_CLAMP = 15000

# ATR / signal filters
MIN_ATR_PIPS = 8.0  # minimum 1H ATR in pips to consider setups

# NEW: Impulse / acceleration filters (v3.1)
IMPULSE_LOOKBACK = 3                     # last 3 × M15 bars (45 minutes)
IMPULSE_MIN_PIPS = 10.0                  # minimum move over those 3 bars
IMPULSE_MIN_AVG_RANGE_ATR_FACTOR = 0.4   # avg M15 range >= 40% of 1H ATR

# Max open trades
MAX_OPEN_TRADES = 3

# Backtest defaults
BACKTEST_DEFAULT_DAYS = 30

# Breakout engine constants
BREAKOUT_BODY_ATR_FACTOR = 0.3  # body >= 30% of 1H ATR
RR_TARGET = 2.0                  # fixed 2.0R target

# ─────────────────────────────────────────────────────────────
# TIME WINDOW
# ─────────────────────────────────────────────────────────────

def trading_window_open() -> bool:
    """
    Sunday 14:00 → Friday 14:00 (server local time)
    """
    now = datetime.now()
    wd = now.weekday()
    hour = now.hour

    if wd == 6:  # Sunday
        return hour >= 14
    if 0 <= wd <= 3:  # Mon–Thu
        return True
    if wd == 4:  # Friday
        return hour < 14
    return False

# ─────────────────────────────────────────────────────────────
# OANDA HELPERS
# ─────────────────────────────────────────────────────────────

def get_candles(instrument: str, granularity: str, count: int = 500, price: str = "M") -> pd.DataFrame:
    url = f"{OANDA_REST_URL}/v3/instruments/{instrument}/candles"
    params = {"granularity": granularity, "count": count, "price": price}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("candles", [])

    rows = []
    for c in data:
        if not c.get("complete", False):
            continue
        t = c["time"]
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if price == "M":
            mid = c["mid"]
            rows.append({
                "time": dt,
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": int(c["volume"]),
            })

    if not rows:
        raise RuntimeError(f"No candles returned for {instrument} {granularity}")

    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)

    if df.index.tz is None:
        df.index = df.index.tz_localize(UTC)
    else:
        df.index = df.index.tz_convert(UTC)

    return df


def get_open_trade_count() -> int:
    url = f"{OANDA_REST_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openPositions"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return len(resp.json().get("positions", []))


def get_nav() -> float:
    url = f"{OANDA_REST_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    nav_str = resp.json().get("account", {}).get("NAV")
    if nav_str is None:
        raise RuntimeError("NAV not found")
    return float(nav_str)


def send_to_bridge(message: str, instrument: str, price: float, sl: float, tp: float, units: int) -> str:
    if not OANDA_BRIDGE_URL:
        return "[BRIDGE] OANDA_BRIDGE_URL not set – skipping"

    payload = {
        "message": message,
        "instrument": instrument,
        "price": round(price, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "qty": int(units),
    }

    try:
        resp = requests.post(OANDA_BRIDGE_URL, json=payload, timeout=15)
        return f"[BRIDGE] {resp.status_code} {resp.text}"
    except Exception as e:
        return f"[BRIDGE] ERROR: {e}"

# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# INDICATORS / UTILS
# ─────────────────────────────────────────────────────────────

def rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    r = series.copy()
    if len(series) == 0:
        return series
    seed_len = min(length, len(series))
    r.iloc[0] = series.iloc[:seed_len].mean()
    for i in range(1, len(series)):
        r.iloc[i] = alpha * series.iloc[i] + (1 - alpha) * r.iloc[i - 1]
    return r


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
    return price_diff / (0.01 if "JPY" in instrument else 0.0001)


def price_from_pips(instrument: str, price: float, pips: float, direction: str) -> float:
    step = 0.01 if "JPY" in instrument else 0.0001
    diff = pips * step
    return price + diff if direction == "up" else price - diff

# ─────────────────────────────────────────────────────────────
# MES SIGNAL STRUCT
# ─────────────────────────────────────────────────────────────

@dataclass
class MesSignal:
    instrument: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: float
    atr_pips: float
    tp_pips: float
    ssi: float   # kept for compatibility; not used in v3 breakout
    reason: str
    units: int = 0

# ─────────────────────────────────────────────────────────────
# HELPER: BREAKOUT LOGIC (SHARED LIVE + BACKTEST)
# ─────────────────────────────────────────────────────────────

def compute_breakout_for_last_bar(
    instrument: str,
    m15: pd.DataFrame,
    atr1h_a: pd.Series,
    trend_up: pd.Series,
    trend_down: pd.Series,
) -> MesSignal:
    """
    Compute MES v3.1 breakout signal on the *latest* completed M15 bar.
    Used by live AUTO mode.
    """
    # Ensure sorted index and enough data
    m15 = m15.sort_index()
    if len(m15) < max(3, IMPULSE_LOOKBACK):
        last_close = float(m15["close"].iloc[-1])
        return MesSignal(
            instrument, "NONE", last_close, 0.0, 0.0, 0.0, 0.0, 0.0,
            f"Not enough M15 candles (need >= {max(3, IMPULSE_LOOKBACK)})", 0
        )

    last_idx = m15.index[-1]

    if pd.isna(atr1h_a.loc[last_idx]):
        last_close = float(m15["close"].loc[last_idx])
        return MesSignal(
            instrument, "NONE", last_close, 0.0, 0.0, 0.0, 0.0, 0.0,
            "ATR data not available at last bar", 0
        )

    atr_now = float(atr1h_a.loc[last_idx])
    atr_pips = pips_diff(instrument, atr_now)

    last_open = float(m15["open"].loc[last_idx])
    last_close = float(m15["close"].loc[last_idx])
    last_high = float(m15["high"].loc[last_idx])
    last_low = float(m15["low"].loc[last_idx])

    body = abs(last_close - last_open)

    # 1) Volatility floor – still require "enough" ATR on 1H
    if atr_pips < MIN_ATR_PIPS:
        return MesSignal(
            instrument, "NONE", last_close, 0.0, 0.0, atr_pips, 0.0, 0.0,
            f"ATR too low ({atr_pips:.1f} pips)", 0
        )

    # 2) NEW: 3-bar impulse + acceleration filter (Option A + D)
    recent = m15.iloc[-IMPULSE_LOOKBACK:]
    first_open = float(recent["open"].iloc[0])
    last_close_imp = float(recent["close"].iloc[-1])

    up_seq = bool((recent["close"] > recent["open"]).all())
    down_seq = bool((recent["close"] < recent["open"]).all())

    move_pips = pips_diff(instrument, last_close_imp - first_open)

    ranges = recent["high"] - recent["low"]
    avg_range = float(ranges.mean())
    avg_range_pips = pips_diff(instrument, avg_range)

    impulse_ok = move_pips >= IMPULSE_MIN_PIPS
    accel_ok = avg_range_pips >= IMPULSE_MIN_AVG_RANGE_ATR_FACTOR * atr_pips

    if not ((up_seq or down_seq) and impulse_ok and accel_ok):
        reason = (
            f"No 3-bar impulse: dir_ok={up_seq or down_seq}, "
            f"move={move_pips:.1f}p, avg_range={avg_range_pips:.1f}p"
        )
        return MesSignal(
            instrument, "NONE", last_close, 0.0, 0.0, atr_pips, 0.0, 0.0,
            reason, 0
        )

    # 3) Keep breakout body filter so last bar isn't tiny
    body_ok = body >= BREAKOUT_BODY_ATR_FACTOR * atr_now
    if not body_ok:
        return MesSignal(
            instrument, "NONE", last_close, 0.0, 0.0, atr_pips, 0.0, 0.0,
            "Breakout body too small vs ATR", 0
        )

    trend_up_now = bool(trend_up.loc[last_idx])
    trend_down_now = bool(trend_down.loc[last_idx])

    # Swing highs/lows from previous 2 completed candles (last 3..1)
    prior = m15.iloc[-3:-1]
    hi_swing = float(prior["high"].max())
    lo_swing = float(prior["low"].min())

    break_up = last_close > hi_swing
    break_down = last_close < lo_swing

    long_signal = trend_up_now and break_up
    short_signal = trend_down_now and break_down

    if not long_signal and not short_signal:
        reason = "No MES v3.1 breakout (structure/trend mismatch)"
        return MesSignal(
            instrument, "NONE", last_close, 0.0, 0.0, atr_pips, 0.0, 0.0,
            reason, 0
        )

    entry = last_close

    if long_signal:
        sl = lo_swing
        risk_pips = pips_diff(instrument, entry - sl)
        side = "BUY"
    else:
        sl = hi_swing
        risk_pips = pips_diff(instrument, sl - entry)
        side = "SELL"

    if risk_pips <= 0:
        return MesSignal(
            instrument, "NONE", entry, 0.0, 0.0, atr_pips, 0.0, 0.0,
            "Invalid risk distance (risk_pips <= 0)", 0
        )

    tp_pips = risk_pips * RR_TARGET
    if side == "BUY":
        tp = price_from_pips(instrument, entry, tp_pips, "up")
    else:
        tp = price_from_pips(instrument, entry, tp_pips, "down")

    # Unit sizing based on risk distance (will be overwritten in build_mes_signal using NAV)
    pip_value = 0.01 if "JPY" in instrument else 0.0001
    risk_amount = get_nav() * (RISK_PERCENT / 100.0)
    per_unit_risk = risk_pips * pip_value
    units_raw = risk_amount / per_unit_risk if per_unit_risk > 0 else 0
    units_min = MIN_DOLLAR_PER_PIP / pip_value
    units = max(units_raw, units_min)
    units_clamped = int(min(MAX_UNITS_CLAMP, max(1000, units)))

    reason = f"MES v3.1 Breakout {side.title()} (impulse)"

    return MesSignal(
        instrument,
        side,
        entry,
        sl,
        tp,
        atr_pips,
        tp_pips,
        0.0,   # ssi not used in v3
        reason,
        units_clamped
    )

# ─────────────────────────────────────────────────────────────
# MES SIGNAL (LIVE ENGINE)
# ─────────────────────────────────────────────────────────────

def build_mes_signal(instrument: str, account_nav: float) -> MesSignal:
    """
    Live engine: pulls latest M15/H1/H4, applies v3.1 breakout logic.
    """
    h1 = get_candles(instrument, "H1", count=500)
    h4 = get_candles(instrument, "H4", count=500)
    m15 = get_candles(instrument, "M15", count=500)

    for df in (h1, h4, m15):
        df.sort_index(inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize(UTC)
        else:
            df.index = df.index.tz_convert(UTC)

    atr1h = calc_atr(h1, 14)
    atr1h_a = atr1h.reindex(m15.index, method="ffill")

    # EMA200 trend filter (1H + 4H must align)
    h1["ema200"] = ema(h1["close"], 200)
    h4["ema200"] = ema(h4["close"], 200)
    ema1h_a = h1["ema200"].reindex(m15.index, method="ffill")
    ema4h_a = h4["ema200"].reindex(m15.index, method="ffill")
    close1h_a = h1["close"].reindex(m15.index, method="ffill")
    close4h_a = h4["close"].reindex(m15.index, method="ffill")

    trend_up = (close1h_a > ema1h_a) & (close4h_a > ema4h_a)
    trend_down = (close1h_a < ema1h_a) & (close4h_a < ema4h_a)

    # Delegate to breakout logic helper
    sig = compute_breakout_for_last_bar(instrument, m15, atr1h_a, trend_up, trend_down)

    # Use provided account_nav for unit sizing instead of recalculating inside helper
    if sig.side in ("BUY", "SELL") and sig.units > 0:
        pip_value = 0.01 if "JPY" in instrument else 0.0001
        if sig.side == "BUY":
            risk_pips = pips_diff(instrument, sig.entry_price - sig.sl_price)
        else:
            risk_pips = pips_diff(instrument, sig.sl_price - sig.entry_price)
        risk_amount = account_nav * (RISK_PERCENT / 100.0)
        per_unit_risk = risk_pips * pip_value
        units_raw = risk_amount / per_unit_risk if per_unit_risk > 0 else 0
        units_min = MIN_DOLLAR_PER_PIP / pip_value
        units = max(units_raw, units_min)
        units_clamped = int(min(MAX_UNITS_CLAMP, max(1000, units)))
        sig.units = units_clamped

    return sig

# ─────────────────────────────────────────────────────────────
# BACKTEST ENGINE – MES v3.1 BREAKOUT
# ─────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    instrument: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    sl_price: float
    tp_price: float
    result: str
    rr: float
    pips: float


def run_backtest(days: int = BACKTEST_DEFAULT_DAYS, pair: Optional[str] = None) -> None:
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        print("OANDA_API_KEY or OANDA_ACCOUNT_ID missing – cannot backtest.")
        sys.exit(1)

    end = datetime.now(UTC)
    start = end - timedelta(days=days)

    inst_list = [pair] if pair else INSTRUMENTS

    print(f"[MES v3.1 BACKTEST] Starting backtest for {days} days...")
    print(f"[MES v3.1 BACKTEST] Time window: {start} → {end}")
    print(f"[MES v3.1 BACKTEST] Instruments: {', '.join(inst_list)}")

    all_trades: List[BacktestTrade] = []

    for instrument in inst_list:
        print(f"[MES v3.1 BACKTEST] Fetching history for {instrument}...")

        # Rough counts: enough to cover days + buffer
        m15 = get_candles(instrument, "M15", count=days * 96 + 500)
        h1 = get_candles(instrument, "H1", count=days * 24 + 200)
        h4 = get_candles(instrument, "H4", count=days * 6 + 50)

        for df in (m15, h1, h4):
            df.sort_index(inplace=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize(UTC)
            else:
                df.index = df.index.tz_convert(UTC)

        # Clip to backtest window
        m15 = m15[(m15.index >= start) & (m15.index <= end)]
        h1 = h1[(h1.index <= end)]
        h4 = h4[(h4.index <= end)]

        if m15.empty or h1.empty or h4.empty:
            print(f"[MES v3.1 BACKTEST] Not enough data for {instrument}, skipping.")
            continue

        atr1h = calc_atr(h1, 14)
        atr1h_a = atr1h.reindex(m15.index, method="ffill")

        # EMA200 trend filter
        h1["ema200"] = ema(h1["close"], 200)
        h4["ema200"] = ema(h4["close"], 200)
        ema1h_a = h1["ema200"].reindex(m15.index, method="ffill")
        ema4h_a = h4["ema200"].reindex(m15.index, method="ffill")
        close1h_a = h1["close"].reindex(m15.index, method="ffill")
        close4h_a = h4["close"].reindex(m15.index, method="ffill")

        trend_up = (close1h_a > ema1h_a) & (close4h_a > ema4h_a)
        trend_down = (close1h_a < ema1h_a) & (close4h_a < ema4h_a)

        in_position = False
        pos_side = ""
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        entry_time: Optional[datetime] = None
        risk_pips_entry = 0.0

        m15 = m15.sort_index()

        for i, ts in enumerate(m15.index):
            if pd.isna(atr1h_a.loc[ts]):
                continue

            price_close = float(m15["close"].loc[ts])
            price_high = float(m15["high"].loc[ts])
            price_low = float(m15["low"].loc[ts])

            atr_now = float(atr1h_a.loc[ts])
            atr_pips = pips_diff(instrument, atr_now)

            # Manage existing position first
            if in_position:
                if pos_side == "BUY":
                    if price_low <= sl_price:
                        exit_price = sl_price
                        result = "SL"
                    elif price_high >= tp_price:
                        exit_price = tp_price
                        result = "TP"
                    else:
                        continue
                else:
                    if price_high >= sl_price:
                        exit_price = sl_price
                        result = "SL"
                    elif price_low <= tp_price:
                        exit_price = tp_price
                        result = "TP"
                    else:
                        continue

                pip_move = pips_diff(
                    instrument,
                    (exit_price - entry_price) if pos_side == "BUY" else (entry_price - exit_price)
                )
                rr = pip_move / risk_pips_entry if risk_pips_entry > 0 else 0.0

                all_trades.append(
                    BacktestTrade(
                        instrument,
                        pos_side,
                        entry_time,
                        ts.to_pydatetime(),
                        entry_price,
                        exit_price,
                        sl_price,
                        tp_price,
                        result,
                        rr,
                        pip_move,
                    )
                )
                in_position = False
                pos_side = ""
                continue

            # No position → look for MES v3.1 breakout entry
            if i < max(3, IMPULSE_LOOKBACK - 1):
                continue  # need enough candles for swings + impulse

            if atr_pips < MIN_ATR_PIPS:
                continue

            # Impulse + acceleration filter on last IMPULSE_LOOKBACK bars (including this one)
            start_idx = i - IMPULSE_LOOKBACK + 1
            if start_idx < 0:
                continue
            recent = m15.iloc[start_idx : i + 1]

            recent_open0 = float(recent["open"].iloc[0])
            recent_close_last = float(recent["close"].iloc[-1])

            up_seq = bool((recent["close"] > recent["open"]).all())
            down_seq = bool((recent["close"] < recent["open"]).all())

            move_pips = pips_diff(instrument, recent_close_last - recent_open0)
            ranges = recent["high"] - recent["low"]
            avg_range = float(ranges.mean())
            avg_range_pips = pips_diff(instrument, avg_range)

            impulse_ok = move_pips >= IMPULSE_MIN_PIPS
            accel_ok = avg_range_pips >= IMPULSE_MIN_AVG_RANGE_ATR_FACTOR * atr_pips

            if not ((up_seq or down_seq) and impulse_ok and accel_ok):
                continue

            trend_up_now = bool(trend_up.loc[ts])
            trend_down_now = bool(trend_down.loc[ts])

            last_open = float(m15["open"].loc[ts])
            body = abs(price_close - last_open)
            if body < BREAKOUT_BODY_ATR_FACTOR * atr_now:
                continue

            prior = m15.iloc[i - 3 : i - 1]
            hi_swing = float(prior["high"].max())
            lo_swing = float(prior["low"].min())

            break_up = price_close > hi_swing
            break_down = price_close < lo_swing

            long_signal = trend_up_now and break_up
            short_signal = trend_down_now and break_down

            if not long_signal and not short_signal:
                continue

            entry_price = price_close

            if long_signal:
                pos_side = "BUY"
                sl_price = lo_swing
                risk_pips_entry = pips_diff(instrument, entry_price - sl_price)
                if risk_pips_entry <= 0:
                    continue
                tp_pips = risk_pips_entry * RR_TARGET
                tp_price = price_from_pips(instrument, entry_price, tp_pips, "up")
            else:
                pos_side = "SELL"
                sl_price = hi_swing
                risk_pips_entry = pips_diff(instrument, sl_price - entry_price)
                if risk_pips_entry <= 0:
                    continue
                tp_pips = risk_pips_entry * RR_TARGET
                tp_price = price_from_pips(instrument, entry_price, tp_pips, "down")

            in_position = True
            entry_time = ts.to_pydatetime()

        # If still in a trade at the end, close at final bar price
        if in_position:
            final_ts = m15.index[-1]
            exit_price = float(m15["close"].iloc[-1])
            pip_move = pips_diff(
                instrument,
                (exit_price - entry_price) if pos_side == "BUY" else (entry_price - exit_price)
            )
            rr = pip_move / risk_pips_entry if risk_pips_entry > 0 else 0.0
            all_trades.append(
                BacktestTrade(
                    instrument,
                    pos_side,
                    entry_time,
                    final_ts.to_pydatetime(),
                    entry_price,
                    exit_price,
                    sl_price,
                    tp_price,
                    "TIMEOUT",
                    rr,
                    pip_move,
                )
            )

    if not all_trades:
        print("[MES v3.1 BACKTEST] No trades generated.")
        return

    df = pd.DataFrame([t.__dict__ for t in all_trades])

    total_trades = len(df)
    wins = (df["result"] == "TP").sum()
    losses = (df["result"] == "SL").sum()
    timeouts = (df["result"] == "TIMEOUT").sum()
    win_rate = wins / total_trades * 100.0

    avg_rr = df["rr"].mean()
    avg_pips = df["pips"].mean()
    sum_pips = df["pips"].sum()

    print("────────────────────────────────────────────")
    print(f"[MES v3.1 BACKTEST] Trades: {total_trades}")
    print(f"[MES v3.1 BACKTEST] Wins : {wins}")
    print(f"[MES v3.1 BACKTEST] Loss : {losses}")
    print(f"[MES v3.1 BACKTEST] Timeouts: {timeouts}")
    print(f"[MES v3.1 BACKTEST] Win rate: {win_rate:.1f}%")
    print(f"[MES v3.1 BACKTEST] Avg R:R : {avg_rr:.2f}")
    print(f"[MES v3.1 BACKTEST] Avg pips: {avg_pips:.1f}")
    print(f"[MES v3.1 BACKTEST] Total pips: {sum_pips:.1f}")
    print("By instrument:")
    print(df.groupby("instrument")["pips"].agg(["count", "sum", "mean"]))

# ─────────────────────────────────────────────────────────────
# AUTO MODE
# ─────────────────────────────────────────────────────────────

def cmd_auto(args) -> None:
    now_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        msg = f"[MES v3.1 AUTO] {now_ts} | OANDA_API_KEY or OANDA_ACCOUNT_ID missing."
        print(msg)
        send_telegram(msg)
        return

    if not trading_window_open():
        msg = f"[MES v3.1 AUTO] {now_ts} | Trading window CLOSED – skipping."
        print(msg)
        send_telegram(msg)
        return

    try:
        nav = get_nav()
    except Exception as e:
        msg = f"[MES v3.1 AUTO] {now_ts} | NAV error: {e}"
        print(msg)
        send_telegram(msg)
        return

    try:
        open_trades = get_open_trade_count()
    except Exception as e:
        msg = f"[MES v3.1 AUTO] {now_ts} | Open trades error: {e}"
        print(msg)
        send_telegram(msg)
        return

    if open_trades >= MAX_OPEN_TRADES:
        msg = f"[MES v3.1 AUTO] {now_ts} | Max trades open ({open_trades}) – no new trades."
        print(msg)
        send_telegram(msg)
        return

    signals: List[MesSignal] = []
    for inst in INSTRUMENTS:
        try:
            sig = build_mes_signal(inst, nav)
            signals.append(sig)
        except Exception as e:
            print(f"[MES v3.1 AUTO] ERROR {inst} | {e}")

    actionable = [s for s in signals if s.side in ("BUY", "SELL") and s.units > 0]
    slots_left = max(0, MAX_OPEN_TRADES - open_trades)
    actionable = actionable[:slots_left]

    trades_sent = []
    for sig in actionable:
        result = send_to_bridge(sig.side, sig.instrument, sig.entry_price,
                                sig.sl_price, sig.tp_price, sig.units)
        trades_sent.append((sig, result))
        print(
            f"[MES v3.1 AUTO] TRADE {sig.instrument} {sig.side} "
            f"units={sig.units} ATR={sig.atr_pips:.1f} TP={sig.tp_pips:.1f} | {result}"
        )

    for sig in signals:
        if sig not in [t[0] for t in trades_sent]:
            print(f"[MES v3.1 AUTO] SKIP {sig.instrument} | {sig.reason}")

    lines = [
        f"*MES v3.1 AUTO {now_ts}*",
        f"Account NAV: `{nav:.2f}`",
        f"Open trades before run: {open_trades}",
    ]
    if trades_sent:
        lines.append("*Trades sent:*")
        for s, res in trades_sent:
            lines.append(
                f"`{s.instrument}` {s.side} units={s.units} "
                f"ATR={s.atr_pips:.1f} TP={s.tp_pips:.1f}p\n{s.reason}"
            )
    else:
        lines.append("_No trades sent this cycle._")
    send_telegram("\n".join(lines))

# ─────────────────────────────────────────────────────────────
# CLI PARSER
# ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) == 1:
        print("Usage: mes_v3_auto.py {auto,backtest} [options]")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="MES v3.1 – Impulse Breakout / Continuation – Auto-Trader + Backtester (OANDA CLI)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_auto = sub.add_parser("auto", help="Run one MES v3.1 auto cycle")
    p_auto.set_defaults(func=cmd_auto)

    p_bt = sub.add_parser("backtest", help="Run MES v3.1 breakout backtest")
    p_bt.add_argument("--days", type=int, default=BACKTEST_DEFAULT_DAYS)
    p_bt.add_argument("--pair", type=str, default=None)

    p_bt.set_defaults(func=lambda args: run_backtest(args.days, args.pair))

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
