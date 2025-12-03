#!/usr/bin/env python3
"""
MES v2.1.1 Pro – CLI Auto-Trader + Backtester (OANDA-only, Python)

Features
--------
- Instruments: EUR/USD, GBP/USD, USD/CAD, USD/CHF, AUD/USD, NZD/USD
- MES v2.0 core logic ported from Pine Script:
    * MTF mood (FearCap, HopeConf, Greed, IndecFear, Neutral)
    * SSI from 15M, 1H, 4H
- v2.1 enhancements:
    * EMA200 trend filter (1H + 4H must align)
    * ATR minimum filter (avoid ultra-chop)
    * SSI tightened to +/-1.0
- v2.1.1 Pro:
    * .env support via python-dotenv
    * AUTO mode (for systemd timer) with ATR-only exits (handled by bridge)
    * BACKTEST mode (30d by default) using MES rules + ATR SL/TP simulation
    * Clean logging + MES v2.1 tags
    * Trading window enforced: Sunday 14:00 → Friday 14:00 (server local time)
    * Max 3 open trades on account
    * Live / practice selectable via OANDA_ENV

Usage
-----
AUTO (used by systemd service):
    python mes_v2_auto.py auto

BACKTEST (manual):
    python mes_v2_auto.py backtest
    python mes_v2_auto.py backtest --days 60
    python mes_v2_auto.py backtest --pair EUR_USD --days 90
"""

import os
import sys
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import List, Optional, Tuple

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
    print("[MES] No .env file found – using OS/systemd environment")

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

# Risk config
RISK_PERCENT = 25.0
MIN_DOLLAR_PER_PIP = 1.0
MAX_UNITS_CLAMP = 15000

# ATR / signal filters
MIN_ATR_PIPS = 8.0
SSI_ENTRY_THRESHOLD = 1.0

# Max open trades
MAX_OPEN_TRADES = 3

# Backtest defaults
BACKTEST_DEFAULT_DAYS = 30

# ─────────────────────────────────────────────────────────────
# TIME WINDOW
# ─────────────────────────────────────────────────────────────

def trading_window_open() -> bool:
    now = datetime.now()
    wd = now.weekday()
    hour = now.hour

    if wd == 6:
        return hour >= 14
    if 0 <= wd <= 3:
        return True
    if wd == 4:
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
# INDICATORS
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
# MES CORE METRICS
# ─────────────────────────────────────────────────────────────

def metrics_from_ohlcv(df: pd.DataFrame, vol_len=20, rng_len=14):
    o = df["open"]; c = df["close"]; h = df["high"]; l = df["low"]; v = df["volume"]

    prev_c = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()],
        axis=1,
    ).max(axis=1)

    vol_sma = v.rolling(vol_len, min_periods=1).mean()
    rng_sma = tr.rolling(rng_len, min_periods=1).mean()

    volR = (v / vol_sma.replace(0, pd.NA)).fillna(0)
    rngR = (tr / rng_sma.replace(0, pd.NA)).fillna(0)

    volRs = ema(volR, 3)
    rngRs = ema(rngR, 3)

    body = (c - o).abs()
    tr_safe = tr.replace(0, 1e-8)
    strength = (body / tr_safe) * volR
    bodyR = body / tr_safe

    bull = c > o
    bear = c < o

    return volRs, rngRs, strength, bull, bear, bodyR


def mood_flags(volRs, rngRs, strength, bull, bear, bodyR):
    fearCap = bear & (volRs > 1.8)
    hopeConf = bull & (volRs > 1.1) & (strength > 0.6)
    greed = bull & (volRs > 1.8) & (rngRs > 1.3) & (bodyR > 0.8)
    indecFear = ((rngRs < 0.7) | (bodyR < 0.25)) | (bear & (volRs > 1.2))
    return fearCap, hopeConf, greed, indecFear


def ssi_from_moods(hope15, fear15, hope1h, fear1h, hope4h, fear4h):
    def score_tf(h, f):
        return h.astype(float) * 1.5 - f.astype(float) * 1.5
    s15 = score_tf(hope15, fear15)
    s1h = score_tf(hope1h, fear1h)
    s4h = score_tf(hope4h, fear4h)
    return ((s15 + s1h + s4h) / 3.0).clip(-3.0, 3.0)

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
    ssi: float
    reason: str
    units: int = 0

# ─────────────────────────────────────────────────────────────
# MES SIGNAL (LIVE ENGINE) — with pandas fixes
# ─────────────────────────────────────────────────────────────

def build_mes_signal(instrument: str, account_nav: float) -> MesSignal:
    h1 = get_candles(instrument, "H1", count=500)
    h4 = get_candles(instrument, "H4", count=500)
    m15 = get_candles(instrument, "M15", count=500)

    for df in (h1, h4, m15):
        df.sort_index(inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize(UTC)
        else:
            df.index = df.index.tz_convert(UTC)

    volRs15, rngRs15, str15, bull15, bear15, bodyR15 = metrics_from_ohlcv(m15)
    volRs1h, rngRs1h, str1h, bull1h, bear1h, bodyR1h = metrics_from_ohlcv(h1)
    volRs4h, rngRs4h, str4h, bull4h, bear4h, bodyR4h = metrics_from_ohlcv(h4)

    fear15, hope15, greed15, indec15 = mood_flags(volRs15, rngRs15, str15, bull15, bear15, bodyR15)
    fear1h, hope1h, greed1h, _ = mood_flags(volRs1h, rngRs1h, str1h, bull1h, bear1h, bodyR1h)
    fear4h, hope4h, greed4h, _ = mood_flags(volRs4h, rngRs4h, str4h, bull4h, bear4h, bodyR4h)

    fear1h_a = fear1h.reindex(m15.index, method="ffill")
    hope1h_a = hope1h.reindex(m15.index, method="ffill")
    greed1h_a = greed1h.reindex(m15.index, method="ffill")
    fear4h_a = fear4h.reindex(m15.index, method="ffill")
    hope4h_a = hope4h.reindex(m15.index, method="ffill")
    greed4h_a = greed4h.reindex(m15.index, method="ffill")

    ssi_series = ssi_from_moods(hope15, fear15, hope1h_a, fear1h_a, hope4h_a, fear4h_a)

    atr1h = calc_atr(h1, 14)
    atr1h_a = atr1h.reindex(m15.index, method="ffill")

    h1["ema200"] = ema(h1["close"], 200)
    h4["ema200"] = ema(h4["close"], 200)
    ema1h_a = h1["ema200"].reindex(m15.index, method="ffill")
    ema4h_a = h4["ema200"].reindex(m15.index, method="ffill")
    close1h_a = h1["close"].reindex(m15.index, method="ffill")
    close4h_a = h4["close"].reindex(m15.index, method="ffill")

    trend_up = (close1h_a > ema1h_a) & (close4h_a > ema4h_a)
    trend_down = (close1h_a < ema1h_a) & (close4h_a < ema4h_a)

    last_idx = m15.index[-1]
    atr_now = float(atr1h_a.loc[last_idx])
    atr_pips = pips_diff(instrument, atr_now)

    if atr_pips < MIN_ATR_PIPS:
        return MesSignal(instrument, "NONE", float(m15["close"].loc[last_idx]), 0, 0,
                         atr_pips, 0, float(ssi_series.loc[last_idx]),
                         f"ATR too low ({atr_pips:.1f})", 0)

    if not bool(trend_up.loc[last_idx] or trend_down.loc[last_idx]):
        return MesSignal(instrument, "NONE", float(m15["close"].loc[last_idx]), 0, 0,
                         atr_pips, 0, float(ssi_series.loc[last_idx]),
                         "No 1H+4H EMA200 trend", 0)

    # ─── CLEANED PREVIOUS MOODS (PANDAS FIX) ─────────────────────
    fear15_prev = fear15.shift(1).infer_objects().fillna(False)
    greed15_prev = greed15.shift(1).infer_objects().fillna(False)
    fear1h_prev = fear1h_a.shift(1).infer_objects().fillna(False)
    greed1h_prev = greed1h_a.shift(1).infer_objects().fillna(False)
    fear4h_prev = fear4h_a.shift(1).infer_objects().fillna(False)
    greed4h_prev = greed4h_a.shift(1).infer_objects().fillna(False)
    # ─────────────────────────────────────────────────────────────

    was_fear_cap = bool((fear15_prev | fear1h_prev | fear4h_prev).loc[last_idx])
    was_greed = bool((greed15_prev | greed1h_prev | greed4h_prev).loc[last_idx])

    hope15_last = bool(hope15.loc[last_idx])
    fear15_last = bool(fear15.loc[last_idx])
    indec15_last = bool(indec15.loc[last_idx])
    volRs15_last = float(volRs15.loc[last_idx])
    rngRs15_last = float(rngRs15.loc[last_idx])
    ssi_last = float(ssi_series.loc[last_idx])
    trend_up_now = bool(trend_up.loc[last_idx])
    trend_down_now = bool(trend_down.loc[last_idx])

    long_signal = (
        was_fear_cap and hope15_last and ssi_last >= SSI_ENTRY_THRESHOLD
        and volRs15_last >= 1.1 and rngRs15_last >= 1.1 and trend_up_now
    )
    short_signal = (
        was_greed and (indec15_last or fear15_last) and ssi_last <= -SSI_ENTRY_THRESHOLD
        and volRs15_last >= 1.1 and rngRs15_last >= 1.1 and trend_down_now
    )

    entry = float(m15["close"].loc[last_idx])

    if not long_signal and not short_signal:
        reason = "No 1H+4H EMA200 trend" if not (trend_up_now or trend_down_now) else \
            "No MES v2.1 entry (Fear/Hope/Greed/SSI not met)"
        return MesSignal(instrument, "NONE", entry, 0, 0, atr_pips, 0, ssi_last, reason, 0)

    rr = 2.0
    tp_pips = atr_pips * rr
    sl_pips = atr_pips

    if long_signal:
        side = "BUY"
        tp = price_from_pips(instrument, entry, tp_pips, "up")
        sl = price_from_pips(instrument, entry, sl_pips, "down")
    else:
        side = "SELL"
        tp = price_from_pips(instrument, entry, tp_pips, "down")
        sl = price_from_pips(instrument, entry, sl_pips, "up")

    pip_value = 0.01 if "JPY" in instrument else 0.0001
    risk_amount = account_nav * (RISK_PERCENT / 100.0)
    per_unit_risk = atr_pips * pip_value
    units_raw = risk_amount / per_unit_risk if per_unit_risk > 0 else 0
    units_min = MIN_DOLLAR_PER_PIP / pip_value
    units = max(units_raw, units_min)
    units_clamped = int(min(MAX_UNITS_CLAMP, max(1000, units)))

    return MesSignal(
        instrument, side, entry, sl, tp, atr_pips, tp_pips, ssi_last,
        "MES v2.1.1 " + ("Long" if side == "BUY" else "Short"), units_clamped
    )

# ─────────────────────────────────────────────────────────────
# BACKTEST ENGINE — with pandas fixes
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

    print(f"[MES v2.1 BACKTEST] Starting backtest for {days} days...")
    print(f"[MES v2.1 BACKTEST] Time window: {start} → {end}")
    print(f"[MES v2.1 BACKTEST] Instruments: {', '.join(inst_list)}")

    all_trades: List[BacktestTrade] = []

    for instrument in inst_list:
        print(f"[MES v2.1 BACKTEST] Fetching history for {instrument}...")

        m15 = get_candles(instrument, "M15", count=days * 96 + 500)
        h1 = get_candles(instrument, "H1", count=days * 24 + 200)
        h4 = get_candles(instrument, "H4", count=days * 6 + 50)

        for df in (m15, h1, h4):
            df.sort_index(inplace=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize(UTC)
            else:
                df.index = df.index.tz_convert(UTC)

        m15 = m15[(m15.index >= start) & (m15.index <= end)]
        h1 = h1[(h1.index <= end)]
        h4 = h4[(h4.index <= end)]

        if m15.empty or h1.empty or h4.empty:
            print(f"[MES v2.1 BACKTEST] Not enough data for {instrument}, skipping.")
            continue

        volRs15, rngRs15, str15, bull15, bear15, bodyR15 = metrics_from_ohlcv(m15)
        volRs1h, rngRs1h, str1h, bull1h, bear1h, bodyR1h = metrics_from_ohlcv(h1)
        volRs4h, rngRs4h, str4h, bull4h, bear4h, bodyR4h = metrics_from_ohlcv(h4)

        fear15, hope15, greed15, indec15 = mood_flags(volRs15, rngRs15, str15, bull15, bear15, bodyR15)
        fear1h, hope1h, greed1h, _ = mood_flags(volRs1h, rngRs1h, str1h, bull1h, bear1h, bodyR1h)
        fear4h, hope4h, greed4h, _ = mood_flags(volRs4h, rngRs4h, str4h, bull4h, bear4h, bodyR4h)

        fear1h_a = fear1h.reindex(m15.index, method="ffill")
        hope1h_a = hope1h.reindex(m15.index, method="ffill")
        greed1h_a = greed1h.reindex(m15.index, method="ffill")
        fear4h_a = fear4h.reindex(m15.index, method="ffill")
        hope4h_a = hope4h.reindex(m15.index, method="ffill")
        greed4h_a = greed4h.reindex(m15.index, method="ffill")

        ssi_series = ssi_from_moods(hope15, fear15, hope1h_a, fear1h_a, hope4h_a, fear4h_a)

        atr1h = calc_atr(h1, 14)
        atr1h_a = atr1h.reindex(m15.index, method="ffill")

        h1["ema200"] = ema(h1["close"], 200)
        h4["ema200"] = ema(h4["close"], 200)
        ema1h_a = h1["ema200"].reindex(m15.index, method="ffill")
        ema4h_a = h4["ema200"].reindex(m15.index, method="ffill")
        close1h_a = h1["close"].reindex(m15.index, method="ffill")
        close4h_a = h4["close"].reindex(m15.index, method="ffill")

        trend_up = (close1h_a > ema1h_a) & (close4h_a > ema4h_a)
        trend_down = (close1h_a < ema1h_a) & (close4h_a < ema4h_a)

        # ─── CLEANED PREVIOUS MOODS (PANDAS FIX) ─────────────────────
        fear15_prev = fear15.shift(1).infer_objects().fillna(False)
        greed15_prev = greed15.shift(1).infer_objects().fillna(False)
        fear1h_prev = fear1h_a.shift(1).infer_objects().fillna(False)
        greed1h_prev = greed1h_a.shift(1).infer_objects().fillna(False)
        fear4h_prev = fear4h_a.shift(1).infer_objects().fillna(False)
        greed4h_prev = greed4h_a.shift(1).infer_objects().fillna(False)
        # ─────────────────────────────────────────────────────────────

        in_position = False
        pos_side = ""
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        entry_time = None
        last_atr_pips = 0.0

        for ts in m15.index:
            if pd.isna(atr1h_a.loc[ts]):
                continue

            price_close = float(m15["close"].loc[ts])
            price_high = float(m15["high"].loc[ts])
            price_low = float(m15["low"].loc[ts])

            atr_now = float(atr1h_a.loc[ts])
            atr_pips = pips_diff(instrument, atr_now)
            last_atr_pips = atr_pips

            trend_up_now = bool(trend_up.loc[ts])
            trend_down_now = bool(trend_down.loc[ts])
            ssi_now = float(ssi_series.loc[ts])

            if in_position:
                if pos_side == "BUY":
                    if price_low <= sl_price:
                        exit_price = sl_price; result = "SL"
                    elif price_high >= tp_price:
                        exit_price = tp_price; result = "TP"
                    else:
                        continue
                else:
                    if price_high >= sl_price:
                        exit_price = sl_price; result = "SL"
                    elif price_low <= tp_price:
                        exit_price = tp_price; result = "TP"
                    else:
                        continue

                pip_move = pips_diff(instrument, (exit_price - entry_price)
                                    if pos_side=="BUY" else (entry_price - exit_price))
                atr_for_rr = last_atr_pips if last_atr_pips > 0 else atr_pips
                rr = pip_move / atr_for_rr if atr_for_rr > 0 else 0.0

                all_trades.append(BacktestTrade(
                    instrument, pos_side, entry_time, ts.to_pydatetime(),
                    entry_price, exit_price, sl_price, tp_price, result, rr, pip_move
                ))
                in_position = False
                pos_side = ""
                continue

            was_fear_cap = bool((fear15_prev | fear1h_prev | fear4h_prev).loc[ts])
            was_greed = bool((greed15_prev | greed1h_prev | greed4h_prev).loc[ts])

            hope15_now = bool(hope15.loc[ts])
            fear15_now = bool(fear15.loc[ts])
            indec15_now = bool(indec15.loc[ts])
            volRs15_now = float(volRs15.loc[ts])
            rngRs15_now = float(rngRs15.loc[ts])

            long_signal = (
                was_fear_cap and hope15_now and ssi_now >= SSI_ENTRY_THRESHOLD
                and volRs15_now >= 1.1 and rngRs15_now >= 1.1
                and trend_up_now and atr_pips >= MIN_ATR_PIPS
            )
            short_signal = (
                was_greed and (indec15_now or fear15_now)
                and ssi_now <= -SSI_ENTRY_THRESHOLD
                and volRs15_now >= 1.1 and rngRs15_now >= 1.1
                and trend_down_now and atr_pips >= MIN_ATR_PIPS
            )

            if not long_signal and not short_signal:
                continue

            entry_price = price_close
            rr = 2.0
            tp_pips = atr_pips * rr
            sl_pips = atr_pips

            if long_signal:
                pos_side = "BUY"
                tp_price = price_from_pips(instrument, entry_price, tp_pips, "up")
                sl_price = price_from_pips(instrument, entry_price, sl_pips, "down")
            else:
                pos_side = "SELL"
                tp_price = price_from_pips(instrument, entry_price, tp_pips, "down")
                sl_price = price_from_pips(instrument, entry_price, sl_pips, "up")

            in_position = True
            entry_time = ts.to_pydatetime()

        if in_position:
            exit_price = float(m15["close"].iloc[-1])
            pip_move = pips_diff(instrument,
                (exit_price - entry_price) if pos_side=="BUY" else (entry_price - exit_price)
            )
            atr_for_rr = last_atr_pips if last_atr_pips > 0 else 1.0
            rr = pip_move / atr_for_rr
            all_trades.append(BacktestTrade(
                instrument, pos_side, entry_time,
                m15.index[-1].to_pydatetime(), entry_price,
                exit_price, sl_price, tp_price, "TIMEOUT", rr, pip_move
            ))

    if not all_trades:
        print("[MES v2.1 BACKTEST] No trades generated.")
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
    print(f"[MES v2.1 BACKTEST] Trades: {total_trades}")
    print(f"[MES v2.1 BACKTEST] Wins : {wins}")
    print(f"[MES v2.1 BACKTEST] Loss : {losses}")
    print(f"[MES v2.1 BACKTEST] Timeouts: {timeouts}")
    print(f"[MES v2.1 BACKTEST] Win rate: {win_rate:.1f}%")
    print(f"[MES v2.1 BACKTEST] Avg R:R : {avg_rr:.2f}")
    print(f"[MES v2.1 BACKTEST] Avg pips: {avg_pips:.1f}")
    print(f"[MES v2.1 BACKTEST] Total pips: {sum_pips:.1f}")
    print("By instrument:")
    print(df.groupby("instrument")["pips"].agg(["count", "sum", "mean"]))

# ─────────────────────────────────────────────────────────────
# AUTO MODE
# ─────────────────────────────────────────────────────────────

def cmd_auto(args) -> None:
    now_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        msg = f"[MES v2.1 AUTO] {now_ts} | OANDA_API_KEY or OANDA_ACCOUNT_ID missing."
        print(msg); send_telegram(msg); return

    if not trading_window_open():
        msg = f"[MES v2.1 AUTO] {now_ts} | Trading window CLOSED – skipping."
        print(msg); send_telegram(msg); return

    try:
        nav = get_nav()
    except Exception as e:
        msg = f"[MES v2.1 AUTO] {now_ts} | NAV error: {e}"
        print(msg); send_telegram(msg); return

    try:
        open_trades = get_open_trade_count()
    except Exception as e:
        msg = f"[MES v2.1 AUTO] {now_ts} | Open trades error: {e}"
        print(msg); send_telegram(msg); return

    if open_trades >= MAX_OPEN_TRADES:
        msg = f"[MES v2.1 AUTO] {now_ts} | Max trades open ({open_trades}) – no new trades."
        print(msg); send_telegram(msg); return

    signals = []
    for inst in INSTRUMENTS:
        try:
            sig = build_mes_signal(inst, nav)
            signals.append(sig)
        except Exception as e:
            print(f"[MES v2.1 AUTO] ERROR {inst} | {e}")

    actionable = [s for s in signals if s.side in ("BUY","SELL") and s.units > 0]
    slots_left = max(0, MAX_OPEN_TRADES - open_trades)
    actionable = actionable[:slots_left]

    trades_sent = []
    for sig in actionable:
        result = send_to_bridge(sig.side, sig.instrument, sig.entry_price,
                                sig.sl_price, sig.tp_price, sig.units)
        trades_sent.append((sig, result))
        print(f"[MES v2.1 AUTO] TRADE {sig.instrument} {sig.side} "
              f"units={sig.units} ATR={sig.atr_pips:.1f} TP={sig.tp_pips:.1f} | {result}")

    for sig in signals:
        if sig not in [t[0] for t in trades_sent]:
            print(f"[MES v2.1 AUTO] SKIP {sig.instrument} | {sig.reason}")

    lines = [
        f"*MES v2.1 AUTO {now_ts}*",
        f"Account NAV: `{nav:.2f}`",
        f"Open trades before run: {open_trades}",
    ]
    if trades_sent:
        lines.append("*Trades sent:*")
        for s, res in trades_sent:
            lines.append(f"`{s.instrument}` {s.side} units={s.units} "
                         f"ATR={s.atr_pips:.1f} TP={s.tp_pips:.1f}p\n{s.reason}")
    else:
        lines.append("_No trades sent this cycle._")
    send_telegram("\n".join(lines))

# ─────────────────────────────────────────────────────────────
# CLI PARSER
# ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) == 1:
        print("Usage: mes_v2_auto.py {auto,backtest} [options]")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="MES v2.1.1 Pro – Auto-Trader + Backtester (OANDA CLI)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_auto = sub.add_parser("auto", help="Run one MES auto cycle")
    p_auto.set_defaults(func=cmd_auto)

    p_bt = sub.add_parser("backtest", help="Run MES v2.1 backtest")
    p_bt.add_argument("--days", type=int, default=BACKTEST_DEFAULT_DAYS)
    p_bt.add_argument("--pair", type=str, default=None)

    p_bt.set_defaults(func=lambda args: run_backtest(args.days, args.pair))

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
