"""
Swing Trading App — Qullamaggie VCP methodology
Launch: streamlit run app.py
"""

import io
import json
import os
import time
from datetime import datetime, date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

BASE              = os.path.dirname(__file__)
JOURNAL_FILE      = os.path.join(BASE, "journal.json")
WATCHLIST_FILE    = os.path.join(BASE, "watchlist.txt")
MY_WATCHLIST_FILE = os.path.join(BASE, "my_watchlist.json")

ADR_MIN_PCT       = 3.0
MOMENTUM_MIN_PCT  = 30.0
CONSOL_MAX_PCT    = 12.0   # slightly wider to catch more bases
BREAKOUT_VOL_MULT = 1.2    # lowered from 1.5 — less strict volume requirement
NEAR_BREAKOUT_PCT = 3.0    # within 3% of the 10-day high = "Buy Soon"
CONSOL_DAYS       = 15     # look back 15 days for consolidation
MOMENTUM_DAYS     = 60
ADR_DAYS          = 20
MA_50             = 50
MA_10             = 10
BATCH_SIZE        = 80

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_URL  = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Swing Trader",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  .alert-green  { background:#0d3b1e; border-left:4px solid #00d084; border-radius:8px; padding:16px 20px; margin:10px 0; color:#f0f0f0; }
  .alert-red    { background:#3b0d0d; border-left:4px solid #ff4444; border-radius:8px; padding:16px 20px; margin:10px 0; color:#f0f0f0; }
  .alert-yellow { background:#3b2d0d; border-left:4px solid #ffaa00; border-radius:8px; padding:16px 20px; margin:10px 0; color:#f0f0f0; }
  .alert-blue   { background:#0d1f3b; border-left:4px solid #4488ff; border-radius:8px; padding:16px 20px; margin:10px 0; color:#f0f0f0; }
  .stButton > button { font-weight:600; border-radius:8px; }
</style>
""", unsafe_allow_html=True)


# ── storage ───────────────────────────────────────────────────────────────────

def _has_supabase():
    try:
        return "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets
    except Exception:
        return False

def _sb_headers():
    k = st.secrets["SUPABASE_KEY"]
    return {"apikey": k, "Authorization": f"Bearer {k}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def _sb_url():
    return st.secrets["SUPABASE_URL"].rstrip("/") + "/rest/v1/journal?id=eq.1"

def load_journal():
    if _has_supabase():
        rows = requests.get(_sb_url(), headers=_sb_headers(), timeout=10).json()
        if rows:
            return rows[0]["data"]
        blank = {"open_trade": None, "closed_trades": []}
        requests.post(st.secrets["SUPABASE_URL"].rstrip("/") + "/rest/v1/journal",
                      headers=_sb_headers(), json={"id": 1, "data": blank}, timeout=10)
        return blank
    with open(JOURNAL_FILE) as f:
        return json.load(f)

def save_journal(data):
    if _has_supabase():
        requests.patch(_sb_url(), headers=_sb_headers(), json={"data": data}, timeout=10)
        return
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_watchlist():
    if "wl_override" in st.session_state:
        return st.session_state["wl_override"]
    if _has_supabase():
        raw = st.secrets.get("WATCHLIST", "NVDA,AAPL,MSFT,TSLA,AMD,META,GOOGL,AMZN,SPY,QQQ")
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    with open(WATCHLIST_FILE) as f:
        return [l.strip().upper() for l in f if l.strip()]

def save_watchlist(tickers):
    st.session_state["wl_override"] = tickers
    if not _has_supabase():
        with open(WATCHLIST_FILE, "w") as f:
            f.write("\n".join(tickers) + "\n")


# ── my personal watchlist (my_watchlist.json / Supabase row id=2) ─────────────

def _sb_mywl_url():
    return st.secrets["SUPABASE_URL"].rstrip("/") + "/rest/v1/journal?id=eq.2"

def _load_my_watchlist_from_storage():
    """Read from Supabase or local file — used only on first load."""
    if _has_supabase():
        try:
            rows = requests.get(_sb_mywl_url(), headers=_sb_headers(), timeout=10).json()
            return rows[0]["data"] if rows else []
        except Exception:
            return []
    if not os.path.exists(MY_WATCHLIST_FILE):
        return []
    with open(MY_WATCHLIST_FILE) as f:
        return json.load(f)

def load_my_watchlist():
    """Return watchlist from session state (instant) — load from storage once per session."""
    if "mywl_items" not in st.session_state:
        st.session_state["mywl_items"] = _load_my_watchlist_from_storage()
    return st.session_state["mywl_items"]

def save_my_watchlist(items):
    """Write to session state immediately, then persist to storage."""
    st.session_state["mywl_items"] = items          # instant, no race condition
    try:
        if _has_supabase():
            rows = requests.get(_sb_mywl_url(), headers=_sb_headers(), timeout=10).json()
            if rows:
                requests.patch(_sb_mywl_url(), headers=_sb_headers(),
                               json={"data": items}, timeout=10)
            else:
                requests.post(st.secrets["SUPABASE_URL"].rstrip("/") + "/rest/v1/journal",
                              headers=_sb_headers(),
                              json={"id": 2, "data": items}, timeout=10)
        else:
            with open(MY_WATCHLIST_FILE, "w") as f:
                json.dump(items, f, indent=2)
    except Exception:
        pass  # session state already updated; storage will sync on next save


# ── ticker universes ──────────────────────────────────────────────────────────

@st.cache_data(ttl=86400, show_spinner=False)
def get_sp500():
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        df  = pd.read_csv(url)
        return sorted(df["Symbol"].str.replace(".", "-").tolist())
    except Exception:
        return []

@st.cache_data(ttl=86400, show_spinner=False)
def get_nasdaq100():
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100", attrs={"id": "constituents"})
        df = tables[0]
        col = next((c for c in df.columns if c.lower() in ("ticker", "symbol")), None)
        if col:
            return sorted(df[col].dropna().str.replace(".", "-").tolist())
    except Exception:
        pass
    # Hardcoded fallback (as of mid-2025)
    return sorted([
        "AAPL","ABNB","ADBE","ADI","ADP","ADSK","AEP","AMAT","AMD","AMGN","AMZN","ANSS","ARM",
        "ASML","AVGO","AZN","BIIB","BKNG","BKR","CCEP","CDNS","CDW","CEG","CHTR","CMCSA","COST",
        "CPRT","CRWD","CSCO","CSGP","CSX","CTAS","CTSH","DASH","DDOG","DLTR","DXCM","EA","EXC",
        "FANG","FAST","FTNT","GEHC","GFS","GILD","GOOG","GOOGL","HON","IDXX","ILMN","INTC","INTU",
        "ISRG","KDP","KHC","KLAC","LRCX","LULU","MAR","MCHP","MDLZ","MELI","META","MNST","MRNA",
        "MRVL","MSFT","MU","NFLX","NVDA","NXPI","ODFL","ON","ORLY","PANW","PAYX","PCAR","PDD",
        "PEP","PLTR","PYPL","QCOM","REGN","ROP","ROST","SBUX","SMCI","SNPS","SPLK","TEAM","TMUS",
        "TSLA","TTD","TXN","VRSK","VRTX","WBA","WBD","WDAY","XEL","ZS",
    ])

@st.cache_data(ttl=86400, show_spinner=False)
def get_dow30():
    return sorted([
        "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
        "GS","HD","HON","IBM","JNJ","JPM","KO","MCD","MMM","MRK",
        "MSFT","NKE","PG","SHW","TRV","UNH","V","VZ","WMT","AMZN",
    ])

@st.cache_data(ttl=86400, show_spinner=False)
def get_russell2000():
    """Curated ~120 liquid small-caps representative of the Russell 2000."""
    return sorted([
        # Financials / Banks
        "WSFS","CVBF","FFIN","HOMB","IBCP","PRSP","HTLF","PACW","BANR","TOWN",
        # Technology
        "SMTC","QLYS","RDWR","POWI","FORM","LBAI","CEVA","NTGR","DAKT","MTRX",
        "ACMR","ICHR","MKSI","NOVT","PMTS","TRMK","CRUS","AMBA","SLAB","NPKI",
        # Healthcare / Biotech
        "NARI","ATRC","ACCD","AMED","PGNY","ADUS","LFST","HALO","INVA","FWRD",
        "AXSM","PRTA","HRMY","SNDX","MDXG","ESTA","HAFC","GKOS","NMRD","ONEM",
        # Consumer
        "WING","CAVA","TXRH","EAT","JACK","CAKE","CBRL","DENN","RUTH","FWRG",
        "CHUY","SHAK","STKS","BJ","HIBB","BOOT","RCUS","BURL","PRPL","FLXS",
        # Industrials
        "KFRC","HSII","HURN","MYR","EXPO","CRAI","MGRC","ARCB","SAIA","ECHO",
        "MRTN","PTSI","ODFL","PNTM","HTLD","CVLG","UHAL","LSTR","WERN","JBHT",
        # Energy
        "SM","RRC","CNX","SWN","CIVI","MGY","MTDR","ESTE","REI","TALO",
        # Real Estate
        "NXRT","PLYM","ILPT","INDP","STAG","UE","ALEX","SITC","ROIC","GMRE",
        # Other
        "REZI","LMB","RCII","GIII","CATO","BCRX","TTGT","MFIN","BSVN","WTFC",
    ])

@st.cache_data(ttl=86400, show_spinner=False)
def get_sector(name):
    sectors = {
        "Technology": [
            "AAPL","MSFT","NVDA","AVGO","META","GOOGL","AMZN","TSLA","AMD","INTC",
            "ORCL","CRM","ADBE","QCOM","TXN","AMAT","MU","LRCX","KLAC","MCHP",
            "NOW","SNOW","PLTR","NET","CRWD","ZS","PANW","FTNT","DDOG","TTD",
        ],
        "Healthcare": [
            "UNH","JNJ","LLY","ABBV","MRK","TMO","ABT","DHR","BMY","AMGN",
            "GILD","VRTX","REGN","ISRG","CI","CVS","HUM","MDT","BSX","SYK",
            "EW","IQV","DXCM","IDXX","GEHC","PODD","HOLX","MASI","ALGN","ZBH",
        ],
        "Financials": [
            "JPM","BAC","WFC","GS","MS","BLK","C","AXP","CB","MMC",
            "AON","PNC","USB","TFC","COF","SPGI","MCO","ICE","CME","SCHW",
            "BK","STT","PRU","MET","AIG","AFL","ALL","PGR","HIG","TRV",
        ],
        "Energy": [
            "XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO","PXD","OXY",
            "HAL","BKR","FANG","DVN","HES","APA","MRO","CTRA","MTDR","PR",
            "RRC","CNX","SM","TALO","MGY","NOG","CIVI","CHX","NE","VAL",
        ],
        "Consumer Discretionary": [
            "AMZN","TSLA","HD","MCD","NKE","SBUX","TJX","BKNG","LOW","CMG",
            "GM","F","ABNB","MAR","HLT","RCL","CCL","NCLH","DHI","LEN",
            "PHM","ROST","ORLY","AZO","DG","DLTR","BBY","EBAY","ETSY","W",
        ],
        "Industrials": [
            "HON","UPS","RTX","CAT","DE","BA","LMT","GE","MMM","EMR",
            "ITW","PH","ETN","ROK","AME","XYL","CARR","OTIS","CSX","NSC",
            "UNP","FDX","DAL","UAL","AAL","GD","NOC","LHX","TDG","CTAS",
        ],
        "Real Estate": [
            "PLD","AMT","EQIX","CCI","PSA","EQR","AVB","O","SPG","VTR",
            "WELL","DLR","SBAC","ARE","BXP","KIM","REG","FRT","HST","IRM",
            "ESS","CPT","MAA","UDR","NNN","WPC","STAG","VICI","GLPI","RHP",
        ],
    }
    return sorted(sectors.get(name, []))

@st.cache_data(ttl=86400, show_spinner=False)
def get_full_market():
    tickers = set()
    for url in [NASDAQ_URL, OTHER_URL]:
        try:
            resp = requests.get(url, timeout=15)
            df   = pd.read_csv(io.StringIO(resp.text), sep="|")[:-1]
            col  = "Symbol" if "Symbol" in df.columns else df.columns[0]
            raw  = df[col].dropna().astype(str)
            tickers.update(raw[~raw.str.contains(r"[^A-Z]", regex=True)].tolist())
        except Exception:
            pass
    return sorted(tickers)


UNIVERSE_OPTIONS = [
    "S&P 500 (~5 min)",
    "Nasdaq 100 (~2 min)",
    "Dow Jones 30 (fast)",
    "Russell 2000 — Small Caps (~3 min)",
    "── Sectors ──",
    "Technology Sector",
    "Healthcare Sector",
    "Financials Sector",
    "Energy Sector",
    "Consumer Discretionary Sector",
    "Industrials Sector",
    "Real Estate Sector",
    "Full US Market (~20 min)",
]


# ── data helpers ──────────────────────────────────────────────────────────────

def flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def fetch_history(ticker, days=150):
    end   = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), progress=False,
                     auto_adjust=True, threads=False)
    return flatten(df) if not df.empty else None

def days_held(entry_date_str):
    try:
        return (date.today() - date.fromisoformat(entry_date_str)).days
    except Exception:
        return 0


# ── VCP scan logic ────────────────────────────────────────────────────────────

def scan_one(ticker):
    df = fetch_history(ticker)
    if df is None or len(df) < MA_50 + CONSOL_DAYS + 5:
        return None, "not enough data"

    # ADR
    adr = float(((df["High"] - df["Low"]) / df["Close"] * 100).tail(ADR_DAYS).mean())
    if adr < ADR_MIN_PCT:
        return None, f"ADR {adr:.1f}% — moves too little"

    # Prior momentum
    w   = df.tail(MOMENTUM_DAYS)
    lo  = float(w["Low"].min())
    hi  = float(w["High"].max())
    mom = (hi - lo) / lo * 100 if lo else 0
    if mom < MOMENTUM_MIN_PCT:
        return None, f"only moved {mom:.0f}% in 60 days — no momentum"

    # 50-day MA trend filter
    closes = df["Close"].dropna()
    ma50   = float(closes.iloc[-MA_50:].mean())
    price  = float(closes.iloc[-1])
    if price < ma50:
        return None, "below 50-day average — downtrend"

    # Consolidation tightness
    consol   = df.tail(CONSOL_DAYS)
    full_vol = df.tail(ADR_DAYS)
    c_hi     = float(consol["High"].max())
    c_lo     = float(consol["Low"].min())
    rng_pct  = (c_hi - c_lo) / price * 100
    if rng_pct > CONSOL_MAX_PCT:
        return None, f"range too wide ({rng_pct:.1f}%) — not consolidating"

    vol_dry  = float(consol["Volume"].mean()) < float(full_vol["Volume"].mean())

    # Breakout / near-breakout check
    # Use last 2 trading days in case today is a weekend (last row = Friday)
    last_day   = df.iloc[-1]
    prior_win  = df.iloc[-(CONSOL_DAYS + 1):-1]
    avg_vol    = float(df.tail(ADR_DAYS + 1).iloc[:-1]["Volume"].mean())
    prior_high = float(prior_win["High"].max())
    last_vol   = float(last_day["Volume"])
    last_close = float(last_day["Close"])

    vol_ok    = avg_vol == 0 or last_vol >= BREAKOUT_VOL_MULT * avg_vol
    broke_out = last_close > prior_high and vol_ok

    # "Buy Soon" — within NEAR_BREAKOUT_PCT% of the prior high, even without vol
    pct_from_high = (prior_high - last_close) / prior_high * 100
    near_break    = not broke_out and pct_from_high <= NEAR_BREAKOUT_PCT

    if broke_out:
        signal = "BREAKOUT"
    elif near_break:
        signal = "BUY SOON"
    else:
        signal = "WATCH"

    risk = round(price - c_lo, 2)
    return {
        "ticker":    ticker,
        "price":     price,
        "adr":       round(adr, 2),
        "momentum":  round(mom, 1),
        "range_pct": round(rng_pct, 2),
        "vol_dry":   vol_dry,
        "signal":    signal,
        "stop":      round(c_lo, 2),
        "risk":      risk,
        "t1":        round(price + risk, 2),
        "t2":        round(price + 2 * risk, 2),
        "t3":        round(price + 3 * risk, 2),
        "pct_from_high": round(pct_from_high, 1),
    }, None


def run_scan(tickers, progress_bar, status_text):
    passing, rejected = [], []
    for i, t in enumerate(tickers):
        progress_bar.progress((i + 1) / max(len(tickers), 1))
        status_text.text(f"Checking {t}... ({i+1} of {len(tickers)})")
        result, reason = scan_one(t)
        if result:
            passing.append(result)
        else:
            rejected.append((t, reason))
    progress_bar.progress(1.0)
    status_text.text(f"Done — {len(passing)} setups found out of {len(tickers)} stocks checked.")
    return passing, rejected


# ── daily position check ──────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def daily_check(ticker, entry_price, entry_date_str):
    """Returns dict with hold/sell signal and supporting data."""
    try:
        df = yf.download(ticker, period="40d", progress=False, auto_adjust=True, threads=False)
        df = flatten(df)
        if df.empty:
            return None
        closes      = df["Close"].dropna()
        price       = float(closes.iloc[-1])
        ma10_val    = float(closes.iloc[-MA_10:].mean()) if len(closes) >= MA_10 else None
        chg_pct     = (price - entry_price) / entry_price * 100
        held        = days_held(entry_date_str)
        below_ma10  = (ma10_val is not None and price < ma10_val)

        if below_ma10:
            signal = "EXIT"
            reason = f"Price (${price:.2f}) dropped below the 10-day average (${ma10_val:.2f}). Exit now."
        elif 3 <= held <= 5:
            signal = "PARTIAL SELL"
            reason = f"Day {held} of holding. Consider selling half and moving stop to breakeven (${entry_price:.2f})."
        else:
            signal = "HOLD"
            gap = price - ma10_val if ma10_val else 0
            reason = f"Still above 10-day average by ${gap:.2f}. Keep holding."

        return {
            "ticker":    ticker,
            "price":     price,
            "chg_pct":   chg_pct,
            "ma10":      ma10_val,
            "held":      held,
            "signal":    signal,
            "reason":    reason,
        }
    except Exception:
        return None


# ── scoring ───────────────────────────────────────────────────────────────────

def score_stock(s):
    score = 0

    # Prior uptrend
    if s["momentum"] >= 100:   score += 30
    elif s["momentum"] >= 60:  score += 20
    else:                      score += 10   # 30-60%

    # ADR
    if s["adr"] >= 7:          score += 25
    elif s["adr"] >= 5:        score += 20
    else:                      score += 10   # 3-5%

    # Volume drying up
    if s["vol_dry"]:           score += 20

    # Base width
    if s["range_pct"] < 8:     score += 20
    elif s["range_pct"] <= 10: score += 10
    else:                      score += 5    # 10-12%

    # Distance from breakout level
    pfh = s.get("pct_from_high", 999)
    if pfh < 1:                score += 25
    elif pfh <= 2:             score += 15
    elif pfh <= 3:             score += 5

    # Target 1 at least 5% above entry
    upside = (s["t1"] - s["price"]) / s["price"] * 100 if s["price"] else 0
    if upside >= 5:            score += 10

    return score


def hard_disqualify(s):
    """Return a reason string if the stock should be removed, else None."""
    if s["price"] < 5:
        return f"price ${s['price']:.2f} under $5"
    if s["range_pct"] > 12:
        return f"base too wide ({s['range_pct']:.1f}%)"
    if s["momentum"] < 30:
        return f"prior move too small ({s['momentum']:.0f}%)"
    if s["adr"] < 3:
        return f"ADR too low ({s['adr']:.1f}%)"
    if not s["vol_dry"] and s.get("pct_from_high", 999) > 1.5:
        return "volume not drying up and not close enough to breakout"
    return None


# ── chart ─────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def build_chart(ticker, entry, stop, t1, t2, t3):
    end   = datetime.today()
    start = end - timedelta(days=120)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), progress=False,
                     auto_adjust=True, threads=False)
    df = flatten(df)
    if df.empty:
        return None

    closes = df["Close"].dropna()
    ma10s  = closes.rolling(MA_10).mean()
    ma50s  = closes.rolling(MA_50).mean()

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=closes.index, y=closes.values, name="Price",
                             line=dict(color="#4488ff", width=2),
                             fill="tozeroy", fillcolor="rgba(68,136,255,0.06)"))
    fig.add_trace(go.Scatter(x=ma10s.index, y=ma10s.values, name="10-day avg",
                             line=dict(color="#ffaa00", width=1.5, dash="dot")))
    fig.add_trace(go.Scatter(x=ma50s.index, y=ma50s.values, name="50-day avg",
                             line=dict(color="#888888", width=1, dash="dash")))

    fig.add_hline(y=entry, line=dict(color="#ffaa00", dash="dash", width=1.5),
                  annotation_text=f"Entry ${entry:.2f}", annotation_font_color="#ffaa00",
                  annotation_position="bottom right")
    fig.add_hline(y=stop, line=dict(color="#ff4444", dash="dot", width=1.5),
                  annotation_text=f"Stop ${stop:.2f}", annotation_font_color="#ff4444",
                  annotation_position="bottom right")
    for val, label, color in [(t1,"Target 1","#00d084"),(t2,"Target 2","#00b070"),(t3,"Target 3","#009060")]:
        if val:
            fig.add_hline(y=val, line=dict(color=color, dash="dot", width=1),
                          annotation_text=f"{label} ${val:.2f}", annotation_font_color=color,
                          annotation_position="top right")

    fig.update_layout(
        paper_bgcolor="#0f0f1a", plot_bgcolor="#0f0f1a",
        font=dict(color="#f0f0f0"), height=420,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(gridcolor="#222233", showgrid=True, color="#f0f0f0"),
        yaxis=dict(gridcolor="#222233", showgrid=True, color="#f0f0f0"),
        legend=dict(bgcolor="#1a1a2e", font=dict(color="#f0f0f0")),
    )
    return fig


# ═════════════════════════════════════════════════════════════════════════════
#  SIDEBAR NAV
# ═════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 📈 Swing Trader")
    st.divider()
    page = st.radio("", ["Today's Check", "Find Stocks", "My Watchlist", "Journal"], label_visibility="collapsed")
    st.divider()
    st.caption("Based on Qullamaggie's VCP strategy")


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE 1: TODAY'S CHECK
# ═════════════════════════════════════════════════════════════════════════════
if page == "Today's Check":
    st.title("📋 Today's Check")

    journal = load_journal()
    trade   = journal.get("open_trade")

    if not trade:
        st.markdown("""
        <div class="alert-blue">
        <b>No open position.</b><br>
        Go to <b>Find Stocks</b> to scan for breakout setups.
        </div>
        """, unsafe_allow_html=True)

    else:
        ticker     = trade["ticker"]
        entry      = trade["entry_price"]
        stop       = trade["stop_price"]
        risk       = trade.get("risk", entry - stop)
        t1, t2, t3 = trade.get("t1"), trade.get("t2"), trade.get("t3")

        with st.spinner(f"Checking {ticker} right now..."):
            check = daily_check(ticker, entry, trade["entry_date"])

        if check is None:
            st.error("Could not fetch data. Check your internet connection.")
        else:
            # ── main signal banner ────────────────────────────────────────────
            sig = check["signal"]
            if sig == "EXIT":
                st.markdown(f"""
                <div class="alert-red">
                <b style="font-size:20px">🔴 SELL — Exit your position</b><br><br>
                {check['reason']}
                </div>
                """, unsafe_allow_html=True)
            elif sig == "PARTIAL SELL":
                st.markdown(f"""
                <div class="alert-yellow">
                <b style="font-size:20px">🟡 Consider selling half</b><br><br>
                {check['reason']}
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="alert-green">
                <b style="font-size:20px">🟢 HOLD — Keep the position</b><br><br>
                {check['reason']}
                </div>
                """, unsafe_allow_html=True)

            st.markdown("")

            # ── numbers ───────────────────────────────────────────────────────
            chg = check["chg_pct"]
            col1, col2, col3, col4, col5 = st.columns(5)
            col1.metric("Stock",       ticker)
            col2.metric("Current",     f"${check['price']:.2f}", f"{chg:+.2f}%")
            col3.metric("You bought at", f"${entry:.2f}")
            col4.metric("Stop loss",   f"${stop:.2f}")
            col5.metric("Days held",   f"{check['held']}")

            if t1:
                st.markdown(f"**Targets:** &nbsp; 1st `${t1:.2f}` &nbsp;&nbsp; 2nd `${t2:.2f}` &nbsp;&nbsp; 3rd `${t3:.2f}`")

            # ── chart ─────────────────────────────────────────────────────────
            st.divider()
            fig = build_chart(ticker, entry, stop, t1, t2, t3)
            if fig:
                st.plotly_chart(fig, use_container_width=True)

            # ── close form ────────────────────────────────────────────────────
            st.divider()
            st.markdown("### Close this trade")
            with st.form("close"):
                default_exit = float(f"{check['price']:.2f}")
                exit_price   = st.number_input("Exit price $", min_value=0.01,
                                               value=default_exit, step=0.01)
                if st.form_submit_button("✅  Save exit & close trade", type="primary"):
                    ret    = (exit_price - entry) / entry * 100
                    r_mult = (exit_price - entry) / risk if risk else 0
                    closed = {
                        "ticker":      ticker,
                        "entry_price": entry,
                        "entry_date":  trade["entry_date"],
                        "exit_price":  exit_price,
                        "exit_date":   date.today().isoformat(),
                        "return_pct":  round(ret, 3),
                        "r_multiple":  round(r_mult, 2),
                        "result":      "WIN" if ret > 0 else "LOSS",
                    }
                    journal["closed_trades"].append(closed)
                    journal["open_trade"] = None
                    save_journal(journal)
                    if ret > 0:
                        st.success(f"Trade closed for a WIN: {ret:+.2f}%  ({r_mult:+.2f}R)")
                        st.balloons()
                    else:
                        st.warning(f"Trade closed for a LOSS: {ret:+.2f}%  ({r_mult:+.2f}R)")
                    time.sleep(2)
                    st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE 2: FIND STOCKS
# ═════════════════════════════════════════════════════════════════════════════
elif page == "Find Stocks":
    st.title("🔍 Find Stocks")
    st.markdown("Scans for stocks that are breaking out of a tight consolidation after a strong uptrend.")

    # ── universe selector ─────────────────────────────────────────────────────
    st.markdown("### What do you want to scan?")
    universe = st.selectbox(
        "",
        UNIVERSE_OPTIONS,
        label_visibility="collapsed",
        format_func=lambda x: x,
    )
    # Divider rows are not selectable — nudge user past them
    if universe == "── Sectors ──":
        st.info("Select a specific sector from the dropdown.")
        st.stop()

    # ── filters (collapsible) ─────────────────────────────────────────────────
    with st.expander("⚙️  Filters (optional — defaults work well)"):
        col1, col2 = st.columns(2)
        price_min  = col1.number_input("Min price $", value=5, min_value=0, step=1)
        price_max  = col2.number_input("Max price $", value=5000, min_value=1, step=50)
        adr_min    = st.slider("Minimum daily move % (ADR)", 1.0, 10.0, ADR_MIN_PCT, 0.5,
                               help="Higher = more volatile stocks only")
        mom_min    = st.slider("Minimum prior uptrend %", 10, 100, int(MOMENTUM_MIN_PCT), 5,
                               help="How much the stock must have moved before the base")
        consol_max = st.slider("Maximum consolidation width %", 3.0, 20.0, CONSOL_MAX_PCT, 0.5,
                               help="Lower = tighter, higher-quality bases")

    run_btn = st.button("▶  Run Scan", type="primary", use_container_width=True)

    if run_btn:
        SECTOR_NAMES = [
            "Technology", "Healthcare", "Financials", "Energy",
            "Consumer Discretionary", "Industrials", "Real Estate",
        ]

        if universe.startswith("S&P 500"):
            with st.spinner("Loading S&P 500 list..."):
                tickers = get_sp500()
            if not tickers:
                st.error("Couldn't load S&P 500 list. Check your internet connection.")
                st.stop()
            univ_label = "S&P 500"
        elif universe.startswith("Nasdaq 100"):
            with st.spinner("Loading Nasdaq 100 list..."):
                tickers = get_nasdaq100()
            univ_label = "Nasdaq 100"
        elif universe.startswith("Dow Jones"):
            tickers    = get_dow30()
            univ_label = "Dow Jones 30"
        elif universe.startswith("Russell 2000"):
            tickers    = get_russell2000()
            univ_label = "Russell 2000 Small Caps"
        elif universe.startswith("Full US Market"):
            with st.spinner("Loading full US market list (~6,000 stocks)..."):
                tickers = get_full_market()
            if not tickers:
                st.error("Couldn't load market list. Check your internet connection.")
                st.stop()
            univ_label = "Full US Market"
        else:
            # Sector — strip " Sector" suffix to get the key
            sector_key = universe.replace(" Sector", "")
            tickers    = get_sector(sector_key)
            univ_label = f"{sector_key} Sector"

        st.info(f"Scanning: **{univ_label}** ({len(tickers)} tickers)")
        progress_bar = st.progress(0.0)
        status_text  = st.empty()
        passing, rejected = run_scan(tickers, progress_bar, status_text)
        st.session_state["passing"]    = passing
        st.session_state["rejected"]   = rejected
        st.session_state["scan_time"]  = datetime.now().strftime("%I:%M %p")
        st.session_state["univ_label"] = f"{univ_label} ({len(tickers)} tickers)"

    # ── results ───────────────────────────────────────────────────────────────
    if "passing" in st.session_state:
        passing   = st.session_state["passing"]
        rejected  = st.session_state["rejected"]
        scan_time = st.session_state.get("scan_time", "")

        # apply filters
        filtered  = [s for s in passing
                     if price_min <= s["price"] <= price_max
                     and s["adr"] >= adr_min
                     and s["momentum"] >= mom_min
                     and s["range_pct"] <= consol_max]

        # ── apply hard disqualifiers + scoring ───────────────────────────────
        scored = []
        dq_extra = []
        for s in filtered:
            reason = hard_disqualify(s)
            if reason:
                dq_extra.append((s["ticker"], reason))
            else:
                s["score"] = score_stock(s)
                scored.append(s)

        breakouts = sorted([s for s in scored if s["signal"] == "BREAKOUT"], key=lambda x: -x["score"])
        buy_soon  = sorted([s for s in scored if s["signal"] == "BUY SOON"],  key=lambda x: -x["score"])
        on_watch  = sorted([s for s in scored if s["signal"] == "WATCH"],     key=lambda x: -x["score"])[:10]

        univ_label = st.session_state.get("univ_label", "")
        st.markdown(
            f"**Scanning: {univ_label}** &nbsp;·&nbsp; finished at {scan_time}  \n"
            f"🚀 {len(breakouts)} breakouts &nbsp;|&nbsp; "
            f"⚡ {len(buy_soon)} buy soon &nbsp;|&nbsp; "
            f"👁 {len(on_watch)} on watch (top 10)"
        )

        journal = load_journal()

        _btn_keys_used = set()

        def open_trade_button(s):
            if journal.get("open_trade"):
                st.caption(f"⚠️  Close your open {journal['open_trade']['ticker']} trade first.")
            else:
                base_key = f"b_{s['ticker']}"
                key = base_key
                suffix = 0
                while key in _btn_keys_used:
                    suffix += 1
                    key = f"{base_key}_{suffix}"
                _btn_keys_used.add(key)
                if st.button(f"Open trade on {s['ticker']}", key=key, type="primary"):
                    trade = {
                        "ticker":      s["ticker"],
                        "entry_price": s["price"],
                        "entry_date":  date.today().isoformat(),
                        "stop_price":  s["stop"],
                        "risk":        s["risk"],
                        "t1": s["t1"], "t2": s["t2"], "t3": s["t3"],
                        "adr": s["adr"], "momentum": s["momentum"],
                    }
                    journal["open_trade"] = trade
                    save_journal(journal)
                    st.success("Trade opened! Go to **Today's Check** to monitor it daily.")

        def add_to_mywl_button(s):
            mywl = load_my_watchlist()
            already = s["ticker"] in [x["ticker"] for x in mywl]
            if already:
                st.caption(f"✅ Already in My Watchlist")
            else:
                if st.button(f"⭐ Add {s['ticker']} to My Watchlist",
                             key=f"mywl_{s['ticker']}_{s.get('score',0)}",
                             use_container_width=True):
                    mywl.append({"ticker": s["ticker"], "added": date.today().isoformat()})
                    save_my_watchlist(mywl)
                    st.success(f"{s['ticker']} added to My Watchlist.")

        def stock_card(s, rank=None):
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([2, 2, 2, 3])
                label = f"#{rank} — {s['ticker']}" if rank else s["ticker"]
                col1.markdown(f"### {label}")
                col1.markdown(f"**${s['price']:.2f}** &nbsp; Score: **{s['score']}/100**")
                col2.metric("Daily range",   f"{s['adr']:.1f}%")
                col2.metric("Prior uptrend", f"{s['momentum']:.0f}%")
                col3.metric("Base width",    f"{s['range_pct']:.1f}%")
                col3.metric("Vol drying",    "Yes ✅" if s["vol_dry"] else "No")
                col4.markdown(f"**Stop loss:** ${s['stop']:.2f}  *(risk: ${s['risk']:.2f}/share)*")
                col4.markdown(f"**Target 1:** ${s['t1']:.2f} &nbsp; **Target 2:** ${s['t2']:.2f} &nbsp; **Target 3:** ${s['t3']:.2f}")
                bcol1, bcol2 = st.columns(2)
                with bcol1:
                    open_trade_button(s)
                with bcol2:
                    add_to_mywl_button(s)

        # ── THE BUY — top scored breakout or buy-soon ─────────────────────────
        st.divider()
        candidates = (breakouts + buy_soon)[:1]
        top = candidates[0] if candidates else None

        if top and top["score"] >= 60:
            st.markdown(f"""
            <div class="alert-green">
            <b style="font-size:22px">🏆 THE BUY THIS WEEK — {top['ticker']}</b><br><br>
            Score: <b>{top['score']}/100</b> &nbsp;|&nbsp;
            Price: <b>${top['price']:.2f}</b> &nbsp;|&nbsp;
            Stop loss: <b>${top['stop']:.2f}</b><br>
            Target 1: <b>${top['t1']:.2f}</b> &nbsp;|&nbsp;
            Target 2: <b>${top['t2']:.2f}</b> &nbsp;|&nbsp;
            Risk per share: <b>${top['risk']:.2f}</b>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("")
            open_trade_button(top)
            # mark this ticker's button as used so stock_card doesn't create a duplicate
            _btn_keys_used.add(f"b_{top['ticker']}")
        else:
            st.markdown("""
            <div class="alert-yellow">
            <b style="font-size:18px">⏸ NO TRADE THIS WEEK</b><br>
            No stock scored above 60/100. Wait for a better setup rather than forcing a trade.
            </div>
            """, unsafe_allow_html=True)

        # ── BREAKOUTS ─────────────────────────────────────────────────────────
        if breakouts:
            st.divider()
            st.markdown("## 🚀 Breaking Out Now")
            st.caption("Crossed above the consolidation range on volume. Highest-quality signal.")
            for i, s in enumerate(breakouts, 1):
                stock_card(s, rank=i)

        # ── BUY SOON — top 3 only ─────────────────────────────────────────────
        if buy_soon:
            st.divider()
            st.markdown("## ⚡ Top 3 — Within 3% of Breaking Out")
            st.caption("Right at the edge of the base, ranked by score. Check back tomorrow.")
            for i, s in enumerate(buy_soon[:3], 1):
                stock_card(s, rank=i)

        # ── ON WATCH — top 10 as table ────────────────────────────────────────
        if on_watch:
            st.divider()
            st.markdown("## 👁  On Watch — Top 10 by Score")
            st.caption("Good structure but not ready yet.")
            rows = []
            for s in on_watch:
                rows.append({
                    "Score":         s["score"],
                    "Ticker":        s["ticker"],
                    "Price":         f"${s['price']:.2f}",
                    "Daily Move":    f"{s['adr']:.1f}%",
                    "Prior Uptrend": f"{s['momentum']:.0f}%",
                    "Base Width":    f"{s['range_pct']:.1f}%",
                    "Vol Dropping":  "Yes ✅" if s["vol_dry"] else "No",
                    "Stop If Bought":f"${s['stop']:.2f}",
                    "Target 1":      f"${s['t1']:.2f}",
                    "Target 2":      f"${s['t2']:.2f}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        if not filtered:
            st.info("No stocks passed the filters. Try loosening the sliders, or add more tickers to your watchlist.")

        all_rejected = rejected + dq_extra
        with st.expander(f"See why stocks were filtered out ({len(all_rejected)} rejected)"):
            for t, reason in all_rejected:
                st.caption(f"**{t}** — {reason}")



# ═════════════════════════════════════════════════════════════════════════════
#  PAGE 3: MY WATCHLIST
# ═════════════════════════════════════════════════════════════════════════════
elif page == "My Watchlist":
    st.title("⭐ My Watchlist")
    st.caption("Your personal list of stocks to track. Separate from the scanner — add anything you're interested in.")

    items = load_my_watchlist()
    tickers_in_list = [i["ticker"] for i in items]

    # ── add ticker ────────────────────────────────────────────────────────────
    # NOTE: logic is inside the form so widget values are readable on submit.
    # clear_on_submit is intentionally NOT used — it resets values before the
    # handler runs, making new_t always empty.
    with st.form("add_to_mywl"):
        col1, col2 = st.columns([4, 1])
        new_t = col1.text_input("", placeholder="Type a ticker, e.g. PLTR",
                                label_visibility="collapsed")
        submitted = col2.form_submit_button("Add to Watchlist", type="primary", use_container_width=True)
        if submitted:
            ticker_to_add = new_t.upper().strip()
            if not ticker_to_add:
                st.warning("Enter a ticker symbol first.")
            elif ticker_to_add in tickers_in_list:
                st.warning(f"{ticker_to_add} is already in your watchlist.")
            else:
                items.append({"ticker": ticker_to_add, "added": date.today().isoformat()})
                save_my_watchlist(items)   # updates session state instantly
                tickers_in_list.append(ticker_to_add)
                st.success(f"{ticker_to_add} added to your watchlist.")

    # ── current list ──────────────────────────────────────────────────────────
    if not items:
        st.info("Your watchlist is empty. Add some tickers above to get started.")
    else:
        st.divider()

        # ── run scan button ───────────────────────────────────────────────────
        run_btn = st.button("🔍  Run Watchlist Scan", type="primary", use_container_width=True)

        if run_btn:
            results = {}
            prog = st.progress(0.0)
            stat = st.empty()
            for i, item in enumerate(items):
                t = item["ticker"]
                prog.progress((i + 1) / len(items))
                stat.text(f"Scanning {t}... ({i+1}/{len(items)})")
                result, reason = scan_one(t)
                if result:
                    result["score"] = score_stock(result)
                results[t] = {
                    "result":      result,
                    "reason":      reason or "",
                    "scanned_at":  datetime.now().strftime("%I:%M %p"),
                }
            prog.progress(1.0)
            stat.text("Done.")
            st.session_state["mywl_results"] = results

        # ── show saved list with remove buttons ───────────────────────────────
        if "mywl_results" not in st.session_state:
            st.markdown("### Your stocks")
            rows = []
            for item in items:
                rows.append({"Ticker": item["ticker"], "Added": item["added"]})
            df_list = pd.DataFrame(rows)

            for i, item in enumerate(items):
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"**{item['ticker']}** — added {item['added']}")
                if c2.button("Remove", key=f"rm_mywl_{item['ticker']}_{i}"):
                    items = [x for x in items if x["ticker"] != item["ticker"]]
                    save_my_watchlist(items)
                    if "mywl_results" in st.session_state:
                        del st.session_state["mywl_results"]
                    st.rerun()

        # ── scan results ──────────────────────────────────────────────────────
        else:
            results = st.session_state["mywl_results"]

            def momentum_status(score):
                if score >= 80:   return "🔥 BREAKING OUT"
                elif score >= 60: return "⚡ Getting Close"
                elif score >= 40: return "👀 Still Setting Up"
                elif score >= 20: return "📉 Losing Steam"
                else:             return "❌ Broken"

            def row_color(score):
                if score >= 60:   return "background-color: #0d3b1e"
                elif score >= 40: return "background-color: #3b2d0d"
                else:             return "background-color: #3b0d0d"

            # build rows
            rows = []
            for item in items:
                t   = item["ticker"]
                res = results.get(t, {})
                r   = res.get("result")
                scanned_at = res.get("scanned_at", "—")

                if r:
                    pfh = r.get("pct_from_high", None)
                    rows.append({
                        "_score":          r["score"],
                        "Ticker":          t,
                        "Price":           f"${r['price']:.2f}",
                        "Score":           r["score"],
                        "Status":          momentum_status(r["score"]),
                        "ADR%":            f"{r['adr']:.1f}%",
                        "Prior Uptrend":   f"{r['momentum']:.0f}%",
                        "Base Width":      f"{r['range_pct']:.1f}%",
                        "Vol Dropping":    "Yes ✅" if r["vol_dry"] else "No",
                        "From Breakout":   f"{pfh:.1f}%" if pfh is not None else "—",
                        "Stop If Bought":  f"${r['stop']:.2f}",
                        "Target 1":        f"${r['t1']:.2f}",
                        "Target 2":        f"${r['t2']:.2f}",
                        "Last Scanned":    scanned_at,
                        "Added":           item["added"],
                    })
                else:
                    reason_str = res.get("reason", "")
                    if not reason_str:
                        status_str = "❌ No data from Yahoo Finance"
                    elif "not enough data" in reason_str:
                        status_str = "❌ Invalid or delisted ticker"
                    else:
                        status_str = f"⛔ {reason_str}"
                    rows.append({
                        "_score":         -1,
                        "Ticker":          t,
                        "Price":           "—",
                        "Score":           "—",
                        "Status":          status_str,
                        "ADR%":            "—",
                        "Prior Uptrend":   "—",
                        "Base Width":      "—",
                        "Vol Dropping":    "—",
                        "From Breakout":   "—",
                        "Stop If Bought":  "—",
                        "Target 1":        "—",
                        "Target 2":        "—",
                        "Last Scanned":    scanned_at,
                        "Added":           item["added"],
                    })

            rows.sort(key=lambda x: x["_score"] if isinstance(x["_score"], int) else -1, reverse=True)

            # summary line
            hot   = sum(1 for r in rows if isinstance(r["_score"], int) and r["_score"] >= 60)
            setup = sum(1 for r in rows if isinstance(r["_score"], int) and 40 <= r["_score"] < 60)
            cold  = sum(1 for r in rows if isinstance(r["_score"], int) and r["_score"] < 40)
            st.markdown(f"**{hot} getting close or breaking out &nbsp;|&nbsp; {setup} still setting up &nbsp;|&nbsp; {cold} losing steam or broken**")
            st.divider()

            # table (drop internal sort key)
            display_cols = ["Ticker","Price","Score","Status","ADR%","Prior Uptrend",
                            "Base Width","Vol Dropping","From Breakout",
                            "Stop If Bought","Target 1","Target 2","Last Scanned","Added"]
            df = pd.DataFrame(rows)[display_cols]

            def highlight_row(row):
                try:
                    score = int(row["Score"])
                except (ValueError, TypeError):
                    return [""] * len(row)
                if score >= 60:
                    bg = "background-color: #0d3b1e"
                elif score >= 40:
                    bg = "background-color: #2d2500"
                else:
                    bg = "background-color: #2d0a0a"
                return [bg] * len(row)

            st.dataframe(
                df.style.apply(highlight_row, axis=1),
                use_container_width=True,
                hide_index=True,
            )

            # remove buttons below table
            st.divider()
            st.markdown("**Remove from watchlist:**")
            cols = st.columns(6)
            for i, item in enumerate(items):
                if cols[i % 6].button(f"✕ {item['ticker']}", key=f"rm2_mywl_{item['ticker']}_{i}", use_container_width=True):
                    items = [x for x in items if x["ticker"] != item["ticker"]]
                    save_my_watchlist(items)
                    del st.session_state["mywl_results"]
                    st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE 4: TRADE HISTORY
# ═════════════════════════════════════════════════════════════════════════════
elif page == "Journal":
    st.title("📓 Journal")
    journal = load_journal()
    trades  = journal.get("closed_trades", [])

    if not trades:
        st.info("No closed trades yet. Once you close a position it will appear here.")
    else:
        returns = [t["return_pct"] for t in trades]
        r_mults = [t.get("r_multiple", 0) for t in trades]
        wins    = [r for r in returns if r > 0]
        wr      = len(wins) / len(returns) * 100

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Trades",       len(trades))
        col2.metric("Win rate",     f"{wr:.0f}%")
        col3.metric("Avg return",   f"{sum(returns)/len(returns):+.2f}%")
        col4.metric("Avg R-multiple", f"{sum(r_mults)/len(r_mults):+.2f}R")
        col5.metric("Total",        f"{sum(returns):+.2f}%")

        # P&L chart
        st.divider()
        df_c = pd.DataFrame(trades).sort_values("exit_date")
        df_c["cumulative"] = df_c["return_pct"].cumsum()
        colors = ["#00d084" if r > 0 else "#ff4444" for r in df_c["return_pct"]]

        fig = go.Figure()
        fig.add_trace(go.Bar(x=df_c["exit_date"], y=df_c["return_pct"],
                             marker_color=colors, name="Return %",
                             text=[f"{r:+.1f}%" for r in df_c["return_pct"]],
                             textposition="outside", textfont=dict(color="#f0f0f0")))
        fig.add_trace(go.Scatter(x=df_c["exit_date"], y=df_c["cumulative"],
                                 mode="lines+markers", name="Running total",
                                 line=dict(color="#ffaa00", width=2), yaxis="y2"))
        fig.update_layout(
            paper_bgcolor="#0f0f1a", plot_bgcolor="#0f0f1a",
            font=dict(color="#f0f0f0"), height=320,
            margin=dict(l=10, r=10, t=20, b=10),
            xaxis=dict(gridcolor="#222233", color="#f0f0f0"),
            yaxis=dict(gridcolor="#222233", color="#f0f0f0", title="Return %"),
            yaxis2=dict(overlaying="y", side="right", color="#f0f0f0", title="Running total %"),
            legend=dict(bgcolor="#1a1a2e", font=dict(color="#f0f0f0")),
        )
        st.plotly_chart(fig, use_container_width=True)

        # table
        st.divider()
        rows = []
        for t in sorted(trades, key=lambda x: x["exit_date"], reverse=True):
            rows.append({
                "Date closed":  t["exit_date"],
                "Stock":        t["ticker"],
                "Bought at":    f"${t['entry_price']:.2f}",
                "Sold at":      f"${t['exit_price']:.2f}",
                "Return":       f"{t['return_pct']:+.2f}%",
                "R-multiple":   f"{t.get('r_multiple', 0):+.2f}R",
                "Result":       t["result"],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
