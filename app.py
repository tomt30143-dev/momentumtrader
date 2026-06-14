"""
Weekly Momentum Trader — Streamlit Web App
Launch: streamlit run app.py
"""

import io
import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

# ── paths ─────────────────────────────────────────────────────────────────────
BASE = os.path.dirname(__file__)
JOURNAL_FILE   = os.path.join(BASE, "journal.json")
WATCHLIST_FILE = os.path.join(BASE, "watchlist.txt")

# ── Supabase helpers (used in cloud; falls back to local file when no secrets) ─
def _supabase_headers():
    key = st.secrets["SUPABASE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def _supabase_url():
    return st.secrets["SUPABASE_URL"].rstrip("/") + "/rest/v1/journal?id=eq.1"

def _has_supabase():
    try:
        return "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets
    except Exception:
        return False

# ── constants ─────────────────────────────────────────────────────────────────
LOOKBACK_WEEKS  = 4
MA_PERIOD       = 50
BATCH_SIZE      = 100
TAKE_PROFIT_PCT = 6.0
STOP_LOSS_PCT   = -3.0

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_URL  = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

SECTORS = [
    "Technology", "Healthcare", "Financial Services", "Consumer Cyclical",
    "Industrials", "Communication Services", "Consumer Defensive",
    "Energy", "Basic Materials", "Real Estate", "Utilities",
]

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Momentum Trader",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  [data-testid="stSidebar"] { background: #0f1117; }
  .metric-card {
    background: #1c1e26; border-radius: 12px; padding: 20px 24px;
    border: 1px solid #2a2d3a;
  }
  .metric-card h3 { margin: 0 0 4px 0; font-size: 13px; color: #8b8fa8; letter-spacing: .06em; text-transform: uppercase; }
  .metric-card p  { margin: 0; font-size: 28px; font-weight: 700; }
  .sell-alert  { background:#1a3a1a; border:1px solid #2ecc71; border-radius:10px; padding:14px 18px; }
  .stop-alert  { background:#3a1a1a; border:1px solid #e74c3c; border-radius:10px; padding:14px 18px; }
  .info-alert  { background:#1a2a3a; border:1px solid #3498db; border-radius:10px; padding:14px 18px; }
  .green { color: #2ecc71; }
  .red   { color: #e74c3c; }
  .gold  { color: #f39c12; }
</style>
""", unsafe_allow_html=True)

# ── persistence helpers ───────────────────────────────────────────────────────
def load_journal():
    if _has_supabase():
        resp = requests.get(_supabase_url(), headers=_supabase_headers(), timeout=10)
        rows = resp.json()
        if rows:
            return rows[0]["data"]
        # first run — seed the row
        blank = {"open_trade": None, "closed_trades": []}
        requests.post(
            st.secrets["SUPABASE_URL"].rstrip("/") + "/rest/v1/journal",
            headers=_supabase_headers(),
            json={"id": 1, "data": blank},
            timeout=10,
        )
        return blank
    with open(JOURNAL_FILE) as f:
        return json.load(f)

def save_journal(data):
    if _has_supabase():
        requests.patch(_supabase_url(), headers=_supabase_headers(),
                       json={"data": data}, timeout=10)
        return
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_watchlist():
    if _has_supabase():
        # store watchlist as a comma-separated string in Streamlit secrets
        raw = st.secrets.get("WATCHLIST", "NVDA,AAPL,MSFT,TSLA,AMD,META,GOOGL,AMZN,SPY,QQQ")
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    with open(WATCHLIST_FILE) as f:
        return [l.strip().upper() for l in f if l.strip()]

def save_watchlist(tickers):
    if _has_supabase():
        # persist in session only — Streamlit secrets are read-only at runtime
        st.session_state["watchlist_override"] = tickers
        return
    with open(WATCHLIST_FILE, "w") as f:
        f.write("\n".join(tickers) + "\n")

def load_watchlist():
    if "watchlist_override" in st.session_state:
        return st.session_state["watchlist_override"]
    if _has_supabase():
        raw = st.secrets.get("WATCHLIST", "NVDA,AAPL,MSFT,TSLA,AMD,META,GOOGL,AMZN,SPY,QQQ")
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    with open(WATCHLIST_FILE) as f:
        return [l.strip().upper() for l in f if l.strip()]

# ── price helpers ─────────────────────────────────────────────────────────────
def get_close(df, ticker=None):
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        col = ticker if (ticker and ticker in close.columns) else close.columns[0]
        return close[col].dropna()
    return close.squeeze().dropna()

def pct_return(series):
    s = series.dropna()
    if len(s) < 2:
        return None
    return float((s.iloc[-1] - s.iloc[0]) / s.iloc[0] * 100)

def above_50ma(series):
    s = series.dropna()
    if len(s) < MA_PERIOD:
        return False
    return float(s.iloc[-1]) > float(s.iloc[-MA_PERIOD:].mean())

# ── universe loader ───────────────────────────────────────────────────────────
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_all_tickers():
    tickers = set()
    for url in [NASDAQ_URL, OTHER_URL]:
        try:
            resp = requests.get(url, timeout=15)
            df = pd.read_csv(io.StringIO(resp.text), sep="|")[:-1]
            sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            raw = df[sym_col].dropna().astype(str)
            tickers.update(raw[~raw.str.contains(r"[^A-Z]", regex=True)].tolist())
        except Exception:
            pass
    wl = load_watchlist()
    return sorted(set(tickers) | set(wl))

# ── batch scanner ─────────────────────────────────────────────────────────────
def scan_batch(tickers, period_days):
    end   = datetime.today()
    start = end - timedelta(days=period_days)
    try:
        df = yf.download(
            tickers, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
            progress=False, auto_adjust=True, threads=True,
        )
    except Exception:
        return {}
    if df.empty:
        return {}

    results = {}
    lookback = LOOKBACK_WEEKS * 5
    for t in tickers:
        try:
            close = get_close(df, t)
            if len(close) < max(MA_PERIOD, lookback + 1):
                continue
            ret = pct_return(close.iloc[-lookback:])
            if ret is None or not above_50ma(close):
                continue
            results[t] = {"price": float(close.iloc[-1]), "return": ret}
        except Exception:
            continue
    return results

def run_full_scan(tickers, progress_bar, status_text):
    period_days = LOOKBACK_WEEKS * 7 + 90
    # SPY benchmark
    status_text.text("Fetching SPY benchmark...")
    spy_res = scan_batch(["SPY"], period_days)
    spy_ret = spy_res.get("SPY", {}).get("return", 0.0)

    non_spy = [t for t in tickers if t != "SPY"]
    batches = [non_spy[i:i+BATCH_SIZE] for i in range(0, len(non_spy), BATCH_SIZE)]
    qualifying = []

    for i, batch in enumerate(batches):
        pct = (i + 1) / len(batches)
        progress_bar.progress(pct)
        status_text.text(f"Scanning batch {i+1}/{len(batches)}  ({min((i+1)*BATCH_SIZE, len(non_spy))} of {len(non_spy)} tickers)...")
        results = scan_batch(batch, period_days)
        for ticker, data in results.items():
            qualifying.append({
                "ticker": ticker,
                "price":  data["price"],
                "return": data["return"],
                "rs":     data["return"] - spy_ret,
            })

    progress_bar.progress(1.0)
    status_text.text(f"Done — {len(qualifying)} qualifying stocks found.")
    return qualifying, spy_ret

# ── sector fetch ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sector(ticker):
    try:
        info = yf.Ticker(ticker).info
        return info.get("sector", "Unknown"), info.get("marketCap", 0)
    except Exception:
        return "Unknown", 0

# ── live price ────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def get_live_price(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="2d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None

# ── price chart ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def get_chart_data(ticker, days=90):
    end = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    return df

def build_price_chart(ticker, entry_price, target_price, stop_price):
    df = get_chart_data(ticker)
    if df.empty:
        return None
    close = get_close(df, ticker)
    dates = close.index

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=close.values, mode="lines", name=ticker,
        line=dict(color="#3498db", width=2),
        fill="tozeroy", fillcolor="rgba(52,152,219,0.08)",
    ))
    fig.add_hline(y=entry_price, line=dict(color="#f39c12", dash="dash", width=1.5),
                  annotation_text=f"Entry ${entry_price:.2f}", annotation_position="bottom right")
    fig.add_hline(y=target_price, line=dict(color="#2ecc71", dash="dot", width=1.5),
                  annotation_text=f"Target ${target_price:.2f}", annotation_position="top right")
    fig.add_hline(y=stop_price, line=dict(color="#e74c3c", dash="dot", width=1.5),
                  annotation_text=f"Stop ${stop_price:.2f}", annotation_position="bottom right")
    fig.update_layout(
        paper_bgcolor="#0f1117", plot_bgcolor="#0f1117",
        font=dict(color="#c9cdd4"), height=380,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(gridcolor="#1c1e26", showgrid=True),
        yaxis=dict(gridcolor="#1c1e26", showgrid=True),
        showlegend=False,
    )
    return fig

def build_pnl_chart(trades):
    if not trades:
        return None
    df = pd.DataFrame(trades).sort_values("exit_date")
    df["cumulative"] = df["return_pct"].cumsum()
    colors = ["#2ecc71" if r > 0 else "#e74c3c" for r in df["return_pct"]]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["exit_date"], y=df["return_pct"],
        marker_color=colors, name="Weekly Return",
        text=[f"{r:+.2f}%" for r in df["return_pct"]],
        textposition="outside",
    ))
    fig.add_trace(go.Scatter(
        x=df["exit_date"], y=df["cumulative"],
        mode="lines+markers", name="Cumulative",
        line=dict(color="#f39c12", width=2),
        yaxis="y2",
    ))
    fig.update_layout(
        paper_bgcolor="#0f1117", plot_bgcolor="#0f1117",
        font=dict(color="#c9cdd4"), height=340,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(gridcolor="#1c1e26"),
        yaxis=dict(gridcolor="#1c1e26", title="Weekly %"),
        yaxis2=dict(overlaying="y", side="right", title="Cumulative %",
                    gridcolor="#1c1e26"),
        legend=dict(bgcolor="#1c1e26"),
        barmode="group",
    )
    return fig

# ═════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ═════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 📈 Momentum Trader")
    st.divider()
    page = st.radio("Navigate", ["🔍 Scanner", "📊 Open Trade", "📓 Journal"], label_visibility="collapsed")
    st.divider()

    if page == "🔍 Scanner":
        st.markdown("### Filters")
        scan_mode = st.radio("Universe", ["Full US Market (~6k stocks)", "My Watchlist only"])
        st.markdown("**Price range**")
        col1, col2 = st.columns(2)
        price_min = col1.number_input("Min $", value=5, min_value=0, step=1)
        price_max = col2.number_input("Max $", value=5000, min_value=1, step=50)
        min_volume = st.number_input("Min avg volume (M)", value=0.5, step=0.1, format="%.1f") * 1_000_000
        sector_filter = st.multiselect("Sector (optional — slower)", SECTORS)
        exchange_filter = st.multiselect("Exchange", ["NASDAQ", "NYSE", "All"], default=["All"])
        top_n = st.slider("Show top N picks", 3, 25, 5)

# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: SCANNER
# ═════════════════════════════════════════════════════════════════════════════
if page == "🔍 Scanner":
    st.title("🔍 Weekly Momentum Scanner")
    st.caption(f"Finds stocks with the strongest relative momentum vs SPY, above their 50-day MA.")

    run_btn = st.button("▶  Run Scan", type="primary", use_container_width=True)

    if run_btn:
        if "Full" in scan_mode:
            with st.spinner("Loading ticker universe..."):
                tickers = fetch_all_tickers()
        else:
            tickers = load_watchlist()

        progress_bar = st.progress(0.0)
        status_text  = st.empty()
        results, spy_ret = run_full_scan(tickers, progress_bar, status_text)
        st.session_state["scan_results"] = results
        st.session_state["spy_ret"]      = spy_ret
        st.session_state["scan_time"]    = datetime.now().strftime("%H:%M:%S")

    if "scan_results" in st.session_state:
        results  = st.session_state["scan_results"]
        spy_ret  = st.session_state["spy_ret"]
        scan_time = st.session_state.get("scan_time", "")

        st.markdown(f"**SPY 4-week return:** {spy_ret:+.2f}%  &nbsp;|&nbsp;  **Scanned at:** {scan_time}  &nbsp;|&nbsp;  **Qualifying stocks:** {len(results)}")

        # apply filters
        filtered = [r for r in results if price_min <= r["price"] <= price_max]

        # sector filter (fetch info for top 200 by RS only)
        if sector_filter:
            with st.spinner(f"Fetching sector data for top results..."):
                top200 = sorted(filtered, key=lambda x: x["rs"], reverse=True)[:200]
                for r in top200:
                    s, mc = fetch_sector(r["ticker"])
                    r["sector"] = s
                    r["market_cap"] = mc
                filtered = [r for r in top200 if r.get("sector", "Unknown") in sector_filter]
        else:
            for r in filtered:
                r.setdefault("sector", "—")
                r.setdefault("market_cap", 0)

        ranked = sorted(filtered, key=lambda x: x["rs"], reverse=True)[:top_n]

        if not ranked:
            st.warning("No stocks match your current filters. Try widening the price range or removing sector filters.")
        else:
            # top pick highlight
            best = ranked[0]
            st.divider()
            st.markdown(f"### 🥇 Top Pick — `{best['ticker']}`")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Price",      f"${best['price']:.2f}")
            c2.metric("4-wk Return", f"{best['return']:+.2f}%")
            c3.metric("RS vs SPY",   f"{best['rs']:+.2f}%")
            c4.metric("Target (+6%)", f"${best['price']*1.06:.2f}")

            journal = load_journal()
            if journal.get("open_trade"):
                st.info(f"⚠️  You already have an open trade: **{journal['open_trade']['ticker']}** @ ${journal['open_trade']['entry_price']:.2f}. Close it first in the Open Trade page.")
            else:
                if st.button(f"✅  Open trade on {best['ticker']} @ ${best['price']:.2f}", type="primary"):
                    trade = {
                        "ticker":       best["ticker"],
                        "entry_price":  best["price"],
                        "entry_date":   datetime.today().strftime("%Y-%m-%d"),
                        "target_price": round(best["price"] * 1.06, 2),
                        "stop_price":   round(best["price"] * 0.97, 2),
                        "rs_score":     round(best["rs"], 2),
                    }
                    journal["open_trade"] = trade
                    save_journal(journal)
                    st.success(f"Trade opened: {trade['ticker']} @ ${trade['entry_price']:.2f}")

            # full ranked table
            st.divider()
            st.markdown(f"### Top {len(ranked)} Momentum Picks")
            df_display = pd.DataFrame(ranked)
            df_display = df_display.rename(columns={
                "ticker": "Ticker", "price": "Price", "return": "4wk Return %",
                "rs": "RS vs SPY %", "sector": "Sector",
            })
            display_cols = [c for c in ["Ticker","Price","4wk Return %","RS vs SPY %","Sector"] if c in df_display.columns]
            df_display = df_display[display_cols]
            df_display["Price"] = df_display["Price"].map("${:.2f}".format)

            def color_rs(val):
                if isinstance(val, float):
                    color = "#2ecc71" if val > 0 else "#e74c3c"
                    return f"color: {color}; font-weight: bold"
                return ""

            st.dataframe(
                df_display.style.map(color_rs, subset=["RS vs SPY %","4wk Return %"]),
                use_container_width=True, hide_index=True,
            )

# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: OPEN TRADE
# ═════════════════════════════════════════════════════════════════════════════
elif page == "📊 Open Trade":
    st.title("📊 Open Trade")
    journal = load_journal()
    trade = journal.get("open_trade")

    if not trade:
        st.info("No open trade. Run the scanner on Monday to get this week's pick.")
    else:
        ticker       = trade["ticker"]
        entry        = trade["entry_price"]
        target       = trade["target_price"]
        stop         = trade["stop_price"]
        entry_date   = trade["entry_date"]

        # live price
        with st.spinner("Fetching live price..."):
            current = get_live_price(ticker)

        if current:
            chg = (current - entry) / entry * 100
        else:
            current, chg = entry, 0.0

        # alert banners
        if chg >= TAKE_PROFIT_PCT:
            st.markdown(f'<div class="sell-alert">🟢 <b>SELL ALERT</b> — {ticker} is up <b>{chg:+.2f}%</b> from entry. You\'ve hit your +6% target!</div>', unsafe_allow_html=True)
            st.markdown("")
        elif chg <= STOP_LOSS_PCT:
            st.markdown(f'<div class="stop-alert">🔴 <b>STOP LOSS WARNING</b> — {ticker} is down <b>{chg:.2f}%</b> from entry. Consider cutting the position.</div>', unsafe_allow_html=True)
            st.markdown("")
        else:
            st.markdown(f'<div class="info-alert">🔵 Holding — {ticker} is <b>{chg:+.2f}%</b> from entry.</div>', unsafe_allow_html=True)
            st.markdown("")

        # metric row
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Ticker",      ticker)
        c2.metric("Entry Price", f"${entry:.2f}", delta=f"Entry date: {entry_date}", delta_color="off")
        c3.metric("Current",     f"${current:.2f}" if current else "—", delta=f"{chg:+.2f}%" if current else None,
                  delta_color="normal")
        c4.metric("Target +6%",  f"${target:.2f}")
        c5.metric("Stop -3%",    f"${stop:.2f}")

        # chart
        st.divider()
        fig = build_price_chart(ticker, entry, target, stop)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        # close trade form
        st.divider()
        st.markdown("### Close This Trade")
        with st.form("close_form"):
            exit_price = st.number_input("Exit price", min_value=0.01, value=float(f"{current:.2f}") if current else entry, step=0.01)
            submitted  = st.form_submit_button("💰  Close Trade & Save Result", type="primary")
            if submitted:
                ret    = (exit_price - entry) / entry * 100
                result = "WIN" if ret > 0 else "LOSS"
                closed = {
                    "ticker":       ticker,
                    "entry_price":  entry,
                    "entry_date":   entry_date,
                    "exit_price":   exit_price,
                    "exit_date":    datetime.today().strftime("%Y-%m-%d"),
                    "return_pct":   round(ret, 3),
                    "result":       result,
                    "rs_score":     trade.get("rs_score"),
                }
                journal["closed_trades"].append(closed)
                journal["open_trade"] = None
                save_journal(journal)
                color = "green" if ret > 0 else "red"
                st.markdown(f'<span class="{color}">Trade closed: {ticker}  {ret:+.2f}%  [{result}]</span>', unsafe_allow_html=True)
                st.balloons() if ret > 0 else None
                time.sleep(1)
                st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: JOURNAL
# ═════════════════════════════════════════════════════════════════════════════
elif page == "📓 Journal":
    st.title("📓 Trade Journal")
    journal = load_journal()
    trades  = journal.get("closed_trades", [])

    # ── summary stats ─────────────────────────────────────────────────────────
    if trades:
        returns  = [t["return_pct"] for t in trades]
        wins     = [r for r in returns if r > 0]
        win_rate = len(wins) / len(returns) * 100
        avg_ret  = sum(returns) / len(returns)
        best     = max(returns)
        worst    = min(returns)
        cumul    = sum(returns)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Trades",   len(trades))
        c2.metric("Win Rate",       f"{win_rate:.1f}%")
        c3.metric("Avg Weekly Ret", f"{avg_ret:+.2f}%", delta_color="normal")
        c4.metric("Best Trade",     f"{best:+.2f}%")
        c5.metric("Cumulative",     f"{cumul:+.2f}%", delta_color="normal")

        # P&L chart
        st.divider()
        st.markdown("### P&L History")
        fig = build_pnl_chart(trades)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        # trade table
        st.divider()
        st.markdown("### All Trades")
        df = pd.DataFrame(trades).sort_values("exit_date", ascending=False)
        df = df.rename(columns={
            "ticker": "Ticker", "entry_price": "Entry $", "exit_price": "Exit $",
            "entry_date": "Opened", "exit_date": "Closed",
            "return_pct": "Return %", "result": "Result", "rs_score": "RS Score",
        })
        display_cols = ["Ticker","Opened","Closed","Entry $","Exit $","Return %","Result","RS Score"]
        df = df[[c for c in display_cols if c in df.columns]]
        df["Entry $"] = df["Entry $"].map("${:.2f}".format)
        df["Exit $"]  = df["Exit $"].map("${:.2f}".format)

        def color_result(val):
            if val == "WIN":  return "color: #2ecc71; font-weight: bold"
            if val == "LOSS": return "color: #e74c3c; font-weight: bold"
            return ""
        def color_return(val):
            if isinstance(val, float):
                return f"color: {'#2ecc71' if val > 0 else '#e74c3c'}; font-weight: bold"
            return ""

        st.dataframe(
            df.style.map(color_result, subset=["Result"])
                    .map(color_return, subset=["Return %"]),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No closed trades yet. Run the scanner, open a trade, and close it at end of week.")

    # ── watchlist manager ─────────────────────────────────────────────────────
    st.divider()
    st.markdown("### Watchlist Manager")
    wl = load_watchlist()
    col1, col2 = st.columns([3, 1])
    new_ticker = col1.text_input("Add ticker", placeholder="e.g. PLTR").upper().strip()
    if col2.button("Add", use_container_width=True) and new_ticker:
        if new_ticker not in wl:
            wl.append(new_ticker)
            save_watchlist(wl)
            st.success(f"{new_ticker} added.")
        else:
            st.warning(f"{new_ticker} already in watchlist.")

    st.markdown("**Current watchlist:**")
    cols = st.columns(5)
    for i, t in enumerate(wl):
        with cols[i % 5]:
            if st.button(f"✕ {t}", key=f"rm_{t}", use_container_width=True):
                wl.remove(t)
                save_watchlist(wl)
                st.rerun()
