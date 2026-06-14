"""
Weekly Momentum Journal
CLI menu for managing your weekly momentum trades.
Usage: python journal_app.py
"""

import json
import os
from datetime import datetime

JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "journal.json")
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.txt")

TAKE_PROFIT_PCT = 6.0
STOP_LOSS_PCT = -3.0


def load_journal():
    with open(JOURNAL_FILE) as f:
        return json.load(f)


def save_journal(data):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_watchlist():
    with open(WATCHLIST_FILE) as f:
        return [line.strip().upper() for line in f if line.strip()]


def save_watchlist(tickers):
    with open(WATCHLIST_FILE, "w") as f:
        for t in tickers:
            f.write(t + "\n")


def pct_change(entry, current):
    return (current - entry) / entry * 100


def check_alerts(trade):
    """Check for take-profit or stop-loss conditions using live price if available."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(trade["ticker"])
        hist = ticker.history(period="1d")
        if hist.empty:
            return None, None
        current = float(hist["Close"].iloc[-1])
    except Exception:
        return None, None

    chg = pct_change(trade["entry_price"], current)
    return current, chg


def show_open_trade():
    journal = load_journal()
    trade = journal.get("open_trade")

    if not trade:
        print("\n  No open trade this week. Run scanner.py on Monday to get a pick.")
        return

    print(f"\n{'='*50}")
    print(f"  OPEN TRADE")
    print(f"{'='*50}")
    print(f"  Ticker:       {trade['ticker']}")
    print(f"  Entry Date:   {trade['entry_date']}")
    print(f"  Entry Price:  ${trade['entry_price']:.2f}")
    print(f"  Target (+6%): ${trade['target_price']:.2f}")
    print(f"  Stop  (-3%):  ${trade['stop_price']:.2f}")
    print(f"  RS Score:     {trade.get('rs_score', 'N/A')}")

    print(f"\n  Fetching live price...")
    current, chg = check_alerts(trade)

    if current is not None:
        print(f"  Current Price: ${current:.2f}  ({chg:+.2f}%)")
        if chg >= TAKE_PROFIT_PCT:
            print(f"\n  *** SELL ALERT: Position is up {chg:.2f}% — take profit! ***")
        elif chg <= STOP_LOSS_PCT:
            print(f"\n  *** STOP LOSS WARNING: Position is down {chg:.2f}% — consider cutting! ***")
        else:
            print(f"  Status: Holding ({chg:+.2f}% from entry)")
    else:
        print("  (Could not fetch live price — check internet connection)")

    print(f"{'='*50}")


def close_week():
    journal = load_journal()
    trade = journal.get("open_trade")

    if not trade:
        print("\n  No open trade to close.")
        return

    print(f"\n  Closing trade for {trade['ticker']} (entry: ${trade['entry_price']:.2f})")

    while True:
        try:
            raw = input("  Enter your exit price: $").strip()
            exit_price = float(raw)
            if exit_price <= 0:
                raise ValueError
            break
        except ValueError:
            print("  Please enter a valid price (e.g. 145.50)")

    ret = pct_change(trade["entry_price"], exit_price)
    result = "WIN" if ret > 0 else "LOSS"

    closed = {
        "ticker": trade["ticker"],
        "entry_price": trade["entry_price"],
        "entry_date": trade["entry_date"],
        "exit_price": exit_price,
        "exit_date": datetime.today().strftime("%Y-%m-%d"),
        "return_pct": round(ret, 3),
        "result": result,
        "rs_score": trade.get("rs_score"),
    }

    journal["closed_trades"].append(closed)
    journal["open_trade"] = None
    save_journal(journal)

    print(f"\n  Trade closed: {trade['ticker']}  {ret:+.2f}%  [{result}]")
    print(f"  Saved to journal.")


def view_stats():
    journal = load_journal()
    trades = journal.get("closed_trades", [])

    print(f"\n{'='*50}")
    print(f"  ALL-TIME STATS")
    print(f"{'='*50}")

    if not trades:
        print("  No closed trades yet.")
        print(f"{'='*50}")
        return

    returns = [t["return_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    win_rate = len(wins) / len(returns) * 100
    avg_ret = sum(returns) / len(returns)
    best = max(returns)
    worst = min(returns)

    best_trade = next(t for t in trades if t["return_pct"] == best)
    worst_trade = next(t for t in trades if t["return_pct"] == worst)

    print(f"  Total Trades:    {len(trades)}")
    print(f"  Win Rate:        {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg Weekly Ret:  {avg_ret:+.2f}%")
    print(f"  Best Trade:      {best_trade['ticker']} {best:+.2f}%  ({best_trade['exit_date']})")
    print(f"  Worst Trade:     {worst_trade['ticker']} {worst:+.2f}%  ({worst_trade['exit_date']})")

    print(f"\n  {'Date':<12} {'Ticker':<8} {'Entry':>8} {'Exit':>8} {'Return':>8}  Result")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8}  {'-'*6}")
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        print(f"  {t['exit_date']:<12} {t['ticker']:<8} "
              f"${t['entry_price']:>7.2f} ${t['exit_price']:>7.2f} "
              f"{t['return_pct']:>+7.2f}%  {t['result']}")

    print(f"{'='*50}")


def add_ticker():
    tickers = load_watchlist()
    raw = input("\n  Enter ticker symbol to add: ").strip().upper()
    if not raw:
        print("  No ticker entered.")
        return
    if raw in tickers:
        print(f"  {raw} is already in your watchlist.")
        return
    tickers.append(raw)
    save_watchlist(tickers)
    print(f"  {raw} added to watchlist. ({len(tickers)} total tickers)")


def print_menu():
    print(f"\n{'='*50}")
    print(f"  WEEKLY MOMENTUM JOURNAL")
    print(f"{'='*50}")
    print(f"  1. See this week's open trade")
    print(f"  2. Close the week (enter exit price)")
    print(f"  3. View all-time stats")
    print(f"  4. Add a ticker to watchlist")
    print(f"  5. Quit")
    print(f"{'='*50}")


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
            print("\n  Bye. Trade well.\n")
            break
        else:
            print("  Invalid choice. Enter 1-5.")


if __name__ == "__main__":
    main()
