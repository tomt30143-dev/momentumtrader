"""
Qullamaggie Swing Trading Scanner
Scans watchlist.txt for VCP (Volatility Contraction Pattern) setups and breakouts.

Usage:
  python scanner.py              # scan watchlist.txt
  python scanner.py --top 10    # show top N candidates (default: all that pass)
"""

import json
import os
import sys
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd

BASE           = os.path.dirname(__file__)
WATCHLIST_FILE = os.path.join(BASE, "watchlist.txt")
JOURNAL_FILE   = os.path.join(BASE, "journal.json")

# ── filter thresholds ─────────────────────────────────────────────────────────
ADR_MIN_PCT          = 3.0    # minimum average daily range %
MOMENTUM_MIN_PCT     = 30.0   # minimum prior 60-day low-to-high move %
CONSOL_MAX_PCT       = 10.0   # maximum consolidation range % (VCP tightness)
BREAKOUT_VOL_MULT    = 1.5    # today's volume must be >= this * 20d avg volume
CONSOL_DAYS          = 10     # days to look back for consolidation window
MOMENTUM_DAYS        = 60     # days to look back for prior momentum move
ADR_DAYS             = 20     # days for average daily range calculation
MA_PERIOD            = 50     # trend filter moving average


# ── helpers ───────────────────────────────────────────────────────────────────

def load_watchlist():
    with open(WATCHLIST_FILE) as f:
        return [l.strip().upper() for l in f if l.strip()]


def load_journal():
    with open(JOURNAL_FILE) as f:
        return json.load(f)


def save_journal(data):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)


def fetch(ticker, days=120):
    end   = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        return None
    # flatten MultiIndex if present (yfinance sometimes returns one for single ticker)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def adr_pct(df, n=ADR_DAYS):
    """Average Daily Range % over last n days."""
    recent = df.tail(n)
    daily_range = (recent["High"] - recent["Low"]) / recent["Close"] * 100
    return float(daily_range.mean())


def prior_momentum(df, n=MOMENTUM_DAYS):
    """Low-to-high % move over the last n trading days."""
    window = df.tail(n)
    lo  = float(window["Low"].min())
    hi  = float(window["High"].max())
    if lo == 0:
        return 0.0
    return (hi - lo) / lo * 100


def above_50ma(df):
    """True if latest close is above the 50-day SMA."""
    closes = df["Close"].dropna()
    if len(closes) < MA_PERIOD:
        return False
    ma = float(closes.iloc[-MA_PERIOD:].mean())
    return float(closes.iloc[-1]) > ma


def consolidation(df, n=CONSOL_DAYS):
    """
    Returns:
      range_pct   — (highest high - lowest low) / current price * 100 over last n days
      vol_dry     — True if avg volume last n days < avg volume last 20 days
    """
    window   = df.tail(n)
    full_vol = df.tail(ADR_DAYS)

    hi        = float(window["High"].max())
    lo        = float(window["Low"].min())
    price     = float(df["Close"].iloc[-1])
    range_pct = (hi - lo) / price * 100

    avg_vol_consol = float(window["Volume"].mean())
    avg_vol_20d    = float(full_vol["Volume"].mean())
    vol_dry        = avg_vol_consol < avg_vol_20d

    return range_pct, vol_dry, lo  # lo = suggested stop


def is_breakout(df, n=CONSOL_DAYS):
    """
    True if today's close is above the highest high of the prior n days
    AND today's volume >= BREAKOUT_VOL_MULT * 20-day avg volume.
    'Prior n days' excludes today.
    """
    if len(df) < n + 2:
        return False

    today        = df.iloc[-1]
    prior_window = df.iloc[-(n + 1):-1]  # n days before today

    prior_high   = float(prior_window["High"].max())
    today_close  = float(today["Close"])
    today_vol    = float(today["Volume"])
    avg_vol_20d  = float(df.tail(ADR_DAYS + 1).iloc[:-1]["Volume"].mean())

    broke_out    = today_close > prior_high
    vol_confirm  = today_vol >= BREAKOUT_VOL_MULT * avg_vol_20d
    return broke_out and vol_confirm


# ── scanner ───────────────────────────────────────────────────────────────────

def scan_ticker(ticker):
    df = fetch(ticker)
    if df is None or len(df) < MA_PERIOD + CONSOL_DAYS + 5:
        return None, f"insufficient data"

    # 1. ADR filter
    adr = adr_pct(df)
    if adr < ADR_MIN_PCT:
        return None, f"ADR {adr:.1f}% < {ADR_MIN_PCT}% — doesn't move enough"

    # 2. Prior momentum filter
    mom = prior_momentum(df)
    if mom < MOMENTUM_MIN_PCT:
        return None, f"prior move {mom:.1f}% < {MOMENTUM_MIN_PCT}% — no institutional momentum"

    # 3. Trend filter (above 50 MA)
    if not above_50ma(df):
        return None, f"below 50-day MA — hard reject"

    # 4. Consolidation / VCP
    range_pct, vol_dry, consol_low = consolidation(df)
    if range_pct > CONSOL_MAX_PCT:
        return None, f"consolidation range {range_pct:.1f}% > {CONSOL_MAX_PCT}% — not tight enough"

    # 5. Breakout check
    breakout = is_breakout(df)

    price      = float(df["Close"].iloc[-1])
    stop       = round(consol_low, 2)
    risk       = round(price - stop, 2)
    target_1r  = round(price + risk, 2)
    target_2r  = round(price + 2 * risk, 2)
    target_3r  = round(price + 3 * risk, 2)

    return {
        "ticker":       ticker,
        "price":        price,
        "adr":          round(adr, 2),
        "momentum":     round(mom, 1),
        "range_pct":    round(range_pct, 2),
        "vol_dry":      vol_dry,
        "breakout":     breakout,
        "stop":         stop,
        "risk":         risk,
        "target_1r":    target_1r,
        "target_2r":    target_2r,
        "target_3r":    target_3r,
    }, None


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args  = sys.argv[1:]
    top_n = None
    if "--top" in args:
        idx = args.index("--top")
        try:
            top_n = int(args[idx + 1])
        except (IndexError, ValueError):
            pass

    print("\n=== Qullamaggie Swing Trading Scanner ===")
    print(f"Date: {datetime.today().strftime('%A, %B %d %Y')}\n")

    tickers   = load_watchlist()
    passing   = []
    rejected  = []

    print(f"Scanning {len(tickers)} tickers...\n")

    for ticker in tickers:
        result, reason = scan_ticker(ticker)
        if result:
            passing.append(result)
        else:
            rejected.append((ticker, reason))

    # sort: breakouts first, then by tightest consolidation range
    passing.sort(key=lambda x: (not x["breakout"], x["range_pct"]))

    if top_n:
        passing = passing[:top_n]

    # ── print rejected ────────────────────────────────────────────────────────
    if rejected:
        print("FILTERED OUT:")
        for ticker, reason in rejected:
            print(f"  {ticker:<8} — {reason}")
        print()

    # ── print results ─────────────────────────────────────────────────────────
    if not passing:
        print("No stocks passed all filters this scan.")
        print("Tip: add more tickers to watchlist.txt or check back after a trending week.")
        return

    print(f"{'='*70}")
    print(f"  PASSING SETUPS  ({len(passing)} stocks)")
    print(f"{'='*70}")

    breakouts   = [s for s in passing if s["breakout"]]
    on_watch    = [s for s in passing if not s["breakout"]]

    def print_stock(s, label):
        print(f"\n  [{label}]  {s['ticker']}  @ ${s['price']:.2f}")
        print(f"  ADR: {s['adr']:.1f}%   Prior move: {s['momentum']:.1f}%   Consol range: {s['range_pct']:.1f}%   Vol drying up: {'Yes' if s['vol_dry'] else 'No'}")
        print(f"  Stop loss:  ${s['stop']:.2f}  (lowest low of consolidation)")
        print(f"  Risk/share: ${s['risk']:.2f}")
        print(f"  Targets:    1R=${s['target_1r']:.2f}   2R=${s['target_2r']:.2f}   3R=${s['target_3r']:.2f}")

    for s in breakouts:
        print_stock(s, "*** BREAKOUT — BUY SIGNAL ***")

    for s in on_watch:
        print_stock(s, "On Watch — Consolidating")

    print(f"\n{'='*70}\n")

    # ── auto-save breakout to journal ─────────────────────────────────────────
    if breakouts:
        journal = load_journal()
        if journal.get("open_trade"):
            existing = journal["open_trade"]
            print(f"NOTE: Open trade exists ({existing['ticker']} @ ${existing['entry_price']:.2f}). Close it before opening a new one.")
            print(f"Top breakout this scan: {breakouts[0]['ticker']} @ ${breakouts[0]['price']:.2f}")
        else:
            best = breakouts[0]
            trade = {
                "ticker":      best["ticker"],
                "entry_price": best["price"],
                "entry_date":  datetime.today().strftime("%Y-%m-%d"),
                "stop_price":  best["stop"],
                "risk":        best["risk"],
                "target_1r":   best["target_1r"],
                "target_2r":   best["target_2r"],
                "target_3r":   best["target_3r"],
                "adr":         best["adr"],
                "momentum":    best["momentum"],
            }
            journal["open_trade"] = trade
            save_journal(journal)
            print(f"*** TRADE OPENED: {trade['ticker']} @ ${trade['entry_price']:.2f} ***")
            print(f"  Stop:    ${trade['stop_price']:.2f}  (risk: ${trade['risk']:.2f}/share)")
            print(f"  1R target: ${trade['target_1r']:.2f}")
            print(f"  2R target: ${trade['target_2r']:.2f}")
            print(f"  3R target: ${trade['target_3r']:.2f}")
            print(f"\nSaved to journal.json.")
    else:
        print("No confirmed breakouts today. Stocks on watch are setting up — check back daily.")


if __name__ == "__main__":
    main()
