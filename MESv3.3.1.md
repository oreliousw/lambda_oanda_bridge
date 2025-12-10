                        ┌──────────────────────┐
                        │  START CYCLE (M15)   │
                        └──────────┬───────────┘
                                   │
                     For each instrument (EUR, GBP, AUD...)
                                   │
                 ┌─────────────────┴──────────────────┐
                 │                                    │
        Check if existing open position?       NO open position
                 │                                    │
          ┌──────┴──────┐                              ▼
          │  SKIP PAIR   │                   ┌────────────────────┐
          └──────────────┘                   │ FETCH ALL CANDLES  │
                                             │  15m, 1h, 4h (300)  │
                                             └──────────┬─────────┘
                                                        │
                                      ┌─────────────────┴──────────────────┐
                                      │   CALCULATE INDICATORS             │
                                      │   (RSI, MACD, ATR)                 │
                                      └──────────┬─────────────────────────┘
                                                 │
                         ┌───────────────────────┴─────────────────────────────┐
                         │       HIGHER TIMEFRAME FILTERS (1H + 4H)            │
                         │                                                     │
                         │  - Is 1H RSI bullish/bearish?                       │
                         │  - Is 4H NOT strongly opposite?                     │
                         └──────────┬──────────────────────────────────────────┘
                                    │
                   If NO HTF alignment →        If YES, direction identified →
                              SKIP                                CONTINUE
                                    │                                 │
                                    ▼                                 ▼
                              ┌──────────┐                ┌────────────────────┐
                              │  SKIP    │                │ STRUCTURE ANALYSIS │
                              └─────┬────┘                └────────────────────┘
                                    │                                 │
                              (Reason printed)        Use last 6 bars (1H):
                                                        - Breakout?
                                                        - Pullback?
                                                        - Continuation?
                                                        - None?
                                                                       │
                             If NONE →───────────────► SKIP
                                                                       │
                                                                       ▼
                                                ┌────────────────────────────┐
                                                │  MACD MOMENTUM CHECK       │
                                                │  (is separation meaningful?)│
                                                └─────────────┬──────────────┘
                                                              │
                                          If weak momentum → SKIP
                                                              │
                                                              ▼
                                 ┌──────────────────────────────────────────┐
                                 │ ATR TREND → DETERMINE VOLATILITY MODE   │
                                 │   - ATR rising? → NORMAL MODE           │
                                 │   - ATR falling? → LOW MODE             │
                                 │                                          │
                                 │   Mode sets TP/SL:                       │
                                 │      LOW   → TP 8 pips, SL 12 pips       │
                                 │      NORMAL→ TP 20 pips, SL 30 pips      │
                                 └───────────────┬──────────────────────────┘
                                                 │
                                                 ▼
                                 ┌───────────────────────────────────────────┐
                                 │ POSITION SIZING (risk-based)              │
                                 │ units = NAV * risk / SL_distance          │
                                 └──────────────────┬────────────────────────┘
                                                    │
                                    If units == 0 → SKIP (SL too tight)
                                                    │
                                                    ▼
                               ┌─────────────────────────────────────────────┐
                               │           PLACE MARKET ORDER                │
                               │   BUY if bullish / SELL if bearish          │
                               └──────────────────┬──────────────────────────┘
                                                  │
                                                  ▼
                                          ┌─────────────┐
                                          │  FINISHED!  │
                                          └─────────────┘
