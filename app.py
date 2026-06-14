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
MA_PERIOD         = 50
ADR_MIN_PCT       = 3.0
MOMENTUM_MIN_PCT  = 30.0
CONSOL_MAX_PCT    = 10.0
BREAKOUT_VOL_MULT = 1.5
CONSOL_DAYS       = 10
MOMENTUM_DAYS     = 60
ADR_DAYS          = 20
MA_10_PERIOD      = 10

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

# ── price / indicator helpers ─────────────────────────────────────────────────
def flatten_df(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def fetch_ticker(ticker, days=150):
    end   = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    return flatten_df(df) if not df.empty else None

def adr_pct(df):
    r = df.tail(ADR_DAYS)
    return float(((r["High"] - r["Low"]) / r["Close"] * 100).mean())

def prior_momentum(df):
    w = df.tail(MOMENTUM_DAYS)
    lo, hi = float(w["Low"].min()), float(w["High"].max())
    return (hi - lo) / lo * 100 if lo else 0.0

def above_50ma(df):
    c = df["Close"].dropna()
    return len(c) >= MA_PERIOD and float(c.iloc[-1]) > float(c.iloc[-MA_PERIOD:].mean())

def consolidation_stats(df):
    w     = df.tail(CONSOL_DAYS)
    full  = df.tail(ADR_DAYS)
    hi, lo = float(w["High"].max()), float(w["Low"].min())
    price  = float(df["Close"].iloc[-1])
    rng    = (hi - lo) / price * 100
    vol_dry = float(w["Volume"].mean()) < float(full["Volume"].mean())
    return rng, vol_dry, lo

def check_breakout(df):
    if len(df) < CONSOL_DAYS + 2:
        return False
    today  = df.iloc[-1]
    prior  = df.iloc[-(CONSOL_DAYS + 1):-1]
    avg20  = float(df.tail(ADR_DAYS + 1).iloc[:-1]["Volume"].mean())
    return (float(today["Close"]) > float(prior["High"].max()) and
            float(today["Volume"]) >= BREAKOUT_VOL_MULT * avg20)

def get_10d_ma(ticker):
    try:
        df = yf.download(ticker, period="30d", progress=False, auto_adjust=True)
        df = flatten_df(df)
        c  = df["Close"].dropna()
        return float(c.iloc[-1]), float(c.iloc[-MA_10_PERIOD:].mean()) if len(c) >= MA_10_PERIOD else None
    except Exception:
        return None, None

def days_held(entry_date_str):
    try:
        from datetime import date
        return (date.today() - date.fromisoformat(entry_date_str)).days
    except Exception:
        return 0

# ── Qullamaggie scanner ───────────────────────────────────────────────────────
def scan_ticker_qmag(ticker):
    df = fetch_ticker(ticker)
    if df is None or len(df) < MA_PERIOD + CONSOL_DAYS + 5:
        return None, "insufficient data"
    adr = adr_pct(df)
    if adr < ADR_MIN_PCT:
        return None, f"ADR {adr:.1f}% < {ADR_MIN_PCT}%"
    mom = prior_momentum(df)
    if mom < MOMENTUM_MIN_PCT:
        return None, f"prior move {mom:.1f}% < {MOMENTUM_MIN_PCT}%"
    if not above_50ma(df):
        return None, "below 50-day MA"
    rng, vol_dry, consol_low = consolidation_stats(df)
    if rng > CONSOL_MAX_PCT:
        return None, f"consol range {rng:.1f}% > {CONSOL_MAX_PCT}%"
    breakout = check_breakout(df)
    price = float(df["Close"].iloc[-1])
    risk  = round(price - consol_low, 2)
    return {
        "ticker":    ticker,
        "price":     price,
        "adr":       round(adr, 2),
        "momentum":  round(mom, 1),
        "range_pct": round(rng, 2),
        "vol_dry":   vol_dry,
        "breakout":  breakout,
        "stop":      round(consol_low, 2),
        "risk":      risk,
        "target_1r": round(price + risk, 2),
        "target_2r": round(price + 2 * risk, 2),
        "target_3r": round(price + 3 * risk, 2),
    }, None

def run_watchlist_scan(tickers, progress_bar, status_text):
    passing, rejected = [], []
    for i, t in enumerate(tickers):
        progress_bar.progress((i + 1) / len(tickers))
        status_text.text(f"Scanning {t}  ({i+1}/{len(tickers)})...")
        result, reason = scan_ticker_qmag(t)
        if result:
            passing.append(result)
        else:
            rejected.append((t, reason))
    progress_bar.progress(1.0)
    status_text.text(f"Done — {len(passing)} setups found.")
    return passing, rejected

# ── price chart ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def get_chart_data(ticker, days=90):
    end = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    return flatten_df(df) if not df.empty else pd.DataFrame()

def build_price_chart(ticker, entry_price, stop_price, t1, t2, t3, ma10=None):
    df = get_chart_data(ticker)
    if df.empty:
        return None
    close = df["Close"].dropna()
    dates = close.index
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=close.values, mode="lines", name=ticker,
        line=dict(color="#3498db", width=2),
        fill="tozeroy", fillcolor="rgba(52,152,219,0.06)",
    ))
    if ma10 is not None:
        ma_series = close.rolling(MA_10_PERIOD).mean()
        fig.add_trace(go.Scatter(x=dates, y=ma_series.values, mode="lines",
                                 name="10d MA", line=dict(color="#f39c12", width=1.5, dash="dot")))
    fig.add_hline(y=entry_price, line=dict(color="#f39c12", dash="dash", width=1.5),
                  annotation_text=f"Entry ${entry_price:.2f}", annotation_position="bottom right")
    fig.add_hline(y=stop_price, line=dict(color="#e74c3c", dash="dot", width=1.5),
                  annotation_text=f"Stop ${stop_price:.2f}", annotation_position="bottom right")
    for target, label, color in [(t1,"1R","#2ecc71"),(t2,"2R","#27ae60"),(t3,"3R","#1e8449")]:
        if target:
            fig.add_hline(y=target, line=dict(color=color, dash="dot", width=1),
                          annotation_text=f"{label} ${target:.2f}", annotation_position="top right")
    fig.update_layout(
        paper_bgcolor="#0f1117", plot_bgcolor="#0f1117",
        font=dict(color="#c9cdd4"), height=400,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(gridcolor="#1c1e26"),
        yaxis=dict(gridcolor="#1c1e26"),
        legend=dict(bgcolor="#1c1e26"),
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
    st.markdown("## 📈 Swing Trader")
    st.caption("Qullamaggie VCP methodology")
    st.divider()
    page = st.radio("Navigate", ["🔍 Scanner", "📊 Open Trade", "📓 Journal"], label_visibility="collapsed")
    st.divider()

    if page == "🔍 Scanner":
        st.markdown("### Filters")
        col1, col2 = st.columns(2)
        price_min = col1.number_input("Min $", value=5, min_value=0, step=1)
        price_max = col2.number_input("Max $", value=5000, min_value=1, step=50)
        adr_min   = st.slider("Min ADR %", 1.0, 10.0, float(ADR_MIN_PCT), 0.5)
        mom_min   = st.slider("Min prior move %", 10, 100, int(MOMENTUM_MIN_PCT), 5)
        consol_max = st.slider("Max consol range %", 3.0, 20.0, float(CONSOL_MAX_PCT), 0.5)

# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: SCANNER
# ═════════════════════════════════════════════════════════════════════════════
if page == "🔍 Scanner":
    st.title("🔍 VCP Breakout Scanner")
    st.caption("Scans your watchlist for Volatility Contraction Pattern setups and confirmed breakouts.")

    run_btn = st.button("▶  Run Scan", type="primary", use_container_width=True)

    if run_btn:
        tickers      = load_watchlist()
        progress_bar = st.progress(0.0)
        status_text  = st.empty()
        passing, rejected = run_watchlist_scan(tickers, progress_bar, status_text)
        st.session_state["scan_passing"]  = passing
        st.session_state["scan_rejected"] = rejected
        st.session_state["scan_time"]     = datetime.now().strftime("%H:%M:%S")

    if "scan_passing" in st.session_state:
        passing  = st.session_state["scan_passing"]
        rejected = st.session_state["scan_rejected"]
        scan_time = st.session_state.get("scan_time", "")

        # apply sidebar filters
        filtered = [s for s in passing
                    if price_min <= s["price"] <= price_max
                    and s["adr"] >= adr_min
                    and s["momentum"] >= mom_min
                    and s["range_pct"] <= consol_max]

        breakouts = [s for s in filtered if s["breakout"]]
        on_watch  = [s for s in filtered if not s["breakout"]]
        on_watch.sort(key=lambda x: x["range_pct"])

        st.markdown(f"**Scanned at:** {scan_time}  &nbsp;|&nbsp;  **Setups found:** {len(filtered)}  &nbsp;|&nbsp;  **Breakouts:** {len(breakouts)}  &nbsp;|&nbsp;  **On watch:** {len(on_watch)}")

        # ── breakouts ─────────────────────────────────────────────────────────
        if breakouts:
            st.divider()
            st.markdown("### 🚀 Confirmed Breakouts")
            journal = load_journal()
            for s in breakouts:
                with st.container(border=True):
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Ticker",        s["ticker"])
                    c2.metric("Price",         f"${s['price']:.2f}")
                    c3.metric("ADR",           f"{s['adr']:.1f}%")
                    c4.metric("Prior move",    f"{s['momentum']:.0f}%")
                    c5.metric("Consol range",  f"{s['range_pct']:.1f}%")
                    st.markdown(f"**Stop:** ${s['stop']:.2f}  &nbsp;|&nbsp;  **Risk/share:** ${s['risk']:.2f}  &nbsp;|&nbsp;  **1R:** ${s['target_1r']:.2f}  &nbsp;|&nbsp;  **2R:** ${s['target_2r']:.2f}  &nbsp;|&nbsp;  **3R:** ${s['target_3r']:.2f}")
                    if journal.get("open_trade"):
                        st.caption(f"⚠️  Close existing {journal['open_trade']['ticker']} trade first.")
                    else:
                        if st.button(f"✅  Open trade on {s['ticker']}", key=f"open_{s['ticker']}", type="primary"):
                            trade = {
                                "ticker":      s["ticker"],
                                "entry_price": s["price"],
                                "entry_date":  datetime.today().strftime("%Y-%m-%d"),
                                "stop_price":  s["stop"],
                                "risk":        s["risk"],
                                "target_1r":   s["target_1r"],
                                "target_2r":   s["target_2r"],
                                "target_3r":   s["target_3r"],
                                "adr":         s["adr"],
                                "momentum":    s["momentum"],
                            }
                            journal["open_trade"] = trade
                            save_journal(journal)
                            st.success(f"Trade opened: {s['ticker']} @ ${s['price']:.2f}")

        # ── on watch ──────────────────────────────────────────────────────────
        if on_watch:
            st.divider()
            st.markdown("### 👁 On Watch — Consolidating")
            df_watch = pd.DataFrame(on_watch)[["ticker","price","adr","momentum","range_pct","vol_dry","stop","target_1r","target_2r","target_3r"]]
            df_watch.columns = ["Ticker","Price","ADR%","Prior Move%","Consol Range%","Vol Drying","Stop","1R","2R","3R"]
            df_watch["Price"] = df_watch["Price"].map("${:.2f}".format)
            df_watch["Stop"]  = df_watch["Stop"].map("${:.2f}".format)
            df_watch["1R"]    = df_watch["1R"].map("${:.2f}".format)
            df_watch["2R"]    = df_watch["2R"].map("${:.2f}".format)
            df_watch["3R"]    = df_watch["3R"].map("${:.2f}".format)
            df_watch["Vol Drying"] = df_watch["Vol Drying"].map(lambda x: "✅ Yes" if x else "No")
            st.dataframe(df_watch, use_container_width=True, hide_index=True)

        if not filtered:
            st.info("No setups pass your filters right now. Try loosening the sliders or adding more tickers to your watchlist.")

        # ── rejected ──────────────────────────────────────────────────────────
        if rejected:
            with st.expander(f"Filtered out ({len(rejected)} tickers)"):
                for t, reason in rejected:
                    st.caption(f"**{t}** — {reason}")

# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: OPEN TRADE
# ═════════════════════════════════════════════════════════════════════════════
elif page == "📊 Open Trade":
    st.title("📊 Open Trade")
    journal = load_journal()
    trade   = journal.get("open_trade")

    if not trade:
        st.info("No open trade. Run the scanner to find a breakout setup.")
    else:
        ticker     = trade["ticker"]
        entry      = trade["entry_price"]
        stop       = trade["stop_price"]
        risk       = trade.get("risk", entry - stop)
        t1         = trade.get("target_1r")
        t2         = trade.get("target_2r")
        t3         = trade.get("target_3r")
        entry_date = trade["entry_date"]
        held       = days_held(entry_date)

        with st.spinner("Fetching live price and 10-day MA..."):
            current, ma10 = get_10d_ma(ticker)

        chg = (current - entry) / entry * 100 if current else 0.0

        # ── Qullamaggie exit signals ──────────────────────────────────────────
        if current and ma10 and current < ma10:
            st.markdown(f'<div class="stop-alert">🔴 <b>FULL EXIT SIGNAL</b> — {ticker} closed below the 10-day MA (${ma10:.2f}). Qullamaggie trailing stop rule: exit the remaining position.</div>', unsafe_allow_html=True)
            st.markdown("")
        elif 3 <= held <= 5:
            st.markdown(f'<div class="sell-alert">🟡 <b>DAY {held} REMINDER</b> — Qullamaggie rule: consider selling 1/3 to 1/2 of your position now and moving your stop to breakeven (${entry:.2f}) on the rest.</div>', unsafe_allow_html=True)
            st.markdown("")
        else:
            above = f"${current - ma10:.2f} above 10-day MA" if (current and ma10) else ""
            st.markdown(f'<div class="info-alert">🔵 <b>HOLD</b> — {ticker} is still above the 10-day MA. {above}</div>', unsafe_allow_html=True)
            st.markdown("")

        # metrics
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Ticker",    ticker)
        c2.metric("Entry",     f"${entry:.2f}", delta=f"Day {held}", delta_color="off")
        c3.metric("Current",   f"${current:.2f}" if current else "—", delta=f"{chg:+.2f}%", delta_color="normal")
        c4.metric("10d MA",    f"${ma10:.2f}" if ma10 else "—")
        c5.metric("Stop",      f"${stop:.2f}")
        c6.metric("Risk/share",f"${risk:.2f}")

        st.markdown(f"**Targets:** &nbsp; 1R `${t1:.2f}` &nbsp;&nbsp; 2R `${t2:.2f}` &nbsp;&nbsp; 3R `${t3:.2f}`" if t1 else "")

        st.divider()
        fig = build_price_chart(ticker, entry, stop, t1, t2, t3, ma10)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.markdown("### Close Position")
        with st.form("close_form"):
            exit_price = st.number_input("Exit price", min_value=0.01,
                                         value=float(f"{current:.2f}") if current else float(entry), step=0.01)
            submitted  = st.form_submit_button("💰  Close Trade & Save Result", type="primary")
            if submitted:
                ret    = (exit_price - entry) / entry * 100
                r_mult = (exit_price - entry) / risk if risk else 0
                result = "WIN" if ret > 0 else "LOSS"
                closed = {
                    "ticker":      ticker,
                    "entry_price": entry,
                    "entry_date":  entry_date,
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
                st.markdown(f'Trade closed: **{ticker}**  {ret:+.2f}%  [{r_mult:+.2f}R]  **{result}**')
                if ret > 0:
                    st.balloons()
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

        r_mults  = [t.get("r_multiple", 0) for t in trades]
        avg_r    = sum(r_mults) / len(r_mults) if r_mults else 0
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Total Trades",  len(trades))
        c2.metric("Win Rate",      f"{win_rate:.1f}%")
        c3.metric("Avg Return",    f"{avg_ret:+.2f}%")
        c4.metric("Avg R-Multiple",f"{avg_r:+.2f}R")
        c5.metric("Best Trade",    f"{best:+.2f}%")
        c6.metric("Cumulative",    f"{cumul:+.2f}%")

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
            "return_pct": "Return %", "result": "Result", "r_multiple": "R Multiple",
        })
        display_cols = ["Ticker","Opened","Closed","Entry $","Exit $","Return %","R Multiple","Result"]
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
