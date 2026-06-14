"""
Qullamaggie Swing Trading Journal
CLI menu for managing open positions using Qullamaggie's exit rules.

Usage: python journal_app.py
"""

import json
import os
from datetime import datetime, date

import yfinance as yf
import pandas as pd

BASE           = os.path.dirname(__file__)
JOURNAL_FILE   = os.path.join(BASE, "journal.json")
WATCHLIST_FILE = os.path.join(BASE, "watchlist.txt")

MA_10_PERIOD = 10  # Qullamaggie trailing stop uses the 10-day MA


# ── persistence ───────────────────────────────────────────────────────────────

def load_journal():
    with open(JOURNAL_FILE) as f:
        return json.load(f)


def save_journal(data):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_watchlist():
    with open(WATCHLIST_FILE) as f:
        return [l.strip().upper() for l in f if l.strip()]


def save_watchlist(tickers):
    with open(WATCHLIST_FILE, "w") as f:
        f.write("\n".join(tickers) + "\n")


# ── price / indicator helpers ─────────────────────────────────────────────────

def get_current_data(ticker):
    """Returns (current_price, 10d_ma) or (None, None) on failure."""
    try:
        df = yf.download(ticker, period="30d", progress=False, auto_adjust=True)
        if df.empty:
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        closes = df["Close"].dropna()
        price  = float(closes.iloc[-1])
        ma10   = float(closes.iloc[-MA_10_PERIOD:].mean()) if len(closes) >= MA_10_PERIOD else None
        return price, ma10
    except Exception:
        return None, None


def days_held(entry_date_str):
    try:
        entry = date.fromisoformat(entry_date_str)
        return (date.today() - entry).days
    except Exception:
        return 0


# ── menu actions ──────────────────────────────────────────────────────────────

def show_open_trade():
    journal = load_journal()
    trade   = journal.get("open_trade")

    if not trade:
        print("\n  No open trade. Run scanner.py to find a breakout setup.")
        return

    ticker     = trade["ticker"]
    entry      = trade["entry_price"]
    stop       = trade["stop_price"]
    risk       = trade["risk"]
    t1, t2, t3 = trade.get("target_1r"), trade.get("target_2r"), trade.get("target_3r")
    held       = days_held(trade["entry_date"])

    print(f"\n{'='*56}")
    print(f"  OPEN TRADE — {ticker}")
    print(f"{'='*56}")
    print(f"  Entry:      ${entry:.2f}  ({trade['entry_date']}, day {held})")
    print(f"  Stop loss:  ${stop:.2f}  (risk: ${risk:.2f}/share)")
    if t1: print(f"  1R target:  ${t1:.2f}")
    if t2: print(f"  2R target:  ${t2:.2f}")
    if t3: print(f"  3R target:  ${t3:.2f}")

    print(f"\n  Fetching live data...")
    price, ma10 = get_current_data(ticker)

    if price is None:
        print("  (Could not fetch live price — check internet connection)")
    else:
        chg = (price - entry) / entry * 100
        print(f"\n  Current price: ${price:.2f}  ({chg:+.2f}% from entry)")
        if ma10:
            print(f"  10-day MA:     ${ma10:.2f}")

        # ── Qullamaggie exit rules ────────────────────────────────────────────
        print()

        # Rule 1: day 3-5 partial profit reminder
        if 3 <= held <= 5:
            print(f"  *** DAY {held} REMINDER ***")
            print(f"  Qullamaggie rule: consider selling 1/3 to 1/2 of your position now")
            print(f"  and moving your stop to breakeven (${entry:.2f}) on the rest.")
            print()

        # Rule 2: 10-day MA trailing stop
        if ma10:
            if price < ma10:
                print(f"  *** FULL EXIT SIGNAL ***")
                print(f"  Price (${price:.2f}) closed below the 10-day MA (${ma10:.2f}).")
                print(f"  Qullamaggie trailing stop rule: EXIT the remaining position.")
            else:
                gap = price - ma10
                print(f"  HOLD — still above 10-day MA by ${gap:.2f}.")
                print(f"  Trail your stop up to the 10-day MA (${ma10:.2f}) if it's higher than your current stop.")

    print(f"{'='*56}")


def close_week():
    journal = load_journal()
    trade   = journal.get("open_trade")

    if not trade:
        print("\n  No open trade to close.")
        return

    ticker = trade["ticker"]
    entry  = trade["entry_price"]
    risk   = trade["risk"]

    print(f"\n  Closing {ticker}  (entry: ${entry:.2f}  risk: ${risk:.2f}/share)")

    while True:
        try:
            exit_price = float(input("  Exit price: $").strip())
            if exit_price > 0:
                break
        except ValueError:
            pass
        print("  Enter a valid price (e.g. 145.50)")

    ret    = (exit_price - entry) / entry * 100
    r_mult = (exit_price - entry) / risk if risk else 0
    result = "WIN" if ret > 0 else "LOSS"

    closed = {
        "ticker":      ticker,
        "entry_price": entry,
        "entry_date":  trade["entry_date"],
        "exit_price":  exit_price,
        "exit_date":   datetime.today().strftime("%Y-%m-%d"),
        "return_pct":  round(ret, 3),
        "r_multiple":  round(r_mult, 2),
        "result":      result,
        "adr":         trade.get("adr"),
        "momentum":    trade.get("momentum"),
    }

    journal["closed_trades"].append(closed)
    journal["open_trade"] = None
    save_journal(journal)

    print(f"\n  Closed: {ticker}  {ret:+.2f}%  [{r_mult:+.2f}R]  [{result}]")
    print(f"  Saved to journal.")


def view_stats():
    journal = load_journal()
    trades  = journal.get("closed_trades", [])

    print(f"\n{'='*56}")
    print(f"  ALL-TIME STATS")
    print(f"{'='*56}")

    if not trades:
        print("  No closed trades yet.")
        print(f"{'='*56}")
        return

    returns   = [t["return_pct"] for t in trades]
    r_mults   = [t.get("r_multiple", 0) for t in trades]
    wins      = [r for r in returns if r > 0]
    losses    = [r for r in returns if r <= 0]
    win_rate  = len(wins) / len(returns) * 100
    avg_ret   = sum(returns) / len(returns)
    avg_r     = sum(r_mults) / len(r_mults)
    best      = max(returns)
    worst     = min(returns)

    best_t  = next(t for t in trades if t["return_pct"] == best)
    worst_t = next(t for t in trades if t["return_pct"] == worst)

    print(f"  Total trades:   {len(trades)}")
    print(f"  Win rate:       {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg return:     {avg_ret:+.2f}%")
    print(f"  Avg R-multiple: {avg_r:+.2f}R")
    print(f"  Best trade:     {best_t['ticker']} {best:+.2f}%  ({best_t.get('exit_date','')})")
    print(f"  Worst trade:    {worst_t['ticker']} {worst:+.2f}%  ({worst_t.get('exit_date','')})")

    print(f"\n  {'Date':<12} {'Ticker':<8} {'Entry':>8} {'Exit':>8} {'Ret%':>7}  {'R':>6}  Result")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*7}  {'-'*6}  {'-'*6}")
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        print(f"  {t['exit_date']:<12} {t['ticker']:<8} "
              f"${t['entry_price']:>7.2f} ${t['exit_price']:>7.2f} "
              f"{t['return_pct']:>+6.2f}%  {t.get('r_multiple',0):>+5.2f}R  {t['result']}")

    print(f"{'='*56}")


def add_ticker():
    tickers = load_watchlist()
    raw     = input("\n  Ticker to add: ").strip().upper()
    if not raw:
        print("  No ticker entered.")
        return
    if raw in tickers:
        print(f"  {raw} is already in watchlist.")
        return
    tickers.append(raw)
    save_watchlist(tickers)
    print(f"  {raw} added. ({len(tickers)} total)")


def print_menu():
    print(f"\n{'='*56}")
    print(f"  QULLAMAGGIE SWING TRADING JOURNAL")
    print(f"{'='*56}")
    print(f"  1. Check open position & exit signals")
    print(f"  2. Close position (enter exit price)")
    print(f"  3. View all-time stats")
    print(f"  4. Add ticker to watchlist")
    print(f"  5. Quit")
    print(f"{'='*56}")


def main():
    while True:
        print_menu()
        choice = input("  Choose (1-5): ").strip()
        if choice == "1":
            show_open_trade()
        elif choice == "2":
            close_week()
        elif choice == "3":
            view_stats()
        elif choice == "4":
            add_ticker()
        elif choice == "5":
            print("\n  Trade well.\n")
            break
        else:
            print("  Enter 1-5.")


if __name__ == "__main__":
    main()
