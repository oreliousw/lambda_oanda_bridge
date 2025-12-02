#!/usr/bin/env python3
"""
MES v2.0 CLI Auto-Trader – OANDA Only (ATR Exits)

- Rebuilds Mr O's MES v2.0 Core in Python using OANDA data.
- Uses MES for entries (FearCap/HopeConf/Greed/IndecFear + SSI).
- Uses ATR-based SL/TP only (no SSI exits).
- Sends trades to Lambda OANDA Bridge with SAME JSON structure as Pine alerts:
    {"message": "BUY/SELL", "instrument": "...", "price": ..., "sl": ..., "tp": ..., "qty": ...}

Usage:
    python mes_v2_auto.py scan --telegram
    python mes_v2_auto.py auto --telegram

Intended to run via systemd timer every 15 minutes.
"""

import os
import sys
import json
import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple, Dict, Set

import requests
import pandas as pd

# ─────────────────────────────────────────────────────────────
# CONFIG / ENV
# ─────────────────────────────────────────────────────────────

OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV        = os.getenv("OANDA_ENV", "practice")  # "practice" or "live"
OANDA_BRIDGE_URL = os.getenv("OANDA_BRIDGE_URL", "")   # Lambda webhook URL

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

# Instruments to scan
INSTRUMENTS = [
    "EUR_USD",
    "GBP_USD",
    "USD_CAD",
    "USD_CHF",
    "AUD_USD",
    "NZD_USD",
]

# MES sizing / risk knobs
ATR_LENGTH    = 14
RR_RATIO      = 2.0
RISK_PCT      = 25.0      # 25% of equity at ATR distance (like Pine's riskPct)
MIN_DOLLAR_PIP = 1.0      # approx $1 per pip minimum
MAX_UNITS_DEMO = 12000    # clamp to OANDA-demo-safe range


# ─────────────────────────────────────────────────────────────
# TIME WINDOW
# ─────────────────────────────────────────────────────────────

def trading_window_open() -> bool:
    """
    Allow trading only:
    - Sunday 14:00 → Friday 14:00 (server local time)
    weekday(): Monday=0, ..., Sunday=6
    """
    now = datetime.now()
    wd = now.weekday()   # 0=Mon, 4=Fri, 6=Sun
    hour = now.hour

    # Sunday: only after 14:00
    if wd == 6:  # Sunday
        return hour >= 14

    # Monday–Thursday: open all day
    if 0 <= wd <= 3:
        return True

    # Friday: only before 14:00
    if wd == 4:  # Friday
        return hour < 14

    # Saturday: closed
    return False


# ─────────────────────────────────────────────────────────────
# OANDA HELPERS – CANDLES, ACCOUNT, POSITIONS
# ─────────────────────────────────────────────────────────────

def get_candles(instrument: str, granularity: str, count: int = 300) -> pd.DataFrame:
    """
    Pull mid-price candles from OANDA for a given instrument and timeframe.
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
            "time":  datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
            "open":  float(c["mid"]["o"]),
            "high":  float(c["mid"]["h"]),
            "low":   float(c["mid"]["l"]),
            "close": float(c["mid"]["c"]),
            "volume": int(c["volume"]),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"No candles returned for {instrument} {granularity}")
    df.set_index("time", inplace=True)
    return df


def get_account_nav() -> float:
    """
    Get current NAV (equity) from OANDA account summary.
    Used for MES sizing.
    """
    url = f"{OANDA_REST_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    account = resp.json().get("account", {})
    # Prefer NAV, fallback to balance
    nav_str = account.get("NAV") or account.get("balance")
    if nav_str is None:
        raise RuntimeError("Could not read account NAV/balance from OANDA summary.")
    return float(nav_str)


def get_open_positions() -> Tuple[int, Set[str]]:
    """
    Return (open_positions_count, set_of_instruments_with_positions).
    Counts open positions at OANDA level.
    """
    url = f"{OANDA_REST_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openPositions"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    positions = resp.json().get("positions", [])
    instruments = set()
    for p in positions:
        inst = p.get("instrument")
        if not inst:
            continue
        # Consider any non-zero long or short units as "open"
        long_units = float(p.get("long", {}).get("units", "0"))
        short_units = float(p.get("short", {}).get("units", "0"))
        if long_units != 0 or short_units != 0:
            instruments.add(inst)
    return len(positions), instruments


# ─────────────────────────────────────────────────────────────
# INDICATORS – RMA, ATR, METRICS, MOODS
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


def calc_atr(df: pd.DataFrame, length: int = ATR_LENGTH) -> pd.Series:
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
        step = 0.01
    else:
        step = 0.0001
    return price_diff / step


def round_price(instrument: str, price: float) -> float:
    """
    Round to typical FX precision:
    - JPY pairs: 3 decimals
    - others:    5 decimals
    """
    if "JPY" in instrument:
        return round(price, 3)
    return round(price, 5)


@dataclass
class Metrics:
    volRs: pd.Series
    rngRs: pd.Series
    strength: pd.Series
    bull: pd.Series
    bear: pd.Series
    bodyR: pd.Series


def compute_metrics(df: pd.DataFrame) -> Metrics:
    """
    Recreate f_metrics() from MES v2.0:
    Returns volRs, rngRs, strength, bull, bear, bodyR series.
    """
    o = df["open"]
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    volLen = 20
    rngLen = 14

    prev_c = c.shift(1)
    tr1 = h - l
    tr2 = (h - prev_c).abs()
    tr3 = (l - prev_c).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    vol_sma = v.rolling(volLen).mean()
    rng_sma = tr.rolling(rngLen).mean()

    volR = v / vol_sma
    rngR = tr / rng_sma

    volRs = ema(volR, 3)
    rngRs = ema(rngR, 3)

    body = (c - o).abs()
    tr_safe = tr.replace(0, 1e-10)
    strength = body / tr_safe * volR
    bodyR = body / tr_safe

    bull = c > o
    bear = c < o

    return Metrics(volRs=volRs, rngRs=rngRs, strength=strength,
                   bull=bull, bear=bear, bodyR=bodyR)


@dataclass
class Moods:
    fearCap: pd.Series
    hopeConf: pd.Series
    greed: pd.Series
    indecFear: pd.Series


def compute_moods(metrics: Metrics) -> Moods:
    """
    Compute mood flags for a given timeframe.
    Matches MES v2.0 conditions.
    """
    volRs   = metrics.volRs
    rngRs   = metrics.rngRs
    strength = metrics.strength
    bull    = metrics.bull
    bear    = metrics.bear
    bodyR   = metrics.bodyR

    fearCap   = bear & (volRs > 1.8) & (rngRs > 1.3)
    hopeConf  = bull & (volRs > 1.1) & (rngRs > 1.0) & (strength > 0.6)
    greed     = bull & (volRs > 1.8) & (rngRs > 1.3) & (bodyR > 0.8)
    indecFear = (rngRs < 0.7) | (bodyR < 0.25) | (bear & (volRs > 1.2))

    return Moods(fearCap=fearCap,
                 hopeConf=hopeConf,
                 greed=greed,
                 indecFear=indecFear)


def compute_ssi(hope15: pd.Series, fear15: pd.Series,
                hope1h: pd.Series, fear1h: pd.Series,
                hope4h: pd.Series, fear4h: pd.Series) -> pd.Series:
    """
    SSI = clamp((s15 + s1h + s4h) / 3, -3, 3)
    scoreTF(_h,_f)=>(_h?1.5:0)-(_f?1.5:0)
    """
    s15 = hope15.astype(float)*1.5 - fear15.astype(float)*1.5
    s1h = hope1h.astype(float)*1.5 - fear1h.astype(float)*1.5
    s4h = hope4h.astype(float)*1.5 - fear4h.astype(float)*1.5
    ssi = (s15 + s1h + s4h) / 3.0
    ssi = ssi.clip(lower=-3.0, upper=3.0)
    return ssi


# ─────────────────────────────────────────────────────────────
# MES SIGNAL BUILDING
# ─────────────────────────────────────────────────────────────

@dataclass
class MesSignal:
    instrument: str
    side: str           # "BUY", "SELL", "NONE"
    entry_price: float
    sl_price: float
    tp_price: float
    atr_pips: float
    units: int
    ssi: float
    reason: str


def build_mes_signal(instrument: str, account_nav: float) -> MesSignal:
    """
    Build MES signal for a single instrument using MES v2.0 logic:
    - Uses 15M as "base" TF (chart TF in your Pine)
    - MTF: 15M, 1H, 4H for SSI
    - Entries: FearCap->HopeConf & Greed->IndecFear/FearCap + SSI + vol/range
    - Exits: NONE here (we use ATR SL/TP only – OANDA handles exits)
    """
    # 1) Fetch candles
    m15 = get_candles(instrument, "M15", 300)
    h1  = get_candles(instrument, "H1",  300)
    h4  = get_candles(instrument, "H4",  300)

    if len(m15) < 50 or len(h1) < 50 or len(h4) < 50:
        return MesSignal(instrument, "NONE", 0, 0, 0, 0, 0, 0.0,
                         "Not enough history for MES metrics")

    # 2) Metrics
    m15_metrics = compute_metrics(m15)
    h1_metrics  = compute_metrics(h1)
    h4_metrics  = compute_metrics(h4)

    # 3) Moods
    m15_moods = compute_moods(m15_metrics)
    h1_moods  = compute_moods(h1_metrics)
    h4_moods  = compute_moods(h4_metrics)

    # 4) SSI (uses HopeConf & FearCap only, like Pine)
    ssi_series = compute_ssi(
        m15_moods.hopeConf, m15_moods.fearCap,
        h1_moods.hopeConf,  h1_moods.fearCap,
        h4_moods.hopeConf,  h4_moods.fearCap,
    )

    ssi_last = float(ssi_series.iloc[-1])

    # 5) ATR (15M) for SL/TP & sizing
    atr_series = calc_atr(m15, ATR_LENGTH)
    atr_last   = float(atr_series.iloc[-1])
    if atr_last <= 0:
        return MesSignal(instrument, "NONE", 0, 0, 0, 0, 0, ssi_last,
                         "ATR <= 0 – invalid for sizing")

    atr_pips = pips_diff(instrument, atr_last)

    # 6) Pull last bar metrics for 15M
    volRs15_last = float(m15_metrics.volRs.iloc[-1])
    rngRs15_last = float(m15_metrics.rngRs.iloc[-1])

    # 7) Mood flags for last/previous bar (15M, 1H, 4H)
    fear15   = m15_moods.fearCap
    hope15   = m15_moods.hopeConf
    greed15  = m15_moods.greed
    indec15  = m15_moods.indecFear

    fear1h   = h1_moods.fearCap
    hope1h   = h1_moods.hopeConf
    greed1h  = h1_moods.greed
    fear4h   = h4_moods.fearCap
    hope4h   = h4_moods.hopeConf
    greed4h  = h4_moods.greed

    # previous bar: fill NaN with False
    fear15_prev  = fear15.shift(1).fillna(False)
    fear1h_prev  = fear1h.shift(1).fillna(False)
    fear4h_prev  = fear4h.shift(1).fillna(False)
    greed15_prev = greed15.shift(1).fillna(False)
    greed1h_prev = greed1h.shift(1).fillna(False)
    greed4h_prev = greed4h.shift(1).fillna(False)

    was_fear_cap = bool((fear15_prev | fear1h_prev | fear4h_prev).iloc[-1])
    was_greed    = bool((greed15_prev | greed1h_prev | greed4h_prev).iloc[-1])

    hope15_last  = bool(hope15.iloc[-1])
    fear15_last  = bool(fear15.iloc[-1])
    greed15_last = bool(greed15.iloc[-1])
    indec15_last = bool(indec15.iloc[-1])

    # 8) Entry signals (MES v2.0)
    # longSignal  = wasFearCap and hopeConf and ssi>=0.5 and volRs>=1.1 and rngRs>=1.1
    # shortSignal = wasGreed   and (indecFear or fearCap) and ssi<=-0.5 and volRs>=1.1 and rngRs>=1.1

    longSignal = (
        was_fear_cap and
        hope15_last and
        ssi_last >= 0.5 and
        volRs15_last >= 1.1 and
        rngRs15_last >= 1.1
    )

    shortSignal = (
        was_greed and
        (indec15_last or fear15_last) and
        ssi_last <= -0.5 and
        volRs15_last >= 1.1 and
        rngRs15_last >= 1.1
    )

    if not (longSignal or shortSignal):
        return MesSignal(
            instrument=instrument,
            side="NONE",
            entry_price=float(m15["close"].iloc[-1]),
            sl_price=0.0,
            tp_price=0.0,
            atr_pips=atr_pips,
            units=0,
            ssi=ssi_last,
            reason="No MES v2.0 entry (Fear/Hope/Greed/SSI conditions not met)"
        )

    # 9) Entry price
    entry = float(m15["close"].iloc[-1])

    # 10) SL/TP based on ATR & RR (ATR exits only)
    if longSignal:
        sl = entry - atr_last
        tp = entry + atr_last * RR_RATIO
        side = "BUY"
        base_reason = "MES Long: FearCap→HopeConf + SSI>=0.5 + vol/range filters"
    else:
        sl = entry + atr_last
        tp = entry - atr_last * RR_RATIO
        side = "SELL"
        base_reason = "MES Short: Greed→IndecFear/FearCap + SSI<=-0.5 + vol/range filters"

    sl = round_price(instrument, sl)
    tp = round_price(instrument, tp)
    entry_rounded = round_price(instrument, entry)

    # 11) Sizing (using OANDA NAV + MES sizing logic)
    pip = 0.01 if "JPY" in instrument else 0.0001
    risk_amt   = account_nav * (RISK_PCT / 100.0)
    units_risk = risk_amt / atr_last  # same as Pine: risk = units * ATR
    units_min  = MIN_DOLLAR_PIP / pip

    units_raw = max(units_risk, units_min)
    # keep some margin buffer: equity * 0.9 / price
    max_afford = account_nav * 0.9 / entry
    units_raw = min(units_raw, max_afford)

    units_clamped = int(round(max(1000, min(MAX_UNITS_DEMO, units_raw))))

    reason = f"{base_reason} | ATR={atr_pips:.1f}p, RISK%={RISK_PCT:.1f}, Units={units_clamped}"

    return MesSignal(
        instrument=instrument,
        side=side,
        entry_price=entry_rounded,
        sl_price=sl,
        tp_price=tp,
        atr_pips=atr_pips,
        units=units_clamped,
        ssi=ssi_last,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# BRIDGE + TELEGRAM
# ─────────────────────────────────────────────────────────────

def build_bridge_payload(sig: MesSignal) -> Dict:
    """
    Build JSON payload for Lambda OANDA Bridge matching MES v2.0 Pine alerts:
    {
      "message": "BUY"/"SELL",
      "instrument": "EUR_USD",
      "price": 1.23456,
      "sl": 1.22222,
      "tp": 1.25555,
      "qty": 10000
    }
    """
    return {
        "message": "BUY" if sig.side == "BUY" else "SELL",
        "instrument": sig.instrument,
        "price": sig.entry_price,
        "sl": sig.sl_price,
        "tp": sig.tp_price,
        "qty": sig.units,
    }


def send_to_bridge(payload: Dict) -> str:
    if not OANDA_BRIDGE_URL:
        return "OANDA_BRIDGE_URL not set – skipping bridge send."
    try:
        resp = requests.post(OANDA_BRIDGE_URL, json=payload, timeout=15)
        return f"Bridge {resp.status_code}: {resp.text}"
    except Exception as e:
        return f"Error sending to bridge: {e}"


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
# CLI COMMANDS – SCAN + AUTO
# ─────────────────────────────────────────────────────────────

def cmd_scan(args):
    """
    Scan mode:
    - Builds MES signals for all instruments
    - No trades placed
    - Optional Telegram summary
    """
    if not trading_window_open():
        msg = "Trading window CLOSED – scan only (no auto trades)."
        print(msg)
        if args.telegram:
            send_telegram(f"*MES v2 Scan*\n{msg}")

    try:
        nav = get_account_nav()
    except Exception as e:
        print(f"[ERROR] Failed to get NAV: {e}")
        nav = 0.0

    signals: List[MesSignal] = []
    for inst in INSTRUMENTS:
        try:
            sig = build_mes_signal(inst, nav if nav > 0 else 1000.0)
        except Exception as e:
            print(f"[ERROR] {inst}: {e}")
            continue
        signals.append(sig)

    print("─────────────── MES v2 SCAN RESULTS ───────────────")
    print(f"Account NAV (approx): {nav:.2f}" if nav > 0 else "Account NAV: (unavailable)")
    for sig in signals:
        print(
            f"{sig.instrument:8} {sig.side:4} "
            f"ATR:{sig.atr_pips:5.1f}p  "
            f"SSI:{sig.ssi:5.2f}  "
            f"Units:{sig.units:6}  "
            f"{sig.reason}"
        )

    if args.telegram:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"*MES v2 Scan {ts}*"]
        for sig in signals:
            if sig.side in ("BUY", "SELL"):
                lines.append(
                    f"`{sig.instrument}` {sig.side} "
                    f"ATR ~{sig.atr_pips:.1f}p, SSI {sig.ssi:.2f}, Units {sig.units}\n"
                    f"{sig.reason}"
                )
        if not any(s.side in ("BUY", "SELL") for s in signals):
            lines.append("_No MES entries right now._")
        send_telegram("\n".join(lines))


def cmd_auto(args):
    """
    Auto mode:
    - Enforces trading window
    - Enforces max 3 open positions
    - Avoids opening new trade on instrument that already has position
    - MES entries only (no SSI exits)
    - ATR SL/TP; OANDA handles completion
    """
    now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not trading_window_open():
        msg = f"MES v2 Auto {now_ts}: Trading window CLOSED, skipping."
        print(msg)
        if args.telegram:
            send_telegram(f"*MES v2 Auto*\n{msg}")
        return

    # Get NAV
    try:
        nav = get_account_nav()
    except Exception as e:
        msg = f"MES v2 Auto {now_ts}: Error getting NAV: {e}"
        print(msg)
        if args.telegram:
            send_telegram(f"*MES v2 Auto*\n{msg}")
        return

    # Get open positions
    try:
        open_count, open_instruments = get_open_positions()
    except Exception as e:
        msg = f"MES v2 Auto {now_ts}: Error getting open positions: {e}"
        print(msg)
        if args.telegram:
            send_telegram(f"*MES v2 Auto*\n{msg}")
        return

    if open_count >= 3:
        msg = f"MES v2 Auto {now_ts}: Max trades open ({open_count}) – no new trades."
        print(msg)
        if args.telegram:
            send_telegram(f"*MES v2 Auto*\n{msg}")
        return

    slots_left = max(0, 3 - open_count)

    signals: List[MesSignal] = []
    trades_sent: List[Tuple[MesSignal, str]] = []

    for inst in INSTRUMENTS:
        if slots_left <= 0:
            break

        if inst in open_instruments:
            print(f"[SKIP ] {inst}: already has open position.")
            continue

        try:
            sig = build_mes_signal(inst, nav)
        except Exception as e:
            print(f"[ERROR] {inst}: {e}")
            continue

        signals.append(sig)

        if sig.side in ("BUY", "SELL") and sig.units > 0:
            payload = build_bridge_payload(sig)
            result = send_to_bridge(payload)
            trades_sent.append((sig, result))
            slots_left -= 1
            print(f"[TRADE] {inst} {sig.side} Units={sig.units} → {result}")
        else:
            print(f"[SKIP ] {inst} {sig.side} – {sig.reason}")

    if args.telegram:
        lines = [f"*MES v2 Auto {now_ts}*"]
        lines.append(f"Account NAV: {nav:.2f}")
        lines.append(f"Open positions before run: {open_count}")

        if trades_sent:
            lines.append("*Trades sent:*")
            for sig, res in trades_sent:
                lines.append(
                    f"`{sig.instrument}` {sig.side} "
                    f"ATR ~{sig.atr_pips:.1f}p, SSI {sig.ssi:.2f}, Units {sig.units}\n"
                    f"{sig.reason}"
                )
        else:
            lines.append("_No MES entries placed this cycle._")

        send_telegram("\n".join(lines))


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    if not OANDA_API_KEY:
        print("OANDA_API_KEY not set – export it first.")
        sys.exit(1)
    if not OANDA_ACCOUNT_ID:
        print("OANDA_ACCOUNT_ID not set – export it first.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="MES v2.0 CLI Auto-Trader (OANDA-only, ATR exits only)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan MES signals and print them (no trades).")
    p_scan.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    p_scan.set_defaults(func=cmd_scan)

    p_auto = sub.add_parser("auto", help="Auto mode: MES entries + ATR SL/TP via OANDA Bridge.")
    p_auto.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    p_auto.set_defaults(func=cmd_auto)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
