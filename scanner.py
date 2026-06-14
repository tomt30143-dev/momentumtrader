"""
Weekly Momentum Scanner
Run every Monday to find the top momentum pick for the week.

Usage:
  python scanner.py              # scan all ~6000 US-listed stocks
  python scanner.py --watchlist  # scan watchlist.txt only (fast)
  python scanner.py --top 10     # show top N picks (default 5)
"""

import json
import os
import sys
import io
from datetime import datetime, timedelta

import requests
import pandas as pd
import yfinance as yf

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.txt")
JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "journal.json")

TOP_N = 5
LOOKBACK_WEEKS = 4
MA_PERIOD = 50
BATCH_SIZE = 100  # tickers per yfinance batch download

# NASDAQ public symbol directory — all US exchange-listed stocks
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL  = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"


# ── data helpers ──────────────────────────────────────────────────────────────

def load_watchlist():
    with open(WATCHLIST_FILE) as f:
        return [line.strip().upper() for line in f if line.strip()]


def load_journal():
    with open(JOURNAL_FILE) as f:
        return json.load(f)


def save_journal(data):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)


def fetch_all_us_tickers():
    """Download the full NASDAQ + other-exchange listing files and return clean tickers."""
    tickers = set()

    for url, name in [(NASDAQ_LISTED_URL, "NASDAQ"), (OTHER_LISTED_URL, "Other exchanges")]:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text), sep="|")
            # last row is a file-creation timestamp line — drop it
            df = df[:-1]
            sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            # filter out test symbols, ETNs, warrants, units, rights
            raw = df[sym_col].dropna().astype(str)
            clean = raw[
                ~raw.str.contains(r"[^A-Z]", regex=True)  # only pure alpha tickers
            ]
            tickers.update(clean.tolist())
            print(f"  {name}: {len(clean)} tickers loaded")
        except Exception as e:
            print(f"  WARNING: could not fetch {name} list ({e})")

    return sorted(tickers)


# ── price helpers ─────────────────────────────────────────────────────────────

def get_close(df, ticker=None):
    """Return Close column as a flat Series for single or multi-ticker DataFrames."""
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        if ticker and ticker in close.columns:
            return close[ticker].dropna()
        return close.iloc[:, 0].dropna()
    if hasattr(close, "squeeze"):
        close = close.squeeze()
    return close.dropna()


def pct_return(series):
    s = series.dropna()
    if len(s) < 2:
        return None
    return float((s.iloc[-1] - s.iloc[0]) / s.iloc[0] * 100)


def above_50ma(series):
    s = series.dropna()
    if len(s) < MA_PERIOD:
        return False
    ma50 = float(s.iloc[-MA_PERIOD:].mean())
    return float(s.iloc[-1]) > ma50


# ── batch scanning ────────────────────────────────────────────────────────────

def scan_batch(tickers, period_days):
    """
    Download a batch of tickers in one yfinance call.
    Returns dict: ticker -> {"price", "return", "rs"} or None on failure.
    """
    end   = datetime.today()
    start = end - timedelta(days=period_days)
    start_str = start.strftime("%Y-%m-%d")
    end_str   = end.strftime("%Y-%m-%d")

    try:
        df = yf.download(
            tickers,
            start=start_str,
            end=end_str,
            progress=False,
            auto_adjust=True,
            threads=True,
        )
    except Exception:
        return {}

    if df.empty:
        return {}

    results = {}
    lookback_rows = LOOKBACK_WEEKS * 5  # approx trading days

    for ticker in tickers:
        try:
            close = get_close(df, ticker)
            if len(close) < max(MA_PERIOD, lookback_rows + 1):
                continue
            recent = close.iloc[-lookback_rows:]
            ret = pct_return(recent)
            if ret is None:
                continue
            if not above_50ma(close):
                continue
            price = float(close.iloc[-1])
            results[ticker] = {"price": price, "return": ret}
        except Exception:
            continue

    return results


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    use_watchlist = "--watchlist" in args
    top_n = TOP_N
    if "--top" in args:
        idx = args.index("--top")
        try:
            top_n = int(args[idx + 1])
        except (IndexError, ValueError):
            pass

    print("\n=== Weekly Momentum Scanner ===")
    print(f"Date: {datetime.today().strftime('%A, %B %d %Y')}\n")

    # ── tickers ───────────────────────────────────────────────────────────────
    if use_watchlist:
        tickers = load_watchlist()
        print(f"Mode: watchlist ({len(tickers)} tickers)\n")
    else:
        print("Mode: full US market — fetching ticker universe...")
        tickers = fetch_all_us_tickers()
        # always include watchlist tickers
        wl = load_watchlist()
        tickers = sorted(set(tickers) | set(wl))
        print(f"  Total tickers to scan: {len(tickers)}\n")

    period_days = LOOKBACK_WEEKS * 7 + 90  # enough calendar days for 50 trading days

    # ── SPY benchmark ─────────────────────────────────────────────────────────
    print("Fetching SPY benchmark...")
    spy_batch = scan_batch(["SPY"], period_days)
    if "SPY" not in spy_batch:
        # fallback: direct fetch
        end = datetime.today()
        spy_df = yf.download("SPY", start=(end - timedelta(days=period_days)).strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        spy_close = get_close(spy_df)
        spy_ret = pct_return(spy_close.iloc[-(LOOKBACK_WEEKS * 5):])
    else:
        spy_ret = spy_batch["SPY"]["return"]

    if spy_ret is None:
        print("ERROR: Could not fetch SPY data. Check your internet connection.")
        return

    print(f"SPY 4-week return: {spy_ret:+.2f}%\n")

    # ── scan in batches ───────────────────────────────────────────────────────
    non_spy = [t for t in tickers if t != "SPY"]
    batches = [non_spy[i:i + BATCH_SIZE] for i in range(0, len(non_spy), BATCH_SIZE)]
    total_batches = len(batches)

    qualifying = []
    filtered_count = 0
    error_count = 0

    print(f"Scanning {len(non_spy)} tickers in {total_batches} batches of {BATCH_SIZE}...")
    print("(Tickers below 50-day MA are silently filtered)\n")

    for i, batch in enumerate(batches, 1):
        print(f"  Batch {i}/{total_batches}  ({(i-1)*BATCH_SIZE}–{min(i*BATCH_SIZE, len(non_spy))} of {len(non_spy)})...", end="\r")
        results = scan_batch(batch, period_days)

        for ticker, data in results.items():
            rs = data["return"] - spy_ret
            qualifying.append({
                "ticker": ticker,
                "price": data["price"],
                "return": data["return"],
                "rs": rs,
            })

        filtered_count += len(batch) - len(results)

    print(f"\n\nScan complete.")
    print(f"  Qualifying (above 50MA): {len(qualifying)}")
    print(f"  Filtered out (below 50MA or no data): {filtered_count}")

    if not qualifying:
        print("\nNo qualifying stocks found. Try running on a trading day.")
        return

    ranked = sorted(qualifying, key=lambda x: x["rs"], reverse=True)
    top = ranked[:top_n]

    print(f"\n{'='*58}")
    print(f"  TOP {top_n} MOMENTUM PICKS  (ranked by RS vs SPY)")
    print(f"{'='*58}")
    print(f"  {'Rank':<5} {'Ticker':<8} {'Price':>9}  {'4wk Ret':>9}  {'RS Score':>9}")
    print(f"  {'-'*5} {'-'*8} {'-'*9}  {'-'*9}  {'-'*9}")
    for i, s in enumerate(top, 1):
        print(f"  #{i:<4} {s['ticker']:<8} ${s['price']:>8.2f}  {s['return']:>+8.2f}%  {s['rs']:>+8.2f}%")
    print(f"{'='*58}\n")

    best = top[0]
    journal = load_journal()

    if journal.get("open_trade"):
        existing = journal["open_trade"]
        print(f"NOTE: Open trade exists: {existing['ticker']} @ ${existing['entry_price']:.2f}")
        print(f"Close it in journal_app.py before opening a new one.")
        print(f"\nTop pick this week: {best['ticker']} @ ${best['price']:.2f}  RS: {best['rs']:+.2f}%")
    else:
        trade = {
            "ticker": best["ticker"],
            "entry_price": best["price"],
            "entry_date": datetime.today().strftime("%Y-%m-%d"),
            "target_price": round(best["price"] * 1.06, 2),
            "stop_price": round(best["price"] * 0.97, 2),
            "rs_score": round(best["rs"], 2),
        }
        journal["open_trade"] = trade
        save_journal(journal)
        print(f"*** TRADE OPENED ***")
        print(f"  Ticker:       {trade['ticker']}")
        print(f"  Entry Price:  ${trade['entry_price']:.2f}")
        print(f"  Target (+6%): ${trade['target_price']:.2f}")
        print(f"  Stop  (-3%):  ${trade['stop_price']:.2f}")
        print(f"  RS Score:     {trade['rs_score']:+.2f}%")
        print(f"\nTrade saved to journal.json.")


if __name__ == "__main__":
    main()
